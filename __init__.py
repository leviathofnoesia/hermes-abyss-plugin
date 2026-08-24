"""Abyss Plugin — Raindrop-style Observability for Hermes AI Agents

This plugin automatically records every tool call, conversation, and action
into the Abyss activity database. Inspired by Raindrop.ai (the "Sentry for AI
Agents"), it implements self-diagnostics, signal detection, and incident
tracking.

The desktop UI plugin reads from this data to display the activity feed,
calendar, tracing view, global search, and Hermes brain graph.

Hook points:
  - pre_tool_call / post_tool_call  → log every tool invocation + detect failures
  - on_session_start / on_session_end → log conversation lifecycle
  - pre_llm_call / post_llm_call   → log LLM interactions + detect drift

Key concepts (Raindrop-inspired):
  - Signals:     anomalies detected by classifiers or self-diagnostics
  - Incidents:   groups of related signals with shared root cause
  - Self-diagnostic: agents proactively report capability gaps and failures
  - Traces:      full timeline of events for a conversation/session
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Sibling-module bootstrap. The plugin manager loads this file by location
# (spec_from_file_location) WITHOUT putting the plugin directory on sys.path,
# so a plain ``from abyss_wave import ...`` would resolve to a same-named
# stdlib module (``wave``) or fail. This mirrors the pattern plugin_api.py
# already uses in production: make the plugin root importable first.
# ---------------------------------------------------------------------------
_PLUGIN_ROOT = str(Path(__file__).resolve().parent)
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)

# ---------------------------------------------------------------------------
# SRP module re-exports (Clean Architecture extraction). Each module is
# self-contained and imports the core lazily inside functions (see
# abyss_wave.py) — these imports keep the public surface of this module
# exactly as before for tests, plugin_api.py and the dashboard.
# ---------------------------------------------------------------------------
from abyss_signals import _SIGNAL_PATTERNS, _detect_signals  # noqa: E402,F401
from abyss_analytics import get_health, get_trends, get_failures, export_data, get_status, get_performance  # noqa: E402,F401
from abyss_incidents import _SEVERITY_RANK, _acknowledge_signal, _resolve_signal, _resolve_signals_bulk, _update_incident_status, _cluster_incidents  # noqa: E402,F401
from abyss_agent import (  # noqa: E402,F401
    _redact, _resolve_agent_cmd, _prune_resolutions, _spawn_agent,
    _AGENT_PROCS, _prune_agent_procs, _cleanup_agent_procs,
    _read_report_file, _write_report_file, _mark_resolution,
    _resolution_context, _build_resolver_prompt, _resolution_finalize,
    _resolution_worker, _dispatch_resolution,
)
from abyss_doctor import (  # noqa: E402,F401
    _doctor_context, _doctor_worker, _dispatch_doctor, _doctor_report,
    _doctor_log,
    _run_benchmark, _doctor_last, _doctor_apply_worker, _dispatch_doctor_apply,
)

# Hermes profile home
HERMES_HOME = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
PROFILE_HOME = os.environ.get("HERMES_PROFILE_HOME", HERMES_HOME)

# Plugin data directory
PLUGIN_DATA = Path(PROFILE_HOME) / "abyss-data"
PLUGIN_DATA.mkdir(parents=True, exist_ok=True)

ACTIVITY_DB = PLUGIN_DATA / "activity.db"
TRACE_DB = PLUGIN_DATA / "traces.db"

logger = logging.getLogger("hermes.plugins.abyss")

# Thread lock for DB writes
_lock = threading.Lock()

# One-time schema bootstrap guard. _init_db() is called from EVERY hook handler
# and every _add_activity/_add_trace/_record_signals (the hot path), and each
# call opens 4 sqlite connections and runs ~15 CREATE TABLE/INDEX + PRAGMA
# statements. DDL takes SQLite schema locks, so this redundant per-event work
# is a direct contributor to the cross-process 'database is locked' WAL
# collisions abyss_wave._wave_with_retry exists to survive. Memoize after the
# first SUCCESSFUL init: PLUGIN_DATA is an import-time constant so the schema
# cannot move mid-process, and reset_db() only DELETEs rows (never drops
# tables or files). If init raises, the flag stays unset and the next caller
# retries the full bootstrap.
_DB_READY = False
_DB_INIT_LOCK = threading.Lock()


def _get_activity_conn():
    """Get SQLite connection for activity feed."""
    conn = sqlite3.connect(str(ACTIVITY_DB), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _get_trace_conn():
    """Get SQLite connection for traces."""
    conn = sqlite3.connect(str(TRACE_DB), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _migrate_schema(conn):
    """Idempotently add columns introduced after the original scaffold."""
    try:
        incident_cols = {r[1] for r in conn.execute("PRAGMA table_info(incidents)").fetchall()}
        if "signal_ids" not in incident_cols:
            conn.execute("ALTER TABLE incidents ADD COLUMN signal_ids TEXT")
        if "resolved_at" not in incident_cols:
            conn.execute("ALTER TABLE incidents ADD COLUMN resolved_at TEXT")
        signal_cols = {r[1] for r in conn.execute("PRAGMA table_info(signals)").fetchall()}
        if "incident_id" not in signal_cols:
            conn.execute("ALTER TABLE signals ADD COLUMN incident_id INTEGER")
        if "details" not in signal_cols:
            conn.execute("ALTER TABLE signals ADD COLUMN details TEXT")
        if "acknowledged_at" not in signal_cols:
            conn.execute("ALTER TABLE signals ADD COLUMN acknowledged_at TEXT")
        if "resolved_at" not in signal_cols:
            conn.execute("ALTER TABLE signals ADD COLUMN resolved_at TEXT")
        # Agent-powered resolution columns (both signals and incidents)
        for table in ("signals", "incidents"):
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if "resolution_status" not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN resolution_status TEXT DEFAULT 'none'")
            if "resolution_note" not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN resolution_note TEXT")
            if "resolution_started_at" not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN resolution_started_at TEXT")
            if "resolution_finished_at" not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN resolution_finished_at TEXT")
    except sqlite3.Error as exc:
        logger.debug("Abyss schema migration skipped: %s", exc)


def _init_db():
    """Initialize databases if not exists (memoized after first success).

    The recording hot path (every hook event) calls _init_db(); after the
    first successful bootstrap this returns immediately without touching
    SQLite, eliminating ~12 connection opens + ~45 DDL statements per event
    (DDL schema locks were a contributor to 'database is locked' WAL races).
    """
    global _DB_READY
    if _DB_READY:
        return
    with _DB_INIT_LOCK:
        if _DB_READY:
            return
        _init_db_unlocked()
        _DB_READY = True


def _init_db_unlocked():
    """Run the actual schema bootstrap. Caller must hold _DB_INIT_LOCK."""
    conn = _get_activity_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            action TEXT NOT NULL,
            description TEXT,
            category TEXT,
            status TEXT DEFAULT 'completed',
            metadata TEXT,
            session_id TEXT,
            tool_name TEXT,
            args TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_activity_ts ON activity(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_activity_cat ON activity(category)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_activity_session ON activity(session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_activity_tool ON activity(tool_name)")
    conn.commit()

    # Trace DB for conversation timelines
    tconn = _get_trace_conn()
    tconn.execute("""
        CREATE TABLE IF NOT EXISTS traces (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            event_data TEXT,
            timestamp TEXT NOT NULL,
            depth INTEGER DEFAULT 0,
            parent_id INTEGER,
            duration_ms INTEGER
        )
    """)
    tconn.execute("CREATE INDEX IF NOT EXISTS idx_traces_session ON traces(session_id)")
    tconn.execute("CREATE INDEX IF NOT EXISTS idx_traces_ts ON traces(timestamp)")
    tconn.execute("CREATE INDEX IF NOT EXISTS idx_traces_parent ON traces(parent_id)")
    tconn.execute("CREATE INDEX IF NOT EXISTS idx_traces_type ON traces(event_type)")
    tconn.commit()
    tconn.close()

    # WAL mode for concurrent gateway + web-server access
    try:
        wal_conn = _get_activity_conn()
        wal_conn.execute("PRAGMA journal_mode=WAL")
        wal_conn.close()
        wal_conn = _get_trace_conn()
        wal_conn.execute("PRAGMA journal_mode=WAL")
        wal_conn.close()
    except sqlite3.Error as exc:
        logger.debug("Abyss WAL pragma skipped: %s", exc)

    # Signals & Incidents tables (Raindrop.ai-inspired observability)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            signal_type TEXT NOT NULL,
            severity TEXT DEFAULT 'warning',
            label TEXT,
            description TEXT,
            session_id TEXT,
            activity_id INTEGER,
            source TEXT DEFAULT 'classifier',
            acknowledged INTEGER DEFAULT 0,
            resolved INTEGER DEFAULT 0
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_session ON signals(session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_type ON signals(signal_type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_severity ON signals(severity)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS incidents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            severity TEXT DEFAULT 'warning',
            signal_count INTEGER DEFAULT 0,
            session_ids TEXT,
            pattern TEXT,
            status TEXT DEFAULT 'open',
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_incidents_ts ON incidents(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents(status)")

    # Migrate AFTER signals/incidents exist. _migrate_schema uses ALTER TABLE,
    # which fails on a not-yet-created table — running it before the signals/
    # incidents CREATE TABLE statements means every column it adds (details,
    # incident_id, acknowledged_at, resolved_at, resolution_*) is silently
    # skipped on first init. The legacy code masked this by calling _init_db()
    # on every write (the second call migrated the now-existing tables); the
    # memoized single-init requires the correct order.
    _migrate_schema(conn)

    # Wave tables — August 2026 plugin-interface expansion (#64182)
    try:
        from abyss_wave import wave_init_db

        wave_init_db(conn)
    except Exception as exc:
        logger.debug("Abyss wave table init skipped: %s", exc)

    conn.commit()
    conn.close()


def _add_activity(
    action: str,
    description: str = "",
    category: str = "general",
    status: str = "completed",
    metadata: Optional[dict] = None,
    session_id: Optional[str] = None,
    tool_name: Optional[str] = None,
    args: Optional[dict] = None,
):
    """Record an activity entry. Thread-safe."""
    _init_db()
    ts = datetime.now().isoformat()

    with _lock:
        conn = _get_activity_conn()
        try:
            cursor = conn.execute(
                """INSERT INTO activity
                   (timestamp, action, description, category, status, metadata,
                    session_id, tool_name, args)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ts, action, description, category, status,
                    json.dumps(metadata) if metadata else None,
                    session_id, tool_name,
                    json.dumps(args) if args else None
                )
            )
            conn.commit()
            row_id = cursor.lastrowid
        except Exception as e:
            logger.error("Failed to add activity: %s", e)
            row_id = None
        finally:
            conn.close()

    return {"id": row_id, "timestamp": ts}


def _add_trace(
    session_id: str,
    event_type: str,
    event_data: Optional[dict] = None,
    parent_id: Optional[int] = None,
    duration_ms: Optional[int] = None,
):
    """Record a trace event for conversation timeline. Thread-safe."""
    _init_db()
    ts = datetime.now().isoformat()

    with _lock:
        conn = _get_trace_conn()
        try:
            cursor = conn.execute(
                """INSERT INTO traces
                   (session_id, event_type, event_data, timestamp, parent_id, duration_ms)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    session_id, event_type,
                    json.dumps(event_data) if event_data else None,
                    ts, parent_id, duration_ms
                )
            )
            conn.commit()
            row_id = cursor.lastrowid
        except Exception as e:
            logger.error("Failed to add trace: %s", e)
            row_id = None
        finally:
            conn.close()

    return row_id


# ---------------------------------------------------------------------------
# Hook handlers
# ---------------------------------------------------------------------------

def _safe_str_preview(value: Any, limit: int = 200) -> str:
    """Best-effort string preview of a hook payload value.

    ``str()`` on an arbitrary tool/LLM result can itself raise (an object
    whose ``__repr__``/``__str__`` blows up, e.g. on a lazy/partially
    destructured response). A hook callback must never raise into the host,
    so fall back to a type label instead of letting the exception escape.
    """
    if not value:
        return ""
    try:
        return str(value)[:limit]
    except Exception as exc:
        return f"<unserializable {type(value).__name__}: {exc!r}>"


def _on_pre_tool_call(
    tool_name: str = "",
    args: Optional[Dict[str, Any]] = None,
    task_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    **_: Any,
) -> None:
    """Log the start of a tool call."""
    _init_db()
    _add_trace(
        session_id=session_id,
        event_type="tool_call",
        event_data={"tool": tool_name, "args": args or {}, "phase": "start", "tool_call_id": tool_call_id},
    )
    _add_activity(
        action=f"tool_call_started",
        description=f"Invoked {tool_name}",
        category="tool",
        status="running",
        metadata={"tool": tool_name, "tool_call_id": tool_call_id},
        session_id=session_id,
        tool_name=tool_name,
        args=args or {},
    )


def _on_post_tool_call(
    tool_name: str = "",
    args: Optional[Dict[str, Any]] = None,
    result: Any = None,
    task_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    status: Optional[str] = None,
    error_type: Optional[str] = None,
    error_message: Optional[str] = None,
    duration_ms: int = 0,
    **_: Any,
) -> None:
    """Log the completion of a tool call + detect signals.

    Mirrors the real Hermes ``post_tool_call`` hook payload
    (see agent/tool_executor.py -> model_tools._emit_post_tool_call_hook):
    status, error_type, error_message and duration_ms arrive as structured
    fields — we use them for signal detection instead of only grepping text.
    """
    _init_db()
    result_preview = _safe_str_preview(result)
    ok = (status or "ok") != "error"
    _add_trace(
        session_id=session_id,
        event_type="tool_call",
        event_data={
            "tool": tool_name,
            "args": args or {},
            "result_preview": result_preview,
            "phase": "end",
            "status": status or ("ok" if ok else "error"),
            "error_type": error_type,
            "error_message": error_message,
            "duration_ms": duration_ms,
        },
        duration_ms=duration_ms,
    )
    activity_result = _add_activity(
        action=f"tool_call_completed",
        description=f"Completed {tool_name}",
        category="tool",
        status="completed" if ok else "error",
        metadata={
            "tool": tool_name,
            "result_preview": result_preview,
            "status": status or ("ok" if ok else "error"),
            "error_type": error_type,
            "error_message": error_message,
            "duration_ms": duration_ms,
        },
        session_id=session_id,
        tool_name=tool_name,
        args=args or {},
    )
    activity_id = activity_result.get("id") if isinstance(activity_result, dict) else None

    # Detect signals (Raindrop-style observability) using structured fields
    _detect_and_record_signals(
        tool_name=tool_name,
        result=result,
        session_id=session_id,
        status="error" if not ok else "completed",
        activity_id=activity_id or 0,
        error_type=error_type or "",
        error_message=error_message or "",
        duration_ms=duration_ms,
    )


def _on_pre_llm_call(
    model: str = "",
    user_message: str = "",
    session_id: str = "",
    task_id: str = "",
    platform: str = "",
    **_: Any,
) -> None:
    """Log the start of an LLM call.

    Real ``pre_llm_call`` payload carries ``user_message`` (not ``prompt``).
    """
    _init_db()
    _add_activity(
        action="llm_call_started",
        description=f"Calling {model}",
        category="llm",
        status="running",
        metadata={
            "model": model,
            "prompt_preview": (user_message or "")[:100],
            "platform": platform,
        },
        session_id=session_id,
    )


def _on_post_llm_call(
    model: str = "",
    assistant_response: Any = None,
    user_message: str = "",
    session_id: str = "",
    task_id: str = "",
    turn_id: str = "",
    platform: str = "",
    **_: Any,
) -> None:
    """Log the completion of an LLM call + detect vague replies.

    Real ``post_llm_call`` payload carries ``assistant_response`` (not ``result``).
    """
    _init_db()
    result_preview = _safe_str_preview(assistant_response)
    activity_result = _add_activity(
        action="llm_call_completed",
        description=f"Completed {model}",
        category="llm",
        status="completed",
        metadata={
            "model": model,
            "result_preview": result_preview,
            "turn_id": turn_id,
            "platform": platform,
        },
        session_id=session_id,
    )
    activity_id = activity_result.get("id") if isinstance(activity_result, dict) else None
    _detect_and_record_signals(
        tool_name="llm_call_completed",
        result=assistant_response,
        session_id=session_id,
        status="completed",
        activity_id=activity_id or 0,
    )
    _add_trace(
        session_id=session_id,
        event_type="llm_call",
        event_data={
            "model": model,
            "result_preview": result_preview,
            "turn_id": turn_id,
            "phase": "end",
        },
    )


def _on_session_start(
    session_id: str = "",
    source: str = "",
    **_: Any,
) -> None:
    """Log the start of a session."""
    _init_db()
    _add_activity(
        action="session_started",
        description=f"Session {str(session_id or '')[:8]} started via {source}",
        category="session",
        status="running",
        metadata={"source": source},
        session_id=session_id,
    )
    _add_trace(
        session_id=session_id,
        event_type="session_start",
        event_data={"source": source},
    )


def _on_session_end(
    session_id: str = "",
    completed: bool = True,
    interrupted: bool = False,
    **_: Any,
) -> None:
    """Log the end of a session."""
    _init_db()
    _add_activity(
        action="session_ended",
        description=f"Session {str(session_id or '')[:8]} {'completed' if completed else 'interrupted'}",
        category="session",
        status="completed",
        metadata={"completed": completed, "interrupted": interrupted},
        session_id=session_id,
    )
    _add_trace(
        session_id=session_id,
        event_type="session_end",
        event_data={"completed": completed, "interrupted": interrupted},
    )


# ---------------------------------------------------------------------------
# Raindrop-style observability: signals, incidents, self-diagnostics
# ---------------------------------------------------------------------------
# (_SIGNAL_PATTERNS + _detect_signals extracted to abyss_signals.py)


def _record_signals(signals: list, session_id: str, activity_id: int) -> list:
    """Persist detected signals into the signals table."""
    _init_db()
    recorded = []
    with _lock:
        conn = _get_activity_conn()
        try:
            for sig in signals:
                cursor = conn.execute(
                    """INSERT INTO signals
                       (timestamp, signal_type, severity, label, description,
                        session_id, activity_id, source, details)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'classifier', ?)""",
                    (
                        datetime.now().isoformat(),
                        sig["signal_type"], sig["severity"], sig["label"],
                        sig["description"], session_id, activity_id,
                        json.dumps(sig.get("details")) if sig.get("details") else None,
                    )
                )
                conn.commit()
                recorded_sig = {**sig, "id": cursor.lastrowid}
                recorded.append(recorded_sig)
                _alert_on_signal(recorded_sig)
                # Event bus (#64164): publish every detected signal so other
                # plugins can observe Abyss findings under the abyss: namespace.
                try:
                    from abyss_wave import emit_abyss_event

                    emit_abyss_event("signal_detected", {
                        "signal_id": cursor.lastrowid,
                        "signal_type": sig["signal_type"],
                        "severity": sig["severity"],
                        "label": sig["label"],
                        "session_id": session_id or "",
                    })
                except Exception as exc:
                    logger.debug("Abyss signal_detected emit failed: %s", exc)
        except Exception as e:
            logger.error("Failed to record signals: %s", e)
        finally:
            conn.close()
    return recorded


def _detect_and_record_signals(
    tool_name: str,
    result: Any,
    session_id: str,
    status: str,
    activity_id: int,
    error_type: str = "",
    error_message: str = "",
    duration_ms: int = 0,
):
    """Run signal detection and persist any found signals."""
    # Fail-open guard (#night-shift): signal detection must never raise out of
    # a hook callback into the host. _add_activity/_add_trace already swallow
    # their own DB errors; detection/classification gets the same treatment.
    try:
        signals = _detect_signals(
            tool_name, result, session_id, status, activity_id,
            error_type=error_type, error_message=error_message, duration_ms=duration_ms,
        )
        if signals:
            _record_signals(signals, session_id, activity_id)
            # Also update activity with signal flag
            conn = _get_activity_conn()
            try:
                conn.execute(
                    "UPDATE activity SET metadata = json_patch(COALESCE(metadata, '{}'), '{\"has_signals\": true}') WHERE id = ?",
                    (activity_id,)
                )
                conn.commit()
            except Exception:
                pass
            finally:
                conn.close()
    except Exception as exc:
        logger.debug("Abyss signal detection failed (ignored): %s", exc)
        signals = []

    # NoneType terminal command backoff guard
    # The LLM sometimes emits a terminal tool call with command=None
    # (a malformed tool call), which the terminal tool rejects with
    # "Invalid command: expected string, got NoneType". On the 3rd repeat of
    # this identical failure in the same session, emit a self_diagnostic signal
    # so operators have an explicit, low-noise marker for the retry storm
    # instead of a flood of generic tool_error signals that loop_detected
    # then amplifies. The terminal tool itself is not broken — this is the
    # model mis-halucinating an empty command argument.
    if tool_name == "terminal" and error_message and "NoneType" in error_message:
        try:
            conn = _get_activity_conn()
            try:
                prior = conn.execute(
                    """SELECT COUNT(*) AS c FROM activity
                       WHERE session_id = ? AND tool_name = 'terminal'
                         AND status = 'error'
                         AND json_extract(metadata, '$.error_message')
                           = ?""",
                    (session_id, error_message),
                ).fetchone()
            finally:
                conn.close()
            repeat_count = (prior["c"] if prior else 0) + 1
            if repeat_count >= 3:
                _record_self_diagnostic(
                    session_id,
                    capability="terminal_tool",
                    gap=(
                        "LLM repeatedly emitted terminal tool_call with "
                        "command=None (malformed tool call). "
                        f"Repeat #{repeat_count} in session "
                        f"{session_id}. Recommend operator backoff / "
                        "re-prompt with a concrete command."
                    ),
                    severity="warning",
                )
        except Exception:
            pass

    return signals


def _record_self_diagnostic(
    session_id: str,
    capability: str,
    gap: str,
    severity: str = "warning",
    **_: Any,
) -> int:
    """Record a self-diagnostic signal from the agent.

    This is the Raindrop 'Self Diagnostics' feature — agents proactively
    report capability gaps, missing context, persistent tool failures.
    """
    _init_db()
    ts = datetime.now().isoformat()
    with _lock:
        conn = _get_activity_conn()
        try:
            cursor = conn.execute(
                """INSERT INTO signals
                  (timestamp, signal_type, severity, label, description,
                   session_id, activity_id, source, details)
              VALUES (?, ?, ?, ?, ?, ?, ?, 'self_diagnostic', ?)""",
                (ts, "self_diagnostic", severity, capability, gap, session_id, None,
                 json.dumps({"capability": capability, "gap": gap}))
            )
            conn.commit()
            return cursor.lastrowid
        except Exception as e:
            logger.error("Failed to record self-diagnostic: %s", e)
            return None
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Triage helpers — acknowledge / resolve signals and incidents
# (_SEVERITY_RANK + triage trio extracted to abyss_incidents.py)
# ---------------------------------------------------------------------------


_RESOLUTION_DIR = PLUGIN_DATA / "resolutions"
_RESOLUTION_DIR.mkdir(parents=True, exist_ok=True)

_AGENT_DEFAULT_TIMEOUT = int(os.environ.get("ABYSS_AGENT_TIMEOUT", "1200") or 1200)

# ---------------------------------------------------------------------------
# Agent-powered resolution: _redact, _resolve_agent_cmd, _prune_resolutions,
# _spawn_agent, _AGENT_PROCS registry + cleanup, report IO, _mark_resolution,
# _resolution_context/_build_resolver_prompt/_resolution_finalize/_resolution_worker,
# _dispatch_resolution — extracted to abyss_agent.py
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Doctor: _doctor_context/_doctor_worker/_dispatch_doctor/_doctor_report,
# _run_benchmark/_doctor_last/_doctor_apply_worker/_dispatch_doctor_apply
# — extracted to abyss_doctor.py
# ---------------------------------------------------------------------------


# (_cluster_incidents extracted to abyss_incidents.py)


def _prune_data(days: int = 30) -> dict:
    """Delete activity/traces/signals/incidents + wave tables older than ``days``.

    Returns counts of deleted rows per table. ``days <= 0`` is a no-op.
    Since the Aug-2026 wave landed, the wave tables (plugin_events, streams,
    api_requests, subagents, approvals, commands, platform_events, skills)
    also carry timestamps and were NOT covered here — retention_days was
    silently no-op for them and they grew unbounded. They are pruned via
    abyss_wave.prune_wave_data (fail-open) and their counts merged.
    """
    if days <= 0:
        base = {"activity": 0, "traces": 0, "signals": 0, "incidents": 0}
        try:
            from abyss_wave import _WAVE_TABLES

            base.update({t: 0 for t in _WAVE_TABLES})
        except Exception:
            pass
        return base
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    counts = {}
    _init_db()
    conn = _get_activity_conn()
    try:
        with _lock:
            for table in ("activity", "signals", "incidents"):
                cur = conn.execute(f"DELETE FROM {table} WHERE timestamp < ?", (cutoff,))
                counts[table] = cur.rowcount
            conn.commit()
    finally:
        conn.close()
    tconn = _get_trace_conn()
    try:
        with _lock:
            cur = tconn.execute("DELETE FROM traces WHERE timestamp < ?", (cutoff,))
            counts["traces"] = cur.rowcount
            tconn.commit()
    finally:
        tconn.close()
    # Wave tables (same activity DB): prune fail-open and merge per-table
    # counts so /abyss prune and POST /prune report the full footprint.
    try:
        from abyss_wave import prune_wave_data

        counts.update(prune_wave_data(days))
    except Exception as exc:
        logger.debug("Abyss wave prune skipped: %s", exc)
    return counts


# ---------------------------------------------------------------------------
# Analytics: health score, trends, failure taxonomy, export, status
# (extracted to abyss_analytics.py)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Webhook alerting (Raindrop-style Slack/Discord/HTTP integration)
# ---------------------------------------------------------------------------

def _notify_webhook(title: str, message: str, severity: str, payload: Optional[dict] = None) -> bool:
    """Fire a webhook POST when ABYSS_WEBHOOK_URL is configured.

    Payload shape is a generic Slack-compatible block so it works with Slack,
    Discord, Teams or any simple HTTP endpoint. Returns True if the POST
    succeeded (2xx), False otherwise. No-op when no URL is configured.
    """
    url = os.environ.get("ABYSS_WEBHOOK_URL", "").strip()
    if not url:
        try:
            from abyss_wave import SETTINGS

            url = str(SETTINGS.get("webhook_url", "") or "").strip()
        except Exception:
            url = ""
    if not url:
        return False
    import urllib.request

    body = {
        "text": f"[Abyss] {title}",
        "attachments": [{
            "color": {"critical": "danger", "error": "danger", "warning": "warning", "info": "good"}.get(severity, "good"),
            "title": title,
            "text": message,
        }],
        "abyss": {
            "severity": severity,
            "generated_at": datetime.now().isoformat(),
            **(payload or {}),
        },
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", "User-Agent": "hermes-abyss/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return 200 <= resp.status < 300
    except Exception as exc:
        logger.debug("Abyss webhook alert failed: %s", exc)
        return False


def _alert_on_signal(sig: dict) -> None:
    """Send webhook alert for high-severity signals (error/critical)."""
    if sig.get("severity") in ("error", "critical"):
        _notify_webhook(
            f"Signal: {sig.get('label', sig.get('signal_type', 'unknown'))}",
            (sig.get("description") or "")[:500],
            sig.get("severity", "error"),
            {"signal_id": sig.get("id"), "signal_type": sig.get("signal_type"), "session_id": sig.get("session_id")},
        )
        try:
            from abyss_wave import emit_abyss_event

            emit_abyss_event("alert_fired", {
                "kind": "signal",
                "signal_id": sig.get("id"),
                "signal_type": sig.get("signal_type"),
                "severity": sig.get("severity"),
                "session_id": sig.get("session_id") or "",
            })
        except Exception as exc:
            logger.debug("Abyss alert_fired emit failed: %s", exc)


def _alert_on_incident(incident: dict) -> None:
    """Send webhook alert for new incidents (dedupe via status)."""
    _notify_webhook(
        f"Incident: {incident.get('title', 'new incident')}",
        (incident.get("description") or "")[:500],
        incident.get("severity", "warning"),
        {"incident_id": incident.get("id"), "pattern": incident.get("pattern"), "signal_count": incident.get("signal_count")},
    )
    try:
        from abyss_wave import emit_abyss_event

        emit_abyss_event("incident_clustered", {
            "incident_id": incident.get("id"),
            "pattern": incident.get("pattern"),
            "severity": incident.get("severity"),
            "signal_count": incident.get("signal_count") or 0,
            "title": str(incident.get("title") or "")[:200],
        })
    except Exception as exc:
        logger.debug("Abyss incident_clustered emit failed: %s", exc)




def list_activity(limit: int = 50, category: Optional[str] = None, since: Optional[str] = None,
                  session_id: Optional[str] = None):
    """List activity feed entries.

    Filters are ANDed: ``category``, ``since`` (ISO timestamp), ``session_id``.
    ``session_id`` rounds out the filter family the other list surfaces
    already expose (``/trace``, ``/signals``, ``/incidents``) so the UI can
    drill one session's full activity story without a separate endpoint.
    """
    _init_db()
    conn = _get_activity_conn()
    try:
        query = "SELECT * FROM activity"
        params = []

        where = []
        if since:
            where.append("timestamp >= ?")
            params.append(since)
        if category:
            where.append("category = ?")
            params.append(category)
        if session_id:
            where.append("session_id = ?")
            params.append(session_id)
        if where:
            query += " WHERE " + " AND ".join(where)

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def get_session_trace(session_id: str, limit: int = 200):
    """Get the full trace/timeline for a session."""
    _init_db()
    conn = _get_trace_conn()
    try:
        rows = conn.execute(
            """SELECT * FROM traces
              WHERE session_id = ?
              ORDER BY timestamp ASC LIMIT ?""",
            (session_id, limit)
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


# ===========================================================================
# Trace graph-node system (Raindrop-style "trajectory" visualization)
# ===========================================================================

def _parse_evt(raw):
    """Safely parse a trace event_data JSON blob into a dict."""
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except (ValueError, TypeError):
        return {}


def _iso_ms(ts: Optional[str]):
    """Return epoch milliseconds for an ISO timestamp (None if unparsable)."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts).timestamp() * 1000.0
    except (ValueError, TypeError):
        return None


def get_trace_graph(session_id: str, limit: int = 300):
    """Build a visual DAG (graph-node system) of a session's trajectory.

    Pairs ``tool_call`` start/end events (by ``tool_call_id``) into a single
    node carrying status/duration/error, groups each reasoning (``llm_call``)
    turn with the tool calls it spawned, and returns nodes + edges the UI can
    lay out as a graph instead of a flat list. Mirrors Raindrop's "visualize
    agent trajectories: every tool call, error, and recovery" model.
    """
    _init_db()
    conn = _get_trace_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM traces WHERE session_id = ? ORDER BY id ASC LIMIT ?",
            (session_id, limit),
        ).fetchall()
    finally:
        conn.close()

    tool_nodes: Dict[str, dict] = {}   # tool_call_id -> node
    tool_seq: list = []                # chronological tool key order
    llm_nodes: list = []               # llm node dicts in order
    llm_keys: list = []                # keys in order
    current_llm: Optional[str] = None  # active reasoning turn

    for row in rows:
        et = row["event_type"]
        d = _parse_evt(row["event_data"])
        tcid = str(d.get("tool_call_id") or f"tc_{row['id']}")
        if et == "tool_call":
            if tcid not in tool_nodes:
                tool_nodes[tcid] = {
                    "id": f"node_{tcid}",
                    "type": "tool",
                    "label": str(d.get("tool") or "tool_call")[:48],
                    "tool": d.get("tool"),
                    "status": "running",
                    "error_type": None,
                    "error_message": None,
                    "duration_ms": row["duration_ms"],
                    "start": row["timestamp"],
                    "end": None,
                    "parent": current_llm,
                    "result_preview": None,
                }
                tool_seq.append(tcid)
            n = tool_nodes[tcid]
            if d.get("phase") == "end":
                n["end"] = row["timestamp"]
                is_err = (d.get("status") == "error") or bool(d.get("error_type"))
                n["status"] = "error" if is_err else "ok"
                if d.get("error_type"):
                    n["error_type"] = str(d["error_type"])[:80]
                if d.get("error_message"):
                    n["error_message"] = str(d["error_message"])[:400]
                n["result_preview"] = d.get("result_preview")
                if row["duration_ms"]:
                    n["duration_ms"] = row["duration_ms"]
                else:
                    s = _iso_ms(n["start"]); e = _iso_ms(row["timestamp"])
                    if s is not None and e is not None:
                        n["duration_ms"] = int(e - s)
        elif et == "llm_call":
            key = f"llm_{row['id']}"
            llm_nodes.append({
                "id": f"node_{key}",
                "type": "llm",
                "label": str(d.get("model") or "reasoning")[:48],
                "model": d.get("model"),
                "status": "ok",
                "duration_ms": row["duration_ms"],
                "start": row["timestamp"],
                "end": row["timestamp"],
                "parent": None,
                "result_preview": d.get("result_preview"),
            })
            llm_keys.append(key)
            current_llm = key
        # session_start / session_end / memory events -> implicit context

    # Assemble node list in chronological order (llms interleaved with tools)
    nodes = [{"id": "node_root", "type": "session", "label": "session",
              "status": "ok", "duration_ms": None, "start": None, "end": None,
              "parent": None}]
    edges = []

    def _llm_id(k):
        return f"node_{k}"

    # Map tool -> parent llm (or root)
    for tcid in tool_seq:
        n = tool_nodes[tcid]
        parent = n.get("parent")
        edges.append({
            "source": _llm_id(parent) if parent and parent in llm_keys else "node_root",
            "target": n["id"],
            "type": "spawn",
        })
    for key in llm_keys:
        edges.append({"source": "node_root", "target": _llm_id(key), "type": "spawn"})

    # Interleave llm + tool nodes in acquisition order (left->right flow):
    # a reasoning node appears when it first owns a following tool node.
    llm_by_key = {n["id"][len("node_"):]: n for n in llm_nodes}
    ordered: list = [nodes[0]]
    seen: set = {nodes[0]["id"]}
    for tcid in tool_seq:
        n = tool_nodes[tcid]
        parent_key = n.get("parent")
        if parent_key and parent_key in llm_by_key and llm_by_key[parent_key]["id"] not in seen:
            ordered.append(llm_by_key[parent_key])
            seen.add(llm_by_key[parent_key]["id"])
        if n["id"] not in seen:
            ordered.append(n)
            seen.add(n["id"])
    for key in llm_keys:
        nid = f"node_{key}"
        if nid not in seen:
            ordered.append(llm_by_key[key])
            seen.add(nid)
    nodes = ordered

    ok = sum(1 for n in nodes if n["type"] == "tool" and n["status"] == "ok")
    err = sum(1 for n in nodes if n["type"] == "tool" and n["status"] == "error")
    open_ = sum(1 for n in nodes if n["type"] == "tool" and n["status"] == "running")
    llm_c = sum(1 for n in nodes if n["type"] == "llm")

    return {
        "session_id": session_id,
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "tools": ok + err + open_,
            "ok": ok, "errors": err, "open": open_, "llms": llm_c,
        },
        "generated_at": datetime.now().isoformat(),
    }


def get_trace_timeline(session_id: str, limit: int = 300):
    """Build per-lane timeline data for one session (single-agent trajectory).

    Positions each event as a horizontal bar relative to session start so the
    UI can render lanes (reasoning / tools / failures) — each agent's session
    read as a timeline, Raindrop-style.
    """
    _init_db()
    conn = _get_trace_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM traces WHERE session_id = ? ORDER BY id ASC LIMIT ?",
            (session_id, limit),
        ).fetchall()
    finally:
        conn.close()

    tool_nodes: Dict[str, dict] = {}
    llm_nodes: list = []
    base = None
    for row in rows:
        d = _parse_evt(row["event_data"])
        ts_ms = _iso_ms(row["timestamp"])
        if ts_ms is not None:
            base = ts_ms if base is None else min(base, ts_ms)
        tcid = str(d.get("tool_call_id") or f"tc_{row['id']}")
        if row["event_type"] == "tool_call":
            if tcid not in tool_nodes:
                tool_nodes[tcid] = {
                    "id": f"node_{tcid}", "label": str(d.get("tool") or "tool")[:48],
                    "tool": d.get("tool"), "status": "running",
                    "error_type": None, "error_message": None,
                    "start_ms": ts_ms, "end_ms": None, "duration_ms": row["duration_ms"],
                }
            n = tool_nodes[tcid]
            if d.get("phase") == "end":
                n["end_ms"] = ts_ms
                n["status"] = "error" if d.get("status") == "error" or d.get("error_type") else "ok"
                if d.get("error_type"):
                    n["error_type"] = str(d["error_type"])[:80]
                if d.get("error_message"):
                    n["error_message"] = str(d["error_message"])[:400]
                if row["duration_ms"]:
                    n["duration_ms"] = row["duration_ms"]
                elif n["start_ms"] is not None and ts_ms is not None:
                    n["duration_ms"] = int(ts_ms - n["start_ms"])
            else:
                if n["start_ms"] is None:
                    n["start_ms"] = ts_ms
        elif row["event_type"] == "llm_call":
            llm_nodes.append({
                "id": f"node_llm_{row['id']}", "label": str(d.get("model") or "reasoning")[:48],
                "model": d.get("model"), "status": "ok",
                "start_ms": ts_ms, "end_ms": ts_ms, "duration_ms": row["duration_ms"],
            })

    if base is None:
        base = 0

    def _off(ms):
        return int((ms - base)) if ms is not None else 0

    reasoning = [{
        "id": n["id"], "label": n["label"], "model": n["model"],
        "start_ms": _off(n["start_ms"]), "end_ms": _off(n["end_ms"]),
        "duration_ms": n["duration_ms"], "status": n["status"],
    } for n in llm_nodes]
    tools = [{
        "id": n["id"], "label": n["label"], "tool": n["tool"],
        "start_ms": _off(n["start_ms"]), "end_ms": _off(n["end_ms"]),
        "duration_ms": n["duration_ms"], "status": n["status"],
        "error_type": n["error_type"], "error_message": n["error_message"],
    } for n in tool_nodes.values()]
    failures = [t for t in tools if t["status"] == "error"]

    return {
        "session_id": session_id,
        "base": base,
        "total_ms": max((_off(n["end_ms"]) for n in llm_nodes + list(tool_nodes.values()) if n.get("end_ms") is not None), default=1),
        "lanes": [
            {"id": "reasoning", "label": "Reasoning", "nodes": reasoning},
            {"id": "tools", "label": "Tools", "nodes": tools},
            {"id": "failures", "label": "Failures", "nodes": failures},
        ],
        "generated_at": datetime.now().isoformat(),
    }


def get_agents_overview(limit: int = 60):
    """Per-session (per-agent) overview for an 'every agent as a timeline' view.

    Each session == one agent conversation. Lightweight aggregate row (start,
    duration, event/error counts, health) rendered as its own timeline lane on
    a shared time axis.
    """
    _init_db()
    # Per-session aggregate over the trace DB. One connection for the whole
    # function and ONE aggregate query (the per-session error count used to
    # open a second query inside the loop — connection churn + N+1 on a
    # UI-polled endpoint; now folded into the GROUP BY via SUM(CASE ...)).
    conn = _get_trace_conn()
    try:
        # Single aggregate carries the per-session error count too: the old
        # loop opened a second query PER session (N+1 on a UI-polled
        # endpoint). Both spacing variants of the error status are matched,
        # exactly like get_recent_sessions — never the error_type key.
        aggs = conn.execute(
            """SELECT session_id,
                      COUNT(*) AS event_count,
                      SUM(CASE WHEN event_type='llm_call' THEN 1 ELSE 0 END) AS llm_count,
                      SUM(CASE WHEN event_type='tool_call'
                                AND (event_data LIKE '%"status":"error"%'
                                     OR event_data LIKE '%"status": "error"%')
                          THEN 1 ELSE 0 END) AS error_count,
                      MIN(timestamp) AS start_ts,
                      MAX(timestamp) AS end_ts
               FROM traces GROUP BY session_id
               ORDER BY MAX(timestamp) DESC LIMIT ?""",
            (limit,),
        ).fetchall()

        out = []
        for a in aggs:
            sid = a["session_id"]
            errors = int(a["error_count"] or 0)
            start_ms = _iso_ms(a["start_ts"]) or 0
            end_ms = _iso_ms(a["end_ts"]) or start_ms
            out.append({
                "session_id": sid,
                "start": start_ms,
                "end": end_ms,
                "duration_ms": int(max(end_ms - start_ms, 1)),
                "event_count": a["event_count"],
                "llm_count": a["llm_count"] or 0,
                "error_count": errors,
                "has_errors": errors > 0,
            })
        return {"agents": out, "generated_at": datetime.now().isoformat()}
    finally:
        conn.close()





def get_graph_data(limit: int = 200):
    """Build a graph of entities (sessions, tools, memories) and their connections.

    Returns nodes and edges suitable for a node-graph visualization.
    """
    _init_db()
    nodes = []
    edges = []

    conn = _get_activity_conn()

    # Sessions
    sessions = conn.execute(
        """SELECT DISTINCT session_id, MIN(timestamp) as first_ts, MAX(timestamp) as last_ts, COUNT(*) as count
           FROM activity WHERE session_id IS NOT NULL
           GROUP BY session_id
           ORDER BY last_ts DESC LIMIT ?""",
        (limit,)
    ).fetchall()

    for s in sessions:
        nodes.append({
            "id": f"session:{s['session_id']}",
            "type": "session",
            "label": s["session_id"][:8] if s["session_id"] else "unknown",
            "data": {
                "session_id": s["session_id"],
                "first_ts": s["first_ts"],
                "last_ts": s["last_ts"],
                "activity_count": s["count"]
            }
        })

    # Tools
    tools = conn.execute(
        """SELECT DISTINCT tool_name, COUNT(*) as count, MIN(timestamp) as first_ts
           FROM activity WHERE tool_name IS NOT NULL
           GROUP BY tool_name
           ORDER BY count DESC LIMIT ?""",
        (limit,)
    ).fetchall()

    for t in tools:
        nodes.append({
            "id": f"tool:{t['tool_name']}",
            "type": "tool",
            "label": t["tool_name"],
            "data": {
                "tool_name": t["tool_name"],
                "usage_count": t["count"],
                "first_used": t["first_ts"]
            }
        })

    # Categories
    cats = conn.execute(
        """SELECT DISTINCT category, COUNT(*) as count
           FROM activity WHERE category IS NOT NULL
           GROUP BY category ORDER BY count DESC LIMIT ?""",
        (limit,)
    ).fetchall()

    for c in cats:
        nodes.append({
            "id": f"category:{c['category']}",
            "type": "category",
            "label": c["category"],
            "data": {"count": c["count"]}
        })

    # Edges: session -> tool (usage)
    edges_data = conn.execute(
        """SELECT DISTINCT session_id, tool_name, COUNT(*) as count
           FROM activity WHERE session_id IS NOT NULL AND tool_name IS NOT NULL
           GROUP BY session_id, tool_name LIMIT ?""",
        (limit * 2,)
    ).fetchall()

    for e in edges_data:
        edges.append({
            "source": f"session:{e['session_id']}",
            "target": f"tool:{e['tool_name']}",
            "type": "usage",
            "weight": e["count"]
        })

    # Edges: session -> category
    edge_cats = conn.execute(
        """SELECT DISTINCT session_id, category, COUNT(*) as count
           FROM activity WHERE session_id IS NOT NULL AND category IS NOT NULL
           GROUP BY session_id, category LIMIT ?""",
        (limit * 2,)
    ).fetchall()

    for e in edge_cats:
        edges.append({
            "source": f"session:{e['session_id']}",
            "target": f"category:{e['category']}",
            "type": "category",
            "weight": e["count"]
        })

    # Also pull from Hermes state.db for memory nodes (must stay inside the
    # activity conn's lifetime — the memory -> session keyword edges query
    # activity via conn, so closing conn first is a use-after-close).
    state_db = os.path.join(PROFILE_HOME, "state.db")
    if os.path.exists(state_db):
        sconn = None
        try:
            sconn = sqlite3.connect(state_db)
            sconn.row_factory = sqlite3.Row

            # Memories
            mems = sconn.execute(
                "SELECT id, content, category FROM memories ORDER BY created_at DESC LIMIT ?"
                , (limit,)).fetchall()

            for m in mems:
                nodes.append({
                    "id": f"memory:{m['id']}",
                    "type": "memory",
                    "label": (m["content"] or "")[:30],
                    "data": {
                        "memory_id": m["id"],
                        "content": m["content"],
                        "category": m["category"]
                    }
                })

                # Connect memories to activity (by keyword matching)
                content_lower = (m["content"] or "").lower()
                if content_lower:
                    act_matches = conn.execute(
                        """SELECT DISTINCT session_id FROM activity
                           WHERE LOWER(action) LIKE ? OR LOWER(description) LIKE ?
                           LIMIT 5""",
                        (f"%{content_lower[:20]}%", f"%{content_lower[:20]}%")
                    ).fetchall()

                    for match in act_matches:
                        sid = match["session_id"]
                        if sid:
                            edges.append({
                                "source": f"session:{sid}",
                                "target": f"memory:{m['id']}",
                                "type": "reference",
                                "weight": 1
                            })

        except Exception as e:
            logger.debug("Graph: state.db read failed: %s", e)
        finally:
            if sconn is not None:
                try:
                    sconn.close()
                except Exception:
                    pass

    try:
        conn.close()
    except Exception:
        pass

    return {
        "nodes": nodes,
        "edges": edges,
        "generated_at": datetime.now().isoformat()
    }


# ---------------------------------------------------------------------------
# Unified API handler — reused by plugin_api.py's REST layer
# ---------------------------------------------------------------------------

def _coerce_body(body) -> dict:
    """Parse a POST body, tolerating double-encoded JSON strings.

    The desktop IPC layer unconditionally ``JSON.stringify``s request bodies,
    so a client that pre-stringifies sends a JSON-encoded *string* here.
    Decode it again so handlers always get a dict. Already-parsed dicts,
    bytes, and empty bodies are handled without raising — a malformed body
    degrades to {} so the dispatcher returns a clean 400/error instead of
    crashing into a 500.
    """
    if body is None or body == "":
        return {}
    if isinstance(body, dict):
        return body
    if isinstance(body, (bytes, bytearray)):
        try:
            body = body.decode("utf-8")
        except Exception:
            return {}
    if not isinstance(body, str):
        return {}
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return {}
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            data = {}
    return data if isinstance(data, dict) else {}


class _BadRequest(Exception):
    """Raised when a client-supplied parameter is malformed (HTTP 400)."""


def _int_param(params: dict, key: str, default: int) -> int:
    """Parse an integer request param.

    Missing/empty values fall back to ``default``; a non-numeric value raises
    ``_BadRequest`` so the dispatcher returns a clean 400 (not a 500).
    """
    raw = params.get(key, default)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise _BadRequest(f"invalid integer for '{key}'")


def handle_request(method: str, path: str, params: dict = None, body: str = None):
    """Handle API requests. Called by both the REST layer and plugin_api.py."""
    params = params or {}

    try:
        if path == "/activity" and method == "GET":
            return list_activity(
                limit=_int_param(params, "limit", 50),
                category=params.get("category"),
                since=params.get("since"),
                session_id=params.get("session_id"),
            )

        elif path == "/activity" and method == "POST":
            data = _coerce_body(body)
            result = _add_activity(
                action=data.get("action", ""),
                description=data.get("description", ""),
                category=data.get("category", "general"),
                status=data.get("status", "completed"),
                metadata=data.get("metadata"),
                session_id=data.get("session_id"),
                tool_name=data.get("tool_name"),
                args=data.get("args"),
            )
            return result

        elif path == "/calendar" and method == "GET":
            # Default start = beginning of today. A naive "now" default was
            # doubly broken: (1) the REST layer passes an EXPLICIT None for
            # missing params, so `params.get("start", now)` returned None and
            # the calendar never included ANY activity rows through the API
            # (only cron entries); (2) a "now" start would exclude activities
            # recorded seconds earlier. Start-of-day is what a calendar view
            # semantically wants: everything since today + the next 7 days.
            start = params.get("start") or datetime.now().replace(
                hour=0, minute=0, second=0, microsecond=0
            ).isoformat()
            end = params.get("end") or (datetime.now() + timedelta(days=7)).isoformat()
            return list_calendar(start, end)

        elif path == "/search" and method == "GET":
            query = params.get("q", "")
            limit = _int_param(params, "limit", 20)
            return global_search(query, limit)

        elif path == "/stats" and method == "GET":
            return get_stats()

        elif path == "/health" and method == "GET":
            return get_health()

        elif path == "/trends" and method == "GET":
            days = _int_param(params, "days", 7)
            bucket = params.get("bucket", "day") or "day"
            return get_trends(days=days, bucket=bucket)

        elif path == "/failures" and method == "GET":
            limit = _int_param(params, "limit", 15)
            return get_failures(limit=limit)

        elif path == "/performance" and method == "GET":
            days = _int_param(params, "days", 7)
            limit = _int_param(params, "limit", 20)
            return get_performance(days=days, limit=limit)

        elif path == "/export" and method == "GET":
            return export_data()

        elif path == "/status" and method == "GET":
            return get_status()

        elif path == "/trace/graph" and method == "GET":
            return get_trace_graph(params.get("session_id", ""), limit=_int_param(params, "limit", 300))

        elif path == "/trace/timeline" and method == "GET":
            return get_trace_timeline(params.get("session_id", ""), limit=_int_param(params, "limit", 300))

        elif path == "/trace/agents" and method == "GET":
            return get_agents_overview(limit=_int_param(params, "limit", 60))

        elif path == "/trace" and method == "GET":
            session_id = params.get("session_id", "")
            if session_id:
                return get_session_trace(session_id, limit=_int_param(params, "limit", 200))
            else:
                # List recent sessions
                return get_recent_sessions(limit=_int_param(params, "limit", 20))

        elif path == "/graph" and method == "GET":
            limit = _int_param(params, "limit", 200)
            return get_graph_data(limit=limit)

        elif path == "/signals" and method == "GET":
            limit = _int_param(params, "limit", 50)
            session_filter = params.get("session_id")
            type_filter = params.get("type")
            severity_filter = params.get("severity")
            state_filter = params.get("state") or ""
            # Triage filters (Aug-2026): at 4,400+ open signals a bare
            # latest-N list can't answer "show me unresolved errors of type
            # X". `type` / `severity` match exact values; `state` is one of:
            #   all         — no resolved/acknowledged filtering (default)
            #   open        — resolved = 0 (the triage backlog)
            #   unack       — resolved = 0 AND acknowledged = 0
            # Unknown values raise _BadRequest -> clean 400, never silent.
            if state_filter and state_filter not in ("all", "open", "unack"):
                raise _BadRequest("invalid value for 'state' (use: all, open, unack)")
            # The signals table has NO tool_name column (documented gap: the
            # UI had to guess which tool produced a signal, or join activity
            # client-side). Enrich each row with the source tool + action from
            # the linked activity row so the feed shows "terminal -> timeout"
            # instead of a bare timeout signal. LEFT JOIN keeps
            # self-diagnostic signals (activity_id NULL) intact.
            query = ("SELECT s.*, a.tool_name AS tool_name, a.action AS tool_action "
                     "FROM signals s LEFT JOIN activity a ON a.id = s.activity_id")
            query_params = []
            clauses = []
            if session_filter:
                clauses.append("s.session_id = ?")
                query_params.append(session_filter)
            if type_filter:
                clauses.append("s.signal_type = ?")
                query_params.append(type_filter)
            if severity_filter:
                clauses.append("s.severity = ?")
                query_params.append(severity_filter)
            if state_filter == "open":
                clauses.append("s.resolved = 0")
            elif state_filter == "unack":
                clauses.append("s.resolved = 0 AND s.acknowledged = 0")
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            query += " ORDER BY s.timestamp DESC LIMIT ?"
            query_params.append(limit)
            conn = _get_activity_conn()
            try:
                rows = conn.execute(query, query_params).fetchall()
            finally:
                conn.close()
            return [dict(row) for row in rows]

        elif path == "/incidents" and method == "GET":
            limit = _int_param(params, "limit", 50)
            status_filter = params.get("status")
            severity_filter = params.get("severity")
            open_only = params.get("open") in (1, True, "1", "true", "True")
            # Triage filter parity with /signals: severity narrows to exact
            # value; open=1 keeps only status='open' rows (the actionable
            # backlog — resolved/closed incidents stay out of the way).
            if open_only and status_filter not in (None, "", "open"):
                raise _BadRequest("'open' cannot be combined with a different 'status'")
            query = "SELECT * FROM incidents"
            query_params = []
            clauses = []
            if status_filter:
                query_params.append(status_filter)
                clauses.append("status = ?")
            if severity_filter:
                query_params.append(severity_filter)
                clauses.append("severity = ?")
            if open_only:
                clauses.append("status = 'open'")
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            query += " ORDER BY timestamp DESC LIMIT ?"
            query_params.append(limit)
            conn = _get_activity_conn()
            try:
                rows = conn.execute(query, query_params).fetchall()
            finally:
                conn.close()
            return [dict(row) for row in rows]

        elif path == "/signals/self-diagnostic" and method == "POST":
            data = _coerce_body(body)
            sid = _record_self_diagnostic(
                session_id=data.get("session_id", ""),
                capability=data.get("capability", "unknown"),
                gap=data.get("gap", ""),
                severity=data.get("severity", "warning"),
            )
            return {"id": sid, "status": "recorded"}

        elif path == "/signals/resolve-bulk" and method == "POST":
            data = _coerce_body(body)
            return _resolve_signals_bulk(
                session_prefix=(data.get("session_prefix") or None),
                signal_type=(data.get("signal_type") or None),
                older_than_days=_int_param(data, "older_than_days", 0) or None,
                close_empty_incidents=bool(data.get("close_empty_incidents", False)),
            )

        elif path == "/incidents/cluster" and method == "POST":
            new_incidents = _cluster_incidents()
            return {"incidents_created": new_incidents}

        elif path == "/prune" and method == "POST":
            data = _coerce_body(body)
            days = _int_param(data, "days", 30)
            deleted = _prune_data(days)
            return {"deleted": deleted, "status": "ok"}

        elif path.startswith("/signals/") and path.endswith("/resolve-agent") and method == "POST":
            try:
                signal_id = int(path.split("/")[2])
            except (IndexError, ValueError):
                return {"error": "Invalid signal id", "code": 400}
            return _dispatch_resolution("signals", signal_id)

        elif path.startswith("/incidents/") and path.endswith("/resolve-agent") and method == "POST":
            try:
                incident_id = int(path.split("/")[2])
            except (IndexError, ValueError):
                return {"error": "Invalid incident id", "code": 400}
            return _dispatch_resolution("incidents", incident_id)

        elif path == "/doctor/run" and method == "POST":
            return _dispatch_doctor()

        elif path == "/doctor/report" and method == "GET":
            return _doctor_report(params.get("report_id", ""))

        elif path == "/doctor/log" and method == "GET":
            return _doctor_log(params.get("report_id", ""))

        elif path == "/doctor/capture" and method == "GET":
            # Deterministic multi-store capture liveness (no agent dispatch):
            # freshness + fragmentation verdict across every known
            # abyss-data/activity.db (active profile, home fallback, siblings).
            from abyss_doctor import _capture_status

            try:
                staleness = float(params.get("max_age_hours") or 6.0)
            except (TypeError, ValueError):
                return {"error": "max_age_hours must be a number", "code": 400}
            if staleness <= 0:
                return {"error": "max_age_hours must be > 0", "code": 400}
            return _capture_status(staleness_hours=staleness)

        elif path == "/doctor/last" and method == "GET":
            return _doctor_last()

        elif path == "/benchmark/run" and method == "POST":
            return _run_benchmark()

        elif path == "/prune-resolutions" and method == "POST":
            data = _coerce_body(body)
            return _prune_resolutions(
                retention_days=_int_param(data, "days", 30),
                keep_recent=_int_param(data, "keep_recent", 20),
            )

        elif path == "/doctor/approve" and method == "POST":
            data = _coerce_body(body)
            return _dispatch_doctor_apply(data.get("report_id", ""), data)

        elif path.startswith("/signals/") and path.endswith("/acknowledge") and method == "POST":
            try:
                signal_id = int(path.split("/")[2])
            except (IndexError, ValueError):
                return {"error": "Invalid signal id", "code": 400}
            row = _acknowledge_signal(signal_id)
            if row is None:
                return {"error": f"Signal {signal_id} not found", "code": 404}
            return {"signal": row, "status": "acknowledged"}

        elif path.startswith("/signals/") and path.endswith("/resolve") and method == "POST":
            try:
                signal_id = int(path.split("/")[2])
            except (IndexError, ValueError):
                return {"error": "Invalid signal id", "code": 400}
            row = _resolve_signal(signal_id)
            if row is None:
                return {"error": f"Signal {signal_id} not found", "code": 404}
            return {"signal": row, "status": "resolved"}

        elif path.startswith("/incidents/") and method == "POST":
            # /incidents/<id>/acknowledge | /incidents/<id>/resolve | /incidents/<id>/reopen
            parts = path.strip("/").split("/")
            if len(parts) != 3:
                return {"error": "Unknown endpoint", "code": 404}
            try:
                incident_id = int(parts[1])
            except ValueError:
                return {"error": "Invalid incident id", "code": 400}
            action = parts[2]
            target = {
                "acknowledge": "acknowledged",
                "resolve": "resolved",
                "reopen": "open",
                "close": "closed",
            }.get(action)
            if target is None:
                return {"error": f"Unknown incident action: {action}", "code": 404}
            row = _update_incident_status(incident_id, target)
            if row is None:
                return {"error": f"Incident {incident_id} not found", "code": 404}
            return {"incident": row, "status": target}

        elif path.startswith("/wave"):
            try:
                from abyss_wave import wave_handle

                return wave_handle(method, path, params, body)
            except Exception as exc:
                import traceback
                return {"error": str(exc), "traceback": traceback.format_exc(), "code": 500}

        else:
            return {"error": f"Unknown endpoint: {method} {path}", "code": 404}

    except _BadRequest as e:
        return {"error": str(e), "code": 400}
    except Exception as e:
        import traceback
        return {
            "error": str(e),
            "traceback": traceback.format_exc(),
            "code": 500
        }


# ---------------------------------------------------------------------------
# Additional query functions
# ---------------------------------------------------------------------------

def add_activity(action, description="", category="general",
                 status="completed", metadata=None, session_id=None,
                 tool_name=None, args=None):
    """Public API to add an activity entry (alias for _add_activity)."""
    return _add_activity(
        action=action, description=description, category=category,
        status=status, metadata=metadata, session_id=session_id,
        tool_name=tool_name, args=args
    )


def list_calendar(start_date: str, end_date: str):
    """List scheduled tasks (cron jobs) for a date range."""
    results = []

    # Read cron jobs from Hermes cron directory. The live store is a single
    # jobs.json containing a `jobs:` array (each job carries its own schedule
    # object + next_run_at). Older layouts stored one <jobid>.json per job
    # with top-level schedule/next_run keys — support both so the calendar
    # never renders a single misparsed "jobs" chip for the aggregate file.
    cron_dir = Path(PROFILE_HOME) / "cron"
    cron_jobs: list[dict] = []
    if cron_dir.exists():
        jobs_file = cron_dir / "jobs.json"
        if jobs_file.exists():
            try:
                data = json.loads(jobs_file.read_text(encoding="utf-8"))
                raw = data.get("jobs") if isinstance(data, dict) else data
                if isinstance(raw, list):
                    cron_jobs.extend(j for j in raw if isinstance(j, dict) and j.get("id"))
            except Exception:
                logger.debug("abyss: failed to read cron jobs store %s", jobs_file)
        # Legacy layout: individual <jobid>.json files (skip the aggregate store).
        for f in sorted(cron_dir.glob("*.json")):
            if f.name == "jobs.json":
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    data.setdefault("id", f.stem)
                    cron_jobs.append(data)
            except Exception:
                continue

    # Map job id -> most recent session id recorded in activity
    # (cron sessions carry session_id = "cron_<jobid>_<YYYYMMDD_HHMMSS>"), so
    # calendar chips for scheduled jobs get the DESIGN.md `trace ›` drill the
    # UI already renders when task.session_id is present.
    latest_session: dict[str, tuple[str, str]] = {}
    conn = None
    try:
        _init_db()
        conn = _get_activity_conn()
        try:
            sid_rows = conn.execute(
                "SELECT session_id, MAX(timestamp) AS ts FROM activity "
                "WHERE session_id LIKE 'cron\\_%' ESCAPE '\\' GROUP BY session_id"
            ).fetchall()
        except Exception:
            sid_rows = []
        for r in sid_rows:
            sid = r["session_id"] or ""
            parts = sid.split("_")
            if len(parts) >= 2 and parts[1]:
                cur = latest_session.get(parts[1])
                if cur is None or r["ts"] > cur[1]:
                    latest_session[parts[1]] = (sid, r["ts"])
    finally:
        if conn is not None:
            conn.close()

    for job in cron_jobs:
        jid = job.get("id", "")
        schedule = job.get("schedule")
        if isinstance(schedule, dict):
            schedule_display = schedule.get("display") or schedule.get("kind") or ""
        else:
            schedule_display = job.get("schedule_display") or str(schedule or "")
        prompt = job.get("prompt") or ""
        results.append({
            "id": jid,
            "title": job.get("name") or jid,
            "description": prompt[:100] + "..." if len(prompt) > 100 else prompt,
            "schedule": schedule_display,
            "next_run": job.get("next_run_at") or job.get("next_run") or "",
            "enabled": job.get("enabled", True),
            "deliver": job.get("deliver", "origin"),
            "category": "cron",
            # trace-drill affordance: most recent Abyss-tracked run of this job
            "session_id": latest_session.get(jid, ("", ""))[0],
            "last_run": job.get("last_run_at") or "",
        })

    # Also include activities within the date range
    _init_db()
    conn = _get_activity_conn()
    rows = conn.execute(
        "SELECT * FROM activity WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp ASC",
        (start_date, end_date)
    ).fetchall()
    conn.close()

    for row in rows:
        results.append({
            "id": f"activity-{row['id']}",
            "title": row["action"],
            "description": row["description"],
            "timestamp": row["timestamp"],
            "category": row["category"],
            "status": row["status"],
            # DESIGN.md: calendar chips carry a `trace ›` drill when the row
            # includes a session_id — the affordance lights up here. tool_name
            # lets the UI show the same per-tool type colors as the activity feed.
            "session_id": row["session_id"],
            "tool_name": row["tool_name"],
        })

    return results


def global_search(query: str, limit: int = 20):
    """Search across memories, documents, and tasks."""
    results = []
    query_lower = query.lower()

    # 1. Search activity feed
    _init_db()
    conn = _get_activity_conn()
    rows = conn.execute(
        "SELECT * FROM activity WHERE LOWER(action) LIKE ? OR LOWER(description) LIKE ? ORDER BY timestamp DESC LIMIT ?",
        (f"%{query_lower}%", f"%{query_lower}%", limit)
    ).fetchall()
    conn.close()

    for row in rows:
        results.append({
            "source": "activity",
            "id": row["id"],
            "title": row["action"],
            "description": row["description"],
            "timestamp": row["timestamp"],
            "category": row["category"],
            "relevance": 1.0,
        })

    # 2. Search Hermes state.db — guaranteed close of each DB handle even on exception.
    state_db = os.path.join(PROFILE_HOME, "state.db")
    if os.path.exists(state_db):
        sconn = None
        try:
            sconn = sqlite3.connect(state_db)
            sconn.row_factory = sqlite3.Row

            # Search sessions
            try:
                session_rows = sconn.execute(
                    "SELECT session_id, title, created_at, source FROM sessions WHERE LOWER(title) LIKE ? ORDER BY created_at DESC LIMIT ?",
                    (f"%{query_lower}%", limit // 3)
                ).fetchall()
                for row in session_rows:
                    results.append({
                        "source": "sessions",
                        "id": row["session_id"],
                        "title": row["title"],
                        "description": f"Session from {row['source']}",
                        "timestamp": row["created_at"],
                        "category": "session",
                        "relevance": 0.9,
                    })
            except Exception:
                pass

            # Search memories
            try:
                mem_rows = sconn.execute(
                    "SELECT id, content, created_at, category FROM memories WHERE LOWER(content) LIKE ? ORDER BY created_at DESC LIMIT ?",
                    (f"%{query_lower}%", limit // 3)
                ).fetchall()
                for row in mem_rows:
                    results.append({
                        "source": "memory",
                        "id": row["id"],
                        "title": (row["content"] or "")[:100],
                        "description": row["content"],
                        "timestamp": row["created_at"],
                        "category": row["category"] or "memory",
                        "relevance": 0.8,
                    })
            except Exception:
                pass

            # Search kanban.db for tasks
            kanban_db = os.path.join(PROFILE_HOME, "kanban.db")
            if os.path.exists(kanban_db):
                kconn = None
                try:
                    kconn = sqlite3.connect(kanban_db)
                    kconn.row_factory = sqlite3.Row
                    task_rows = kconn.execute(
                        "SELECT id, title, description, status, board, created_at, updated_at FROM tasks WHERE LOWER(title) LIKE ? OR LOWER(description) LIKE ? ORDER BY updated_at DESC LIMIT ?",
                        (f"%{query_lower}%", f"%{query_lower}%", limit // 3)
                    ).fetchall()
                    for row in task_rows:
                        results.append({
                            "source": "kanban",
                            "id": row["id"],
                            "title": row["title"],
                            "description": (row["description"] or "")[:200],
                            "timestamp": row["updated_at"],
                            "category": "task",
                            "status": row["status"],
                            "relevance": 0.7,
                        })
                except Exception:
                    pass
                finally:
                    if kconn is not None:
                        try:
                            kconn.close()
                        except Exception:
                            pass

        except Exception:
            pass
        finally:
            if sconn is not None:
                try:
                    sconn.close()
                except Exception:
                    pass

    results.sort(key=lambda x: (x.get("relevance", 0), x.get("timestamp", "")), reverse=True)
    return results[:limit]


def get_stats():
    """Get dashboard summary statistics."""
    from collections import Counter

    _init_db()
    conn = _get_activity_conn()

    total = conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0]
    categories = dict(Counter(r[0] for r in conn.execute("SELECT category FROM activity").fetchall()))
    recent = [dict(r) for r in conn.execute("SELECT * FROM activity ORDER BY timestamp DESC LIMIT 5").fetchall()]

    # Error-rate + health metrics
    errors = conn.execute("SELECT COUNT(*) FROM activity WHERE status = 'error'").fetchone()[0]
    tools = dict(Counter(r[0] for r in conn.execute(
        "SELECT tool_name FROM activity WHERE tool_name IS NOT NULL AND tool_name != ''").fetchall()))
    top_tools = [{"name": k, "count": v} for k, v in sorted(tools.items(), key=lambda kv: kv[1], reverse=True)[:10]]

    # Signals & incidents health
    signal_total = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    signal_open = conn.execute("SELECT COUNT(*) FROM signals WHERE resolved = 0").fetchone()[0]
    incident_total = conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
    incident_open = conn.execute("SELECT COUNT(*) FROM incidents WHERE status IN ('open', 'acknowledged')").fetchone()[0]
    signal_types = dict(Counter(r[0] for r in conn.execute("SELECT signal_type FROM signals").fetchall()))
    top_signals = [{"type": k, "count": v} for k, v in sorted(signal_types.items(), key=lambda kv: kv[1], reverse=True)[:10]]

    # 24h window
    day_ago = (datetime.now() - timedelta(days=1)).isoformat()
    activity_24h = conn.execute(
        "SELECT COUNT(*) FROM activity WHERE timestamp >= ?", (day_ago,)
    ).fetchone()[0]
    sessions_24h = conn.execute(
        "SELECT COUNT(DISTINCT session_id) FROM activity WHERE timestamp >= ? AND session_id IS NOT NULL",
        (day_ago,),
    ).fetchone()[0]
    conn.close()

    cron_count = 0
    cron_dir = Path(PROFILE_HOME) / "cron"
    if cron_dir.exists():
        cron_count = len(list(cron_dir.iterdir()))

    return {
        "total_activities": total,
        "categories": categories,
        "recent_activities": recent,
        "cron_jobs": cron_count,
        "plugin_data_path": str(PLUGIN_DATA),
        "errors": errors,
        "error_rate": round(errors / total, 4) if total else 0.0,
        "top_tools": top_tools,
        "signals_total": signal_total,
        "signals_open": signal_open,
        "incidents_total": incident_total,
        "incidents_open": incident_open,
        "top_signals": top_signals,
        "activity_24h": activity_24h,
        "sessions_24h": sessions_24h,
        "generated_at": datetime.now().isoformat(),
    }


def get_recent_sessions(limit: int = 20):
    """List recent sessions from activity data."""
    _init_db()
    conn = _get_activity_conn()
    try:
        rows = conn.execute(
            """SELECT DISTINCT session_id, MIN(timestamp) as first_ts,
                      MAX(timestamp) as last_ts, COUNT(*) as count
               FROM activity WHERE session_id IS NOT NULL
               GROUP BY session_id
               ORDER BY last_ts DESC LIMIT ?""",
            (limit,)
        ).fetchall()
    finally:
        conn.close()

    sessions = []
    for row in rows:
        sessions.append({
            "session_id": row["session_id"],
            "first_ts": row["first_ts"],
            "last_ts": row["last_ts"],
            "activity_count": row["count"],
            "traces": [],
        })

    # Attach trace-health aggregates (error / reasoning counts) per session so
    # the list view can show health at a glance without a second round-trip.
    tconn = _get_trace_conn()
    try:
        agg_rows = tconn.execute(
            """SELECT session_id,
                      SUM(CASE WHEN event_type='llm_call' THEN 1 ELSE 0 END) AS llm_count,
                      COUNT(*) AS trace_count,
                      SUM(CASE WHEN event_type='tool_call'
                                AND (event_data LIKE '%"status":"error"%'
                                     OR event_data LIKE '%"status": "error"%')
                          THEN 1 ELSE 0 END) AS error_count
               FROM traces WHERE session_id IS NOT NULL GROUP BY session_id""",
        ).fetchall()
        agg = {r["session_id"]: r for r in agg_rows}
        for session in sessions:
            sid = session["session_id"]
            a = agg.get(sid)
            session["llm_count"] = (a["llm_count"] or 0) if a else 0
            session["trace_count"] = (a["trace_count"] or 0) if a else 0
            session["error_count"] = (a["error_count"] or 0) if a else 0
            session["has_errors"] = session["error_count"] > 0

        # Add trace data for each session. Batched into ONE query instead of
        # N+1 per-session round-trips (/trace/agents?limit=60 used to open 61
        # trace queries per poll). The per-session LIMIT 100 contract is
        # preserved by a window function; both spacing variants of the error
        # status are matched exactly as in the aggregate above.
        if sessions:
            sids = [s["session_id"] for s in sessions if s["session_id"]]
            if sids:
                placeholders = ", ".join("?" * len(sids))
                trace_rows = tconn.execute(
                    f"""SELECT id, session_id, event_type, event_data,
                               timestamp, depth, parent_id, duration_ms
                        FROM (
                            SELECT id, session_id, event_type, event_data,
                                   timestamp, depth, parent_id, duration_ms,
                                   ROW_NUMBER() OVER (
                                       PARTITION BY session_id
                                       ORDER BY timestamp ASC
                                   ) AS _rn
                            FROM traces
                            WHERE session_id IN ({placeholders})
                        ) WHERE _rn <= 100
                        ORDER BY session_id, timestamp ASC""",
                    sids,
                ).fetchall()
                per_session: Dict[str, list] = {}
                for t in trace_rows:
                    per_session.setdefault(t["session_id"], []).append(dict(t))
                for session in sessions:
                    session["traces"] = per_session.get(session["session_id"], [])
    finally:
        tconn.close()

    return sessions


# ---------------------------------------------------------------------------
# Unified API handler — used by plugin_api.py REST layer
# ---------------------------------------------------------------------------


def register(ctx) -> None:
    """Plugin entrypoint — register hooks for automatic activity logging."""
    # Initialize databases on load
    _init_db()

    # Register hooks for automatic activity recording
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("post_llm_call", _on_post_llm_call)
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("on_session_end", _on_session_end)

    # August 2026 plugin-interface expansion (#64182): event bus, streaming
    # hooks, API telemetry, subagents, approvals, platform events, commands,
    # skills, config bridge, redaction registry, ownership ledger.
    try:
        from abyss_wave import wave_register

        wave_register(ctx)
    except Exception as exc:
        logger.error("Abyss wave registration failed: %s", exc)

    # Auto-cluster any new signals and prune old data on startup
    try:
        _cluster_incidents(alert=False)
        try:
            from abyss_wave import SETTINGS

            retention = int(SETTINGS.get("retention_days", 30) or 30)
        except Exception:
            retention = int(os.environ.get("ABYSS_RETENTION_DAYS", "30") or 30)
        _prune_data(days=retention)
    except Exception as exc:
        logger.debug("Abyss startup maintenance skipped: %s", exc)

    # Register a slash command for manual activity queries
    ctx.register_command(
        "abyss",
        handler=_handle_slash,
        description="Query Abyss observability data",
    )

    # Register the named sub-commands + their slash aliases declared in
    # plugin.yaml `commands:`. The runtime ignores that manifest field, so
    # they must be registered explicitly to be invocable (e.g. /activity,
    # /a-stats, /a-trace, and the dotted /abyss.recent forms).
    _SUBCOMMAND_ALIASES = {
        "abyss.recent": "recent",
        "abyss.stats": "stats",
        "abyss.search": "search",
        "abyss.trace": "trace",
        "abyss.incidents": "incidents",
        "abyss.wave": "wave",
        "activity": "recent",
        "a-stats": "stats",
        "a-search": "search",
        "a-trace": "trace",
        "a-incidents": "incidents",
        "a-wave": "wave",
    }
    for _name, _sub in _SUBCOMMAND_ALIASES.items():
        ctx.register_command(
            _name,
            handler=lambda raw_args, _s=_sub: _handle_slash(
                f"{_s} {raw_args}".strip()
            ),
            description=f"Abyss {_sub} (alias)",
        )


def _handle_slash(raw_args: str) -> str:
    """Handle /abyss slash commands."""
    argv = raw_args.strip().split()
    _init_db()

    if not argv or argv[0] in {"help", "-h", "--help"}:
        return """\
/abyss — query observability data

Subcommands:
  recent [N]                   Show last N activity entries (default 10)
  stats                        Show summary statistics
  health                       Show agent health score (0-100)
  trends [days] [hour|day]     Show activity/error/signal trends
  failures [limit]             Root-cause failure taxonomy
  performance [days] [limit]   Latency percentiles (tools + models)
  search <query>              Search activity, memories, sessions
  trace <session>            Show trace timeline for a session
  signals [--session=<sid>] [--type=<t>] [--severity=<sev>]
          [--state=open|unack]
                                       Show detected signals; --open /
          --unacked are shorthands for the state filter
  incidents [--status=<st>] [--severity=<sev>] [--open]
                                       Show incidents
  ack <signal_id>            Acknowledge a signal
  resolve <signal_id>        Resolve a signal
  resolve-stale [days] [p]   Bulk-resolve stale signals (older than N days,
                             optional session_id prefix, e.g. cron_; use
                             --type <t> to filter one signal type (e.g.
                             empty_stream); appending 'close' closes
                             emptied incidents)
  resolve-agent <id>         Dispatch a free-Nous agent to diagnose + fix a signal/incident
  doctor                     Dispatch the doctor agent for a full diagnosis
  incident <id> <action>     Acknowledge/resolve/reopen/close an incident
  diagnostic <cap> <gap>     Record a self-diagnostic signal
  webhook [url|off]          Show/set webhook alerting (ABYSS_WEBHOOK_URL)
  export                     Show data volume (full JSON via GET /export)
  prune [days]               Delete data older than N days (default 30)
  clean                      Clear all data (irreversible)
  wave [surface]             Aug-2026 expansion: events|streams|api|subagents|
                             approvals|commands|platform|skills|summary

Category filters:
  --category=<cat>  Filter recent by: cron, tool, llm, session, system
  --session=<sid>   Filter recent to one session (drill-don't-rediscover)
"""

    sub = argv[0]

    if sub == "recent":
        n = 10
        category = None
        session_id = None
        for arg in argv[1:]:
            if arg.startswith("--category="):
                category = arg.split("=", 1)[1]
            elif arg.startswith("--session="):
                session_id = arg.split("=", 1)[1]
            elif arg.isdigit():
                n = int(arg)

        activities = list_activity(limit=n, category=category, session_id=session_id)
        if not activities:
            return "No activity entries found."

        lines = [f"Recent {len(activities)} activity entr{'y' if len(activities) == 1 else 'ies'}:"]
        for a in activities:
            time_str = a.get("timestamp", "")[:19] if a.get("timestamp") else "?"
            lines.append(f"  [{time_str}] {a.get('category', '?')}/{a.get('status', '?')} — {a.get('action', '?')}")
        return "\n".join(lines)

    if sub == "stats":
        from collections import Counter
        conn = _get_activity_conn()
        total = conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0]
        cats = dict(Counter(r[0] for r in conn.execute("SELECT category FROM activity").fetchall()))
        tools = dict(Counter(r[0] for r in conn.execute("SELECT tool_name FROM activity WHERE tool_name IS NOT NULL").fetchall()))
        conn.close()

        lines = [f"Abyss Stats:"]
        lines.append(f"  Total entries: {total}")
        lines.append(f"  Categories: {', '.join(f'{k}={v}' for k, v in sorted(cats.items()))}")
        lines.append(f"  Tools used: {', '.join(f'{k}={v}' for k, v in sorted(tools.items())) if tools else 'none'}")
        return "\n".join(lines)

    if sub == "search":
        query = " ".join(argv[1:])
        if not query:
            return "Usage: /abyss search <query>"
        results = global_search(query, limit=10)
        if not results:
            return f"No results for '{query}'."

        lines = [f"Search results for '{query}' ({len(results)} matches):"]
        for r in results:
            time_str = r.get("timestamp", "")[:19] if r.get("timestamp") else "?"
            lines.append(f"  [{r['source']}/{time_str}] {r.get('title', r.get('action', '?'))[:60]}")
        return "\n".join(lines)

    if sub == "trace":
        if len(argv) < 2:
            return "Usage: /abyss trace <session_id>"
        traces = get_session_trace(argv[1])
        if not traces:
            return f"No traces found for session {argv[1]}."

        lines = [f"Trace timeline for session {argv[1][:8]}...:"]
        for t in traces:
            time_str = t.get("timestamp", "")[:19]
            data = json.loads(t.get("event_data") or "{}")
            lines.append(f"  [{time_str}] {t.get('event_type')}: {json.dumps(data)[:80]}")
        return "\n".join(lines)

    if sub == "signals":
        limit = 50
        session_arg = None
        type_arg = None
        severity_arg = None
        state_arg = None
        for arg in argv[1:]:
            if arg.startswith("--limit="):
                limit = int(arg.split("=", 1)[1])
            elif arg.startswith("--session="):
                session_arg = arg.split("=", 1)[1]
            elif arg.startswith("--type="):
                type_arg = arg.split("=", 1)[1]
            elif arg.startswith("--severity="):
                severity_arg = arg.split("=", 1)[1]
            elif arg == "--state=open" or arg == "--open":
                state_arg = "open"
            elif arg == "--state=unack" or arg == "--unacked":
                state_arg = "unack"

        conn = _get_activity_conn()
        query = "SELECT * FROM signals"
        params = []
        clauses = []
        if session_arg:
            clauses.append("session_id = ?")
            params.append(session_arg)
        if type_arg:
            clauses.append("signal_type = ?")
            params.append(type_arg)
        if severity_arg:
            clauses.append("severity = ?")
            params.append(severity_arg)
        if state_arg == "open":
            clauses.append("resolved = 0")
        elif state_arg == "unack":
            clauses.append("resolved = 0 AND acknowledged = 0")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
        conn.close()

        if not rows:
            return "No signals detected. All quiet in the abyss."

        lines = [f"Recent signals ({len(rows)} detected):"]
        for r in rows:
            ack = "✓" if r["acknowledged"] else "•"
            res = "✓" if r["resolved"] else ""
            repeat = ""
            try:
                _details = json.loads(r["details"]) if r["details"] else {}
                _n = int(_details.get("repeat_count", 0))
                if _n > 0:
                    repeat = f" ×{_n + 1}"
            except (ValueError, TypeError):
                pass
            lines.append(
                f"  [{ack}{res}] [{r['timestamp'][:19]}] {r['severity'].upper()}: {r['label']}{repeat} — {r['description'][:80]}"
            )
        return "\n".join(lines)

    if sub == "incidents":
        limit = 50
        status_filter = None
        for arg in argv[1:]:
            if arg.startswith("--limit="):
                limit = int(arg.split("=", 1)[1])
            elif arg.startswith("--status="):
                status_filter = arg.split("=", 1)[1]

        conn = _get_activity_conn()
        query = "SELECT * FROM incidents"
        params = []
        if status_filter:
            query += " WHERE status = ?"
            params.append(status_filter)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
        conn.close()

        if not rows:
            return "No incidents recorded. The abyss is calm."

        lines = [f"Incidents ({len(rows)} found):"]
        for r in rows:
            lines.append(f"  [{r['status'].upper()}] [{r['timestamp'][:19]}] {r['severity']} — {r['title']}")
            lines.append(f"    Signals: {r['signal_count']} | Pattern: {r['pattern']}")
        return "\n".join(lines)

    if sub == "diagnostic":
        """Record a self-diagnostic signal — the agent reports its own capability gap."""
        if len(argv) < 3:
            return "Usage: /abyss diagnostic <capability> <gap description>"
        capability = argv[1]
        gap = " ".join(argv[2:])
        sig_id = _record_self_diagnostic(
            session_id="",
            capability=capability,
            gap=gap,
        )
        _cluster_incidents()
        return f"✓ Self-diagnostic recorded (signal #{sig_id}). Abyss is watching."

    if sub == "ack":
        """Acknowledge a signal: /abyss ack <signal_id>"""
        if len(argv) < 2:
            return "Usage: /abyss ack <signal_id>"
        try:
            sig_id = int(argv[1])
        except ValueError:
            return f"Invalid signal id: {argv[1]}"
        row = _acknowledge_signal(sig_id)
        if row is None:
            return f"Signal {sig_id} not found."
        return f"✓ Signal #{sig_id} acknowledged ({row['signal_type']})."

    if sub == "resolve":
        """Resolve a signal: /abyss resolve <signal_id>"""
        if len(argv) < 2:
            return "Usage: /abyss resolve <signal_id>"
        try:
            sig_id = int(argv[1])
        except ValueError:
            return f"Invalid signal id: {argv[1]}"
        row = _resolve_signal(sig_id)
        if row is None:
            return f"Signal {sig_id} not found."
        return f"✓ Signal #{sig_id} resolved ({row['signal_type']})."

    if sub == "resolve-stale":
        """Bulk-resolve old signals, e.g. /abyss resolve-stale 7 cron_

        Syntax: /abyss resolve-stale [days] [session_prefix] [--type <t>] [close]
        - days: resolve signals older than N days (default 7)
        - session_prefix: only signals whose session_id starts with this
          (e.g. cron_ for overnight watcher noise); omit (or use --type)
          when filtering by signal type alone
        - --type <t>: only resolve signals of this type, e.g. --type
          empty_stream for flood cleanup, --type tool_error
        - close: also close incidents left with zero open signals
        """
        days = int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else 7
        prefix = None
        sig_type = None
        close_incidents = False
        rest = argv[2:]
        if rest and not rest[0].startswith("--"):
            prefix = rest.pop(0)
        while rest:
            a = rest.pop(0)
            if a in ("--type", "-t") and rest:
                sig_type = rest.pop(0)
            elif a in ("--close", "close"):
                close_incidents = True
        result = _resolve_signals_bulk(
            session_prefix=prefix,
            signal_type=sig_type,
            older_than_days=days,
            close_empty_incidents=close_incidents,
        )
        if "error" in result:
            return f"✗ {result['error']}"
        lines = [f"✓ Bulk-resolved {result['resolved']} signal(s)"]
        if result["signal_ids"]:
            lines.append(f"  ids: {result['signal_ids'][:10]}{'...' if len(result['signal_ids']) > 10 else ''}")
        if result["incidents_closed"]:
            lines.append(f"  incidents closed: {result['incidents_closed']}")
        return "\n".join(lines)

    if sub == "resolve-agent":
        """Dispatch a free-Nous agent to diagnose + fix: /abyss resolve-agent <signal|incident> <id>"""
        if len(argv) < 3 or argv[1] not in ("signal", "incident"):
            return "Usage: /abyss resolve-agent <signal|incident> <id>"
        try:
            obj_id = int(argv[2])
        except ValueError:
            return f"Invalid id: {argv[2]}"
        kind = "signals" if argv[1] == "signal" else "incidents"
        result = _dispatch_resolution(kind, obj_id)
        if "error" in result:
            return f"✗ {result['error']}"
        return f"⏳ Agent dispatched to resolve {argv[1]} #{obj_id} (report: {result.get('report_id')})"

    if sub == "doctor":
        """Dispatch the doctor agent: /abyss doctor"""
        result = _dispatch_doctor()
        if "error" in result:
            return f"✗ {result['error']}"
        return f"⏳ Doctor agent dispatched (report: {result.get('report_id')}). Poll GET /doctor/report?report_id=..."

    if sub == "incident":
        """Manage an incident: /abyss incident <id> <acknowledge|resolve|reopen|close>"""
        if len(argv) < 3:
            return "Usage: /abyss incident <id> <acknowledge|resolve|reopen|close>"
        try:
            inc_id = int(argv[1])
        except ValueError:
            return f"Invalid incident id: {argv[1]}"
        action = argv[2].lower()
        target = {"acknowledge": "acknowledged", "resolve": "resolved",
                  "reopen": "open", "close": "closed"}.get(action)
        if target is None:
            return f"Unknown incident action: {action}"
        row = _update_incident_status(inc_id, target)
        if row is None:
            return f"Incident {inc_id} not found."
        return f"✓ Incident #{inc_id} → {target}."

    if sub == "prune":
        """Delete data older than N days: /abyss prune [days]"""
        days = int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else 30
        deleted = _prune_data(days)
        return f"✓ Pruned data older than {days} days: {deleted}"

    if sub == "health":
        """Show overall agent health score."""
        h = get_health()
        lines = [f"Abyss Health: {h['score']}/100 ({h['level'].upper()})"]
        lines.append(f"  error_rate: {h['components']['error_rate']:.1%}")
        lines.append(f"  signals_open: {h['counts']['signals_open']} | incidents_open: {h['counts']['incidents_open']}")
        lines.append(f"  activity_24h: {h['counts']['activity_24h']}")
        lines.append(f"  breakdown: {h['components']}")
        return "\n".join(lines)

    if sub == "trends":
        """Show activity/signal trends: /abyss trends [days] [hour|day]"""
        days = int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else 7
        bucket = argv[2] if len(argv) > 2 and argv[2] in ("hour", "day") else "day"
        t = get_trends(days=days, bucket=bucket)
        if not t["timestamps"]:
            return "No trend data."
        lines = [f"Abyss trends ({bucket} buckets, last {days} days):"]
        for i, ts in enumerate(t["timestamps"]):
            if t["activity"][i] or t["errors"][i] or t["signals"][i]:
                lines.append(f"  {ts}: act={t['activity'][i]} err={t['errors'][i]} sig={t['signals'][i]} inc={t['incidents'][i]}")
        return "\n".join(lines) if len(lines) > 1 else "No activity in window."

    if sub == "failures":
        """Show root-cause failure taxonomy: /abyss failures [limit]"""
        limit = int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else 10
        f = get_failures(limit=limit)
        lines = ["Abyss failure taxonomy:"]
        if f["by_type"]:
            lines.append("  Top signal types: " + ", ".join(f"{x['type']}={x['count']}" for x in f["by_type"][:5]))
        if f["by_tool"]:
            lines.append("  Top failing tools: " + ", ".join(f"{x['tool']}={x['count']}" for x in f["by_tool"][:5]))
        if f["by_message"]:
            lines.append("  Common errors:")
            for x in f["by_message"][:5]:
                lines.append(f"    - [{x['count']}x] {x['message'][:90]}")
        return "\n".join(lines) if len(lines) > 1 else "No failures recorded."

    if sub == "performance":
        """Show latency percentiles: /abyss performance [days] [limit]"""
        days = int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else 7
        limit = int(argv[2]) if len(argv) > 2 and argv[2].isdigit() else 10
        p = get_performance(days=days, limit=limit)
        lines = [f"Abyss performance (last {days}d):"]
        lines.append(
            f"  totals: {p['totals']['tool_calls']} tool calls "
            f"({p['totals']['tool_errors']} errors), "
            f"{p['totals']['llm_requests']} LLM requests "
            f"({p['totals']['llm_errors']} errors)"
        )
        if p["tools"]:
            lines.append("  Slowest tools (p95 ms):")
            for t in p["tools"][:5]:
                lines.append(
                    f"    {t['tool']}: p50={t['p50_ms']} p95={t['p95_ms']} "
                    f"max={t['max_ms']} ({t['count']} calls)"
                )
        if p["models"]:
            lines.append("  Slowest models (p95 ms):")
            for m in p["models"][:5]:
                lines.append(
                    f"    {m['model']} ({m['provider']}): p50={m['p50_ms']} "
                    f"p95={m['p95_ms']} max={m['max_ms']} "
                    f"({m['count']} reqs)"
                )
        return "\n".join(lines) if len(lines) > 2 else "No performance data in window."

    if sub == "export":
        """Dump all data as JSON: /abyss export"""
        data = export_data()
        summary = {
            "activity": len(data["activity"]),
            "signals": len(data["signals"]),
            "incidents": len(data["incidents"]),
            "traces": len(data["traces"]),
            "wave": {k: len(v) for k, v in data.get("wave", {}).items()
                     if k != "_missing"},
            "exported_at": data["exported_at"],
        }
        return json.dumps(summary)

    if sub == "webhook":
        """Check or set webhook alerting: /abyss webhook [url] | /abyss webhook off"""
        try:
            from abyss_wave import SETTINGS as _WAVE_SETTINGS
        except Exception:
            _WAVE_SETTINGS = None
        if len(argv) < 2:
            url = os.environ.get("ABYSS_WEBHOOK_URL", "").strip()
            if not url and _WAVE_SETTINGS is not None:
                url = str(_WAVE_SETTINGS.get("webhook_url", "") or "").strip()
            return f"Webhook: {'configured (' + url + ')' if url else 'not configured (set ABYSS_WEBHOOK_URL or plugins.entries.abyss.settings.webhook_url)'}"
        if argv[1] == "off":
            os.environ["ABYSS_WEBHOOK_URL"] = ""
            if _WAVE_SETTINGS is not None:
                _WAVE_SETTINGS["webhook_url"] = ""
            return "✓ Webhook disabled for this process."
        os.environ["ABYSS_WEBHOOK_URL"] = argv[1]
        if _WAVE_SETTINGS is not None:
            _WAVE_SETTINGS["webhook_url"] = argv[1]
        return f"✓ Webhook set: {argv[1]}"

    if sub == "clean":
        conn = _get_activity_conn()
        conn.execute("DELETE FROM activity")
        conn.execute("DELETE FROM signals")
        conn.execute("DELETE FROM incidents")
        conn.execute("DELETE FROM sqlite_sequence WHERE name='activity'")
        conn.execute("DELETE FROM sqlite_sequence WHERE name='signals'")
        conn.execute("DELETE FROM sqlite_sequence WHERE name='incidents'")
        conn.commit()
        conn.close()

        tconn = _get_trace_conn()
        tconn.execute("DELETE FROM traces")
        tconn.execute("DELETE FROM sqlite_sequence WHERE name='traces'")
        tconn.commit()
        tconn.close()

        # Aug-2026 wave tables live on the same activity DB; a "clear all
        # data" that leaves stream/API/approval history behind is a half-wipe.
        _wave_cleared = {}
        try:
            from abyss_wave import clear_wave_data

            _wave_cleared = clear_wave_data()
        except Exception as exc:
            logger.debug("Abyss wave clean skipped: %s", exc)

        return ("✓ All activity, signals, incidents, trace, and wave data "
                f"cleared{(' (' + ', '.join(f'{k}={v}' for k, v in _wave_cleared.items() if v) + ' rows)') if _wave_cleared else ''}.")

    if sub == "wave":
        """August 2026 wave surfaces: events, streams, api, subagents, approvals,
        commands, platform, skills — the plugin-interface expansion observability."""
        surface = argv[1] if len(argv) > 1 else "summary"
        limit = 20
        for arg in argv[2:]:
            if arg.startswith("--limit="):
                limit = int(arg.split("=", 1)[1])
        try:
            from abyss_wave import (
                list_api_requests,
                list_approvals,
                list_commands,
                list_platform_events,
                list_skills,
                list_streams,
                list_subagents,
                list_wave_events,
                wave_summary,
            )
        except Exception as exc:
            return f"✗ Wave module unavailable: {exc}"

        if surface == "summary":
            s = wave_summary()
            tables = s.get("tables", {})
            lines = ["Abyss wave — plugin-interface expansion (Aug 2026):"]
            for name, info in tables.items():
                lines.append(f"  {name:16s} {info['count']:>6}  last={info.get('last') or '—'}")
            return "\n".join(lines)

        table_map = {
            "events": ("plugin events", list_wave_events),
            "streams": ("streams", list_streams),
            "api": ("API requests", list_api_requests),
            "subagents": ("subagents", list_subagents),
            "approvals": ("approvals", list_approvals),
            "commands": ("commands", list_commands),
            "platform": ("platform events", list_platform_events),
            "skills": ("skills", list_skills),
        }
        if surface not in table_map:
            return "Usage: /abyss wave [summary|events|streams|api|subagents|approvals|commands|platform|skills] [--limit=N]"
        label, fn = table_map[surface]
        rows = fn(limit=limit)
        if not rows:
            return f"No {label} recorded yet."
        lines = [f"Recent {label} ({len(rows)}):"]
        for r in rows:
            ts = str(r.get("timestamp") or "")[:19]
            primary = r.get("event") or r.get("command") or r.get("event_type") \
                or r.get("action") or r.get("model") or r.get("child_role") or r.get("choice") or r.get("name") or "?"
            detail = r.get("surface") or r.get("provider") or r.get("status") or r.get("platform") or ""
            lines.append(f"  [{ts}] {primary} {('| ' + str(detail)) if detail else ''}")
        return "\n".join(lines)

    return f"Unknown subcommand: {sub}\nType /abyss help for usage."
