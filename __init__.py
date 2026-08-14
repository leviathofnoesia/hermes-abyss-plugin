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
    """Initialize databases if not exists."""
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
    _migrate_schema(conn)
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
    result_preview = str(result)[:200] if result else ""
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
    result_preview = str(assistant_response)[:200] if assistant_response else ""
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
        description=f"Session {session_id[:8]} started via {source}",
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
        description=f"Session {session_id[:8]} {'completed' if completed else 'interrupted'}",
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

# Failure patterns that Raindrop detects — these are the "silent agent failures"
# that traditional logging misses (per Raindrop docs: silent tool errors,
# "forgetting", vague replies, persona drift, hallucinations, loops).
_SIGNAL_PATTERNS = [
    ("tool_error",      "error",  "error",   "Tool call failed with an error"),
    ("timeout",         "timeout", "warning", "Tool call or operation timed out"),
    ("rate_limit",      "rate_limit", "warning", "API rate limit hit"),
    ("loop_detected",   "loop",   "error",   "Agent appears to be in a loop"),
    ("vague_reply",     "vague",  "warning", "LLM response was vague or unhelpful"),
    ("drift_detected",  "drift",  "warning", "Potential persona drift detected"),
    ("context_loss",    "context", "error",  "Context may have been lost between turns"),
]


def _detect_signals(
    tool_name: str,
    result: Any,
    session_id: str,
    status: str,
    activity_id: int,
    error_type: str = "",
    error_message: str = "",
    duration_ms: int = 0,
) -> list:
    """Run Raindrop-style signal classifiers on a tool call result.

    Returns a list of detected signals. Each signal is dict with:
    signal_type, severity, label, description, details.

    Detection logic:
    1. Error-based: structured ``status == error`` / ``error_type`` set, or
       result text contains error-like keywords
    2. Timeout-based: structured ``error_type`` mentions timeout, or result text
    3. Rate limit: structured ``error_type`` mentions rate/429, or result text
    4. Slow call: structured ``duration_ms`` above threshold (60s)
    5. Loop detection: same tool called with identical args in same session
    6. Vague reply: LLM response very short or refusal-like
    """
    signals = []
    result_str = str(result).lower() if result else ""
    err_type_l = (error_type or "").lower()
    err_msg_l = (error_message or "").lower()
    already: set = set()

    # 1b. Benign read_file error suppression (computed before signal detection)
    # "File not found" is an agent's normal exploratory path probing (e.g.
    # probing for config/README/etc.) and not a backend fault. "Access denied:
    # ...credential store" is an intentional defense-in-depth gate, not a real
    # failure. Both flood the signal firehose with tool_error signals; suppress
    # them. Genuine permission errors on real files still classify normally.
    _SUPPRESS_TOOL_ERROR = False
    if tool_name == "read_file" and status == "error":
        _rf_low = err_msg_l + result_str
        if "file not found" in _rf_low or ("access denied" in _rf_low and "credential store" in _rf_low):
            _SUPPRESS_TOOL_ERROR = True

    def _add(signal_type, severity, label, description, details=None):
        if signal_type in already:
            return
        already.add(signal_type)
        signals.append({
            "signal_type": signal_type,
            "severity": severity,
            "label": label,
            "description": description,
            "details": details or {},
        })

    # 0. Exit-code classification. A bare "exit N" error_message carries no
    # diagnostic value yet currently triple-fires (tool_error + timeout +
    # slow_call for exit 124). Map the known codes to a single cause so each
    # event yields exactly one correctly-typed signal.
    _exit_match = re.match(r"^exit (-?\d+)$", (error_message or "").strip())
    _exit_cause = ({"124": "timeout", "127": "command-not-found",
                    "137": "killed", "-1": "killed", "-9": "killed"}.get(_exit_match.group(1))
                   if _exit_match else None)

    # 1. Error detection (structured only). The old text fallback scanned
    # result text on COMPLETED calls for error-like keywords ("error:",
    # "traceback", "*Error" class names) and fired ~127 false tool_error
    # signals on successful calls whose output merely mentioned an error
    # (grep results, build logs, read-file contents, memory entries). Real
    # tool failures always arrive with status=="error" or a structured
    # error_type, so the fallback was pure false-positive noise and is
    # removed. The read_file suppression above now fully silences tool_error.
    if _exit_cause == "timeout":
        pass  # exit 124 is a timeout kill; the timeout branch below owns it
    elif (not _SUPPRESS_TOOL_ERROR) and (status == "error" or err_type_l or "error" in err_type_l):
        if error_message:
            desc = f"Tool '{tool_name}' failed: {error_message[:200]}"
        else:
            desc = f"Tool '{tool_name}' failed with an error state"
        details = {"error_type": error_type, "error_message": error_message}
        if _exit_match:
            details["exit_code"] = _exit_match.group(1)
        # Bare exit codes are downgraded to warning: no diagnostic value.
        _add("tool_error", "warning" if _exit_match else "error", "Tool Error", desc, details)

    # 2. Timeout detection
    # Structured fields first; the result-text fallback requires a strong
    # tool-generated signature. A bare "timed out" substring in free text
    # (a page the agent merely read, log prose) is not evidence the tool call
    # itself timed out and flooded the feed with false positives.
    if _exit_cause == "timeout" or "timeout" in err_type_l or "timeout" in err_msg_l or "timed out" in err_msg_l \
            or "timeout error" in result_str or "operation timed out" in result_str:
        _add("timeout", "warning", "Timeout",
             f"Tool '{tool_name}' operation timed out",
             {"error_type": error_type, "error_message": error_message})

    # 3. Rate limit detection (structured evidence only).
    # Result-text scanning fired on benign content: 29/37 unresolved
    # rate_limit signals sat on COMPLETED calls with empty structured fields
    # (a read_file of a file that merely mentions "rate"/"quota", memory
    # entries containing "rate"). A rate limit is a backend error and always
    # arrives via error_type/error_message. Keep only the unambiguous
    # "429"/"too many requests" result tokens, restricted to failed calls.
    _RATE_LIMIT_TOKENS = ("429", "too many requests")
    _CREDIT_TOKENS = ("credit", "balance", "exhausted", "payment required",
                      "402", "quota", "insufficient")
    if any(tok in err_msg_l for tok in _RATE_LIMIT_TOKENS) \
            or ("429" in err_type_l or "too many requests" in err_type_l) \
            or (status == "error" and any(tok in result_str for tok in _RATE_LIMIT_TOKENS)) \
            or any(tok in err_msg_l for tok in _CREDIT_TOKENS) \
            or any(tok in err_type_l for tok in _CREDIT_TOKENS):
        _add("rate_limit", "warning", "Rate Limit",
             f"API rate limit hit during '{tool_name}'",
             {"error_type": error_type, "error_message": error_message})

    # 4. Slow call detection (structured duration)
    if duration_ms and duration_ms > 60000 and _exit_cause != "timeout":
        # A timeout-killed call's duration IS the timeout, not a slow call.
        _add("slow_call", "info", "Slow Call",
             f"Tool '{tool_name}' took {duration_ms / 1000:.1f}s (>60s)",
             {"duration_ms": duration_ms})

    # 5. Loop detection: check if same tool called with same args recently
    if tool_name and session_id:
        conn = _get_activity_conn()
        try:
            recent = conn.execute(
                """SELECT tool_name, args, timestamp FROM activity
                   WHERE session_id = ? AND tool_name = ? AND id != ?
                   ORDER BY timestamp DESC LIMIT 3""",
                (session_id, tool_name, activity_id)
            ).fetchall()
            if len(recent) >= 2:
                # If same tool called 3+ times in same session with similar args = loop
                args_list = [r["args"] for r in recent]
                if len(set(args_list)) == 1:
                    _add("loop_detected", "error", "Agent Loop",
                         f"Tool '{tool_name}' called {len(recent)+1}x with identical args in same session")
        except sqlite3.Error:
            pass
        finally:
            conn.close()

    # 6. Vague/empty reply detection for LLM results
    if tool_name in ("llm_call_completed",) or "llm" in str(tool_name).lower():
        result_clean = result_str.strip().strip('"\'` \n\r\t').strip()
        if result_clean:
            if len(result_clean) < 20 and not result_clean.endswith(('.', '!', '?')):
                _add("vague_reply", "warning", "Vague Reply",
                     f"LLM response appears too short or vague ({len(result_clean)} chars)")
            elif any(token in result_clean for token in (
                "i don't know", "i cannot", "i can't", "not sure how", "unable to",
                "i am not able", "sorry, i can't", "i'm sorry, but i can't",
            )):
                _add("refusal", "warning", "LLM Refusal",
                     f"LLM response contains a refusal/unable pattern")
        elif status == "error":
            _add("empty_result", "warning", "Empty Result",
                 f"Tool '{tool_name}' returned no result with error status")

    return signals


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
# ---------------------------------------------------------------------------

_SEVERITY_RANK = {"critical": 4, "error": 3, "warning": 2, "info": 1}


def _acknowledge_signal(signal_id: int, note: str = "") -> Optional[dict]:
    """Mark a signal acknowledged. Returns the updated row or None."""
    _init_db()
    conn = _get_activity_conn()
    try:
        conn.execute(
            "UPDATE signals SET acknowledged = 1, acknowledged_at = ? WHERE id = ?",
            (datetime.now().isoformat(), signal_id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone()
        return dict(row) if row else None
    except sqlite3.Error as e:
        logger.error("Failed to acknowledge signal %s: %s", signal_id, e)
        return None
    finally:
        conn.close()


def _resolve_signal(signal_id: int, note: str = "") -> Optional[dict]:
    """Mark a signal resolved (also acknowledged). Returns the updated row."""
    _init_db()
    now = datetime.now().isoformat()
    conn = _get_activity_conn()
    try:
        conn.execute(
            "UPDATE signals SET acknowledged = 1, resolved = 1, acknowledged_at = ?, resolved_at = ? WHERE id = ?",
            (now, now, signal_id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone()
        return dict(row) if row else None
    except sqlite3.Error as e:
        logger.error("Failed to resolve signal %s: %s", signal_id, e)
        return None
    finally:
        conn.close()


def _update_incident_status(incident_id: int, status: str) -> Optional[dict]:
    """Transition an incident to a new status (open/acknowledged/resolved/closed).

    When resolved/closed, also resolves all linked open signals.
    """
    valid = {"open", "acknowledged", "resolved", "closed"}
    if status not in valid:
        return None
    _init_db()
    conn = _get_activity_conn()
    try:
        conn.execute(
            "UPDATE incidents SET status = ?, resolved_at = ? WHERE id = ?",
            (status, datetime.now().isoformat() if status in ("resolved", "closed") else None, incident_id),
        )
        if status in ("resolved", "closed"):
            conn.execute(
                "UPDATE signals SET resolved = 1, acknowledged = 1 WHERE incident_id = ?",
                (incident_id,),
            )
        conn.commit()
        row = conn.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        return dict(row) if row else None
    except sqlite3.Error as e:
        logger.error("Failed to update incident %s: %s", incident_id, e)
        return None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Agent-powered resolution — spawn a free-Nous Hermes agent to diagnose + fix
# ---------------------------------------------------------------------------
#
# The "resolve" buttons in the desktop UI now dispatch a real agent instead
# of just flipping a DB flag. An independent `hermes chat -q` process runs as
# the same profile (default free Nous model), loads the `abyss-doctor` skill,
# diagnoses the root cause, fixes it on the backend, and writes a JSON report.
# A background thread watches the report and only then marks the
# signal/incident resolved (or failed, so the user can retry).

_RESOLUTION_DIR = PLUGIN_DATA / "resolutions"
_RESOLUTION_DIR.mkdir(parents=True, exist_ok=True)

_AGENT_DEFAULT_TIMEOUT = int(os.environ.get("ABYSS_AGENT_TIMEOUT", "1200") or 1200)


def _redact(value: Any, limit: int = 200) -> Any:
    """Redact secret-looking keys and truncate long values before embedding
    tool arguments / error payloads into an agent prompt."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            kl = str(k).lower()
            if any(t in kl for t in ("token", "secret", "password", "api_key", "apikey", "authorization", "auth")):
                out[k] = "***"
            else:
                out[k] = _redact(v, limit)
        return out
    if isinstance(value, (list, tuple)):
        return [_redact(v, limit) for v in value[:20]]
    if value is None:
        return None
    s = str(value)
    return s if len(s) <= limit else s[:limit] + "…"


def _resolve_agent_cmd(prompt: str) -> list:
    """Build the command list that runs the Abyss agent.

    Honors the ``ABYSS_AGENT_CMD`` override (either a JSON list of argv or a
    shlex string; the prompt is placed after ``-q`` if present, else appended)
    — used by the test suites to stub the agent. The default is the `hermes`
    CLI with the abyss-doctor skill preloaded and quiet mode, so it runs as
    the same profile (free Nous model by default).
    """
    override = os.environ.get("ABYSS_AGENT_CMD", "").strip()
    if override:
        argv = None
        if override.startswith("["):
            try:
                argv = json.loads(override)
            except (ValueError, TypeError):
                argv = None
        if argv is None:
            argv = shlex.split(override)
        if "-q" in argv:
            idx = argv.index("-q")
            # Only replace the arg AFTER -q when it is actually the prompt;
            # -q at the end (or followed by another flag) appends instead of
            # crashing with IndexError or clobbering the flag.
            if idx + 1 < len(argv) and not argv[idx + 1].startswith("-"):
                argv[idx + 1] = prompt
            else:
                argv.append(prompt)
        else:
            argv.append(prompt)
        return argv

    exe = shutil.which("hermes")
    if not exe:
        candidates = [
            Path(HERMES_HOME) / "hermes-agent" / "venv" / "Scripts" / "hermes.exe",
            Path(HERMES_HOME) / "hermes-agent" / "venv" / "bin" / "hermes",
            Path(HERMES_HOME) / "bin" / "hermes",
        ]
        exe = next((str(c) for c in candidates if Path(c).exists()), None)
    if not exe:
        raise RuntimeError("hermes CLI not found; set ABYSS_AGENT_CMD to the agent command")
    # --max-turns 300: dispatched agents routinely die at the default 60-turn
    # cap before finishing multi-fix apply runs (observed: "2/8 applied" twice
    # because the agent hit "Reached maximum iterations (60)"). The prompts
    # time-box investigation, so a larger budget cannot spin forever.
    return [exe, "chat", "-q", prompt, "-s", "abyss-doctor", "-Q", "--max-turns", "300"]


def _prune_resolutions(retention_days: int = 30, keep_recent: int = 20, safety_hours: int = 1) -> dict:
    """Hygiene: bound the resolutions dir.

    Deletes resolution ``.json``/``.log`` pairs older than ``retention_days``,
    keeping at least the newest ``keep_recent`` per kind (doctor-/signal-/
    incident-/other-). Never touches files modified within ``safety_hours``
    (an active run is being written) or reports still in an
    in_progress/running state (the UI/worker polls those).
    """
    deleted = 0
    if not _RESOLUTION_DIR.exists():
        return {"deleted": 0}
    cutoff = time.time() - retention_days * 86400
    safety = time.time() - safety_hours * 3600
    by_kind: dict = {}
    for f in _RESOLUTION_DIR.glob("*.json"):
        kind = f.stem.split("-")[0] if "-" in f.stem else "other"
        by_kind.setdefault(kind, []).append(f)
    for kind, files in by_kind.items():
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for i, f in enumerate(files):
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            if mtime >= cutoff or mtime > safety or i < keep_recent:
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("status") in ("in_progress", "running"):
                    continue
            except Exception:
                pass
            for ext in (".json", ".log"):
                try:
                    (f.parent / (f.stem + ext)).unlink()
                    deleted += 1
                except OSError:
                    pass
    return {"deleted": deleted}


def _spawn_agent(prompt: str, report_path: Path, role: str = "resolver") -> "subprocess.Popen":
    """Spawn the Abyss agent as an independent, background `hermes` process.

    The child inherits this process's HERMES_HOME / HERMES_PROFILE_HOME so it
    runs as the same profile (free Nous model) and loads the same skills. The
    report path is passed via ``ABYSS_REPORT_PATH`` and stdout/stderr are
    captured to a sibling ``.log`` file (no console window on Windows).

    Every spawned child is registered in ``_AGENT_PROCS`` and terminated by an
    atexit hook if this process exits while the child is still alive — a
    dispatched agent must NEVER outlive its backend (stray agents hold file
    handles on the install and block Hermes self-updates).
    """
    cmd = _resolve_agent_cmd(prompt)
    env = os.environ.copy()
    env["HERMES_HOME"] = str(HERMES_HOME)
    env["HERMES_PROFILE_HOME"] = str(PROFILE_HOME)
    env["ABYSS_REPORT_PATH"] = str(report_path)
    env["ABYSS_AGENT_ROLE"] = role
    env["PYTHONUNBUFFERED"] = "1"
    log_path = report_path.with_suffix(".log")
    logf = open(log_path, "wb", buffering=0)
    kwargs = {
        "stdout": logf,
        "stderr": subprocess.STDOUT,
        "env": env,
        "cwd": str(PROFILE_HOME),
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    _prune_agent_procs()  # drop finished children first (never grows unbounded)
    try:
        _prune_resolutions()  # lazy hygiene: bound the resolutions dir on activity
    except Exception:
        pass
    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except Exception:
        # Never leak the open .log handle (it holds a file lock on Windows).
        try:
            logf.close()
        except Exception:
            pass
        raise
    _AGENT_PROCS.add(proc)
    return proc


# Registry of live dispatched agents + cleanup on process exit. A child that
# outlives the backend (backend killed, test run aborted) lingers holding file
# handles on the install — the Hermes updater then refuses to run ("another
# Hermes process is using this installation"). Kill survivors on exit.
# Finished children are PRUNED on every spawn so the registry (and each
# child's open .log stream) cannot accumulate for the backend's lifetime.
_AGENT_PROCS: set = set()


def _prune_agent_procs() -> None:
    for proc in list(_AGENT_PROCS):
        if proc.poll() is not None:  # finished — release its .log stream handle
            try:
                if getattr(proc, "stdout", None) is not None:
                    proc.stdout.close()
            except Exception:
                pass
            _AGENT_PROCS.discard(proc)


def _cleanup_agent_procs() -> None:
    for proc in list(_AGENT_PROCS):
        if proc.poll() is None:  # still running
            try:
                proc.terminate()
            except Exception:
                pass
    _AGENT_PROCS.clear()


import atexit as _atexit  # noqa: E402
_atexit.register(_cleanup_agent_procs)


def _read_report_file(report_path: Path) -> Optional[dict]:
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _write_report_file(report_path: Path, report: dict) -> None:
    try:
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.error("Abyss: failed to write report %s: %s", report_path, exc)


def _mark_resolution(kind: str, obj_id: int, status: str, note: str = "") -> None:
    """Record the outcome of an agent resolution run on a signal/incident.

    ``status == "succeeded"`` also resolves the object (and, for incidents,
    every linked signal); ``failed`` leaves it open so the user can retry.
    """
    _init_db()
    now = datetime.now().isoformat()
    note = (note or "")[:2000]
    conn = _get_activity_conn()
    try:
        if kind == "signals":
            conn.execute(
                """UPDATE signals SET resolution_status = ?, resolution_note = ?,
                       resolution_finished_at = ?, resolved = ?, acknowledged = 1, resolved_at = ?
                   WHERE id = ?""",
                (status, note, now, 1 if status == "succeeded" else 0,
                 now if status == "succeeded" else None, obj_id),
            )
        else:
            conn.execute(
                """UPDATE incidents SET resolution_status = ?, resolution_note = ?,
                       resolution_finished_at = ?, status = ?, resolved_at = ?
                   WHERE id = ?""",
                (status, note, now,
                 "resolved" if status == "succeeded" else "open",
                 now if status == "succeeded" else None, obj_id),
            )
            if status == "succeeded":
                conn.execute(
                    "UPDATE signals SET resolved = 1, acknowledged = 1, resolved_at = ? WHERE incident_id = ?",
                    (now, obj_id),
                )
        conn.commit()
    except sqlite3.Error as exc:
        logger.error("Failed to mark resolution %s %s: %s", kind, obj_id, exc)
    finally:
        conn.close()


def _resolution_context(kind: str, row: dict) -> dict:
    """Build the evidence context handed to the resolver agent."""
    _init_db()
    conn = _get_activity_conn()
    try:
        ctx = {"kind": kind, "id": row["id"]}
        if kind == "signals":
            ctx["signal"] = _redact(dict(row))
            linked = None
            if row.get("activity_id"):
                linked = conn.execute("SELECT * FROM activity WHERE id = ?", (row["activity_id"],)).fetchone()
            ctx["linked_activity"] = _redact(dict(linked)) if linked else None
            errs = conn.execute(
                """SELECT id, timestamp, action, tool_name, status, metadata
                   FROM activity WHERE session_id = ? AND status = 'error'
                   ORDER BY timestamp DESC LIMIT 8""",
                (row.get("session_id") or "",),
            ).fetchall()
            ctx["session_errors"] = [_redact(dict(r)) for r in errs]
        else:
            ctx["incident"] = _redact(dict(row))
            sig_ids = []
            try:
                sig_ids = json.loads(row.get("signal_ids") or "[]")
            except (ValueError, TypeError):
                sig_ids = []
            sigs = []
            if sig_ids:
                placeholders = ",".join("?" for _ in sig_ids)
                sigs = conn.execute(
                    f"SELECT * FROM signals WHERE id IN ({placeholders}) ORDER BY timestamp DESC LIMIT 30",
                    sig_ids,
                ).fetchall()
            ctx["linked_signals"] = [_redact(dict(s)) for s in sigs]
            sessions = []
            for s in sigs[:10]:
                sid = s["session_id"]
                if sid and sid not in sessions:
                    sessions.append(sid)
            errs = []
            for sid in sessions[:3]:
                errs.extend(conn.execute(
                    """SELECT id, timestamp, action, tool_name, status, metadata
                       FROM activity WHERE session_id = ? AND status = 'error'
                       ORDER BY timestamp DESC LIMIT 5""",
                    (sid,),
                ).fetchall())
            ctx["session_errors"] = [_redact(dict(r)) for r in errs[:15]]
        return ctx
    finally:
        conn.close()


def _build_resolver_prompt(kind: str, row: dict, context: dict, report_path: Path) -> str:
    ctx_json = json.dumps(context, indent=2)[:20000]
    kind_label = "signal" if kind == "signals" else "incident"
    return (
        "You are the Abyss resolver: an autonomous Hermes agent that diagnoses and FIXES "
        "agent-observability issues detected by the Abyss plugin.\n\n"
        "Load the 'abyss-doctor' skill and follow it exactly.\n\n"
        f"OBSERVED {kind_label.upper()} CONTEXT (JSON):\n{ctx_json}\n\n"
        "TASK:\n"
        "1. Diagnose the root cause from the context AND the live system. You have full "
        "tool access: read files, inspect HERMES_HOME logs, check processes, edit configs "
        "and plugin code.\n"
        "2. Actually FIX the root cause on the backend — do not only describe it.\n"
        "3. If the fix is reusable, save a skill named abyss-fix-<pattern> documenting it.\n"
        "4. Write your report to ABYSS_REPORT_PATH as JSON with this exact schema:\n"
        '{"schema":"abyss-resolution/1","role":"resolver","report_id":"' + report_path.stem + '",'
        '"status":"succeeded|failed","summary":"one-line summary",'
        '"findings":[{"title":"...","detail":"...","evidence":"..."}],'
        '"actions_taken":["..."],"skills_saved":["..."],"error":null}\n'
        "5. Your final chat response must be the one-line summary of what you fixed.\n"
        "6. TIME-BOX: spend at most ~8 tool actions on investigation. If you cannot "
        "reach a verified fix, write a PARTIAL report with status 'failed' and your "
        "findings so far — do not keep investigating forever.\n"
        "Do NOT mark anything resolved yourself — the backend does that from your report."
    )


def _resolution_finalize(kind: str, obj_id: int, report_path: Path, report: Optional[dict]) -> None:
    ok = bool(report and report.get("status") == "succeeded")
    note = (report or {}).get("summary") or (report or {}).get("error") or "agent completed"
    _mark_resolution(kind, obj_id, "succeeded" if ok else "failed", note)
    _add_activity(
        action="resolution_completed" if ok else "resolution_failed",
        description=f"Agent {'resolved' if ok else 'failed to resolve'} {kind[:-1]} {obj_id}: {note[:120]}",
        category="system",
        status="completed" if ok else "error",
        metadata={"kind": kind, "obj_id": obj_id, "note": note[:500], "report": str(report_path)},
    )


def _resolution_worker(kind: str, obj_id: int, report_path: Path, prompt: str) -> None:
    """Background thread: run the resolver agent, then finalize from its report."""
    timeout_s = _AGENT_DEFAULT_TIMEOUT
    try:
        proc = _spawn_agent(prompt, report_path, role="resolver")
    except Exception as exc:
        logger.error("Abyss resolver spawn failed: %s", exc)
        _mark_resolution(kind, obj_id, "failed", f"agent spawn failed: {exc}")
        return
    deadline = time.time() + timeout_s
    report = None
    while time.time() < deadline:
        if proc.poll() is not None:
            report = _read_report_file(report_path)
            if report is not None:
                break
            try:
                log_tail = report_path.with_suffix(".log").read_text(encoding="utf-8", errors="replace")[-1500:]
            except Exception:
                log_tail = ""
            report = {
                "schema": "abyss-resolution/1", "role": "resolver", "status": "failed",
                "summary": f"agent exited without report (code {proc.returncode})",
                "error": log_tail[-500:],
            }
            break
        report = _read_report_file(report_path)
        # Only a TERMINAL report ends the wait while the agent still runs —
        # an incremental status:"running" write must not finalize the run
        # early (same contract as the doctor apply worker).
        if report is not None and report.get("status") in ("succeeded", "failed"):
            break
        time.sleep(3)
    if report is None or report.get("status") not in ("succeeded", "failed"):
        try:
            proc.terminate()
        except Exception:
            pass
        report = {
            "schema": "abyss-resolution/1", "role": "resolver", "status": "failed",
            "summary": f"agent timed out after {timeout_s // 60} min",
            "error": None,
        }
    _resolution_finalize(kind, obj_id, report_path, report)


def _dispatch_resolution(kind: str, obj_id: int) -> dict:
    """Dispatch a free-Nous Hermes agent to diagnose + fix a signal/incident."""
    _init_db()
    conn = _get_activity_conn()
    try:
        if kind == "signals":
            row = conn.execute("SELECT * FROM signals WHERE id = ?", (obj_id,)).fetchone()
        else:
            row = conn.execute("SELECT * FROM incidents WHERE id = ?", (obj_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return {"error": f"{kind[:-1]} {obj_id} not found", "code": 404}
    row = dict(row)
    if row.get("resolution_status") == "running":
        # A run left "running" by a crashed/restarted backend (its worker
        # thread died with the old process) would block re-dispatch forever —
        # the UI's resolve button would hang. Treat runs started longer than
        # the agent timeout (+2 min margin) as stale and allow a fresh run.
        started = row.get("resolution_started_at")
        stale = True
        if started:
            try:
                stale = (time.time() - datetime.fromisoformat(started).timestamp()) > (_AGENT_DEFAULT_TIMEOUT + 120)
            except (ValueError, TypeError):
                stale = True  # unparseable start = orphaned row
        if not stale:
            return {"status": "already_running", "kind": kind, "id": obj_id}

    report_id = f"{kind[:-1]}-{obj_id}-{int(time.time())}"
    report_path = _RESOLUTION_DIR / f"{report_id}.json"
    prompt = _build_resolver_prompt(kind, row, _resolution_context(kind, row), report_path)

    now = datetime.now().isoformat()
    _init_db()
    conn = _get_activity_conn()
    try:
        if kind == "signals":
            conn.execute(
                """UPDATE signals SET resolution_status = 'running', resolution_started_at = ?,
                       resolution_finished_at = NULL, resolution_note = NULL WHERE id = ?""",
                (now, obj_id),
            )
        else:
            conn.execute(
                """UPDATE incidents SET resolution_status = 'running', resolution_started_at = ?,
                       resolution_finished_at = NULL, resolution_note = NULL WHERE id = ?""",
                (now, obj_id),
            )
        conn.commit()
    finally:
        conn.close()
    _add_activity(
        action="resolution_dispatched",
        description=f"Agent dispatched to resolve {kind[:-1]} {obj_id}",
        category="system",
        metadata={"kind": kind, "obj_id": obj_id, "report_id": report_id},
    )
    threading.Thread(target=_resolution_worker, args=(kind, obj_id, report_path, prompt), daemon=True).start()
    return {"status": "dispatched", "kind": kind, "id": obj_id, "report_id": report_id}


# ---------------------------------------------------------------------------
# Doctor — full overarching diagnosis with user-approval-gated fixes
# ---------------------------------------------------------------------------

def _doctor_context() -> dict:
    """Everything the doctor agent needs to form an overarching diagnosis."""
    health = get_health()
    failures = get_failures(limit=10)
    _init_db()
    conn = _get_activity_conn()
    try:
        signals = [dict(r) for r in conn.execute(
            "SELECT * FROM signals WHERE resolved = 0 ORDER BY timestamp DESC LIMIT 30").fetchall()]
        incidents = [dict(r) for r in conn.execute(
            "SELECT * FROM incidents WHERE status IN ('open','acknowledged') ORDER BY timestamp DESC LIMIT 20").fetchall()]
        errors = [dict(r) for r in conn.execute(
            """SELECT id, timestamp, action, tool_name, status, metadata
               FROM activity WHERE status = 'error' ORDER BY timestamp DESC LIMIT 40""").fetchall()]
    finally:
        conn.close()
    return {
        "health": _redact(health),
        "failures": _redact(failures),
        "open_signals": [_redact(s) for s in signals],
        "open_incidents": [_redact(i) for i in incidents],
        "recent_errors": [_redact(e) for e in errors],
    }


def _doctor_worker(report_path: Path, prompt: str) -> None:
    """Background thread for the diagnosis phase (no DB changes)."""
    timeout_s = _AGENT_DEFAULT_TIMEOUT
    try:
        proc = _spawn_agent(prompt, report_path, role="doctor")
    except Exception as exc:
        logger.error("Abyss doctor spawn failed: %s", exc)
        _write_report_file(report_path, {
            "schema": "abyss-resolution/1", "role": "doctor", "status": "failed",
            "summary": f"agent spawn failed: {exc}", "error": str(exc),
        })
        return
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        if _read_report_file(report_path) is not None:
            break
        time.sleep(3)
    report = _read_report_file(report_path)
    if report is None:
        try:
            proc.terminate()
        except Exception:
            pass
        # Structural safety net: even a non-writing agent leaves a trace in
        # its captured stdout — surface what it FOUND so the run is never a
        # bare "timed out". The UI shows `error`/`summary` on failed reports.
        log_tail = ""
        try:
            log_path = report_path.with_suffix(".log")
            if log_path.exists():
                log_tail = log_path.read_text(encoding="utf-8", errors="replace")[-2500:]
        except Exception:
            log_tail = ""
        _write_report_file(report_path, {
            "schema": "abyss-resolution/1", "role": "doctor", "status": "failed",
            "summary": "doctor agent timed out before writing a report",
            "error": (log_tail.strip() or None),
        })
    _add_activity(
        action="doctor_completed",
        description="Doctor diagnosis ready",
        category="system",
        metadata={"report_id": report_path.stem},
    )
    try:
        from abyss_wave import emit_abyss_event

        emit_abyss_event("doctor_completed", {
            "report_id": report_path.stem,
            "status": (report or {}).get("status", "unknown"),
        })
    except Exception as exc:
        logger.debug("Abyss doctor_completed emit failed: %s", exc)


def _dispatch_doctor() -> dict:
    """Dispatch the doctor agent: full diagnosis + proposed fixes (no changes)."""
    report_id = f"doctor-{int(time.time())}"
    report_path = _RESOLUTION_DIR / f"{report_id}.json"
    context = _doctor_context()
    prompt = (
        "You are the Abyss doctor: an autonomous Hermes agent performing a full, overarching "
        "diagnosis of this Hermes installation's health.\n\n"
        "Load the 'abyss-doctor' skill and follow it exactly.\n\n"
        "FULL CONTEXT (JSON):\n" + json.dumps(context, indent=2)[:24000] + "\n\n"
        "TASK:\n"
        "1. Synthesize an overarching diagnosis: what is actually wrong, what is noise, and the "
        "root causes behind the open signals/incidents.\n"
        "2. Produce a prioritized list of proposed fixes. Each fix MUST carry target_signals / "
        "target_incidents ids so the backend can resolve them after the user approves.\n"
        "3. Write your report to ABYSS_REPORT_PATH as JSON with this exact schema:\n"
        '{"schema":"abyss-resolution/1","role":"doctor","report_id":"' + report_id + '",'
        '"status":"succeeded","summary":"...",'
        '"findings":[{"title":"...","detail":"...","evidence":"..."}],'
        '"proposed_fixes":[{"id":"fix-1","title":"...","action":"...","target_signals":[],"target_incidents":[]}]}\n'
        "4. WRITE THE REPORT INCREMENTALLY - do NOT leave it to the end. After your FIRST ~8 "
        "tool actions, write an initial report to ABYSS_REPORT_PATH (status \"running\" is "
        "acceptable) listing findings so far, then UPDATE the same file as you go. A partial "
        "report beats a perfect one that never gets written - if you run low on iterations "
        "the backend still sees your findings.\n"
        "5. TIME-BOX your investigation: at most ~15 tool actions total. Prefer breadth "
        "(taxonomy queries + code grep for the top error signatures, per the abyss-doctor "
        "skill's deep-diagnosis method) over deep dives into any single file. If you cannot "
        "finish, write a PARTIAL report with status \"succeeded\" and your findings so far.\n"
        "6. Your final chat response must be the one-line summary.\n"
        "Do NOT change anything yet — the user approves fixes before they are applied."
    )
    _add_activity(
        action="doctor_dispatched",
        description="Doctor agent dispatched for full diagnosis",
        category="system",
        metadata={"report_id": report_id},
    )
    threading.Thread(target=_doctor_worker, args=(report_path, prompt), daemon=True).start()
    return {"status": "dispatched", "report_id": report_id}


def _doctor_report(report_id: str) -> dict:
    """Poll endpoint: returns the doctor report once the agent has written it."""
    if not report_id or not re.fullmatch(r"[A-Za-z0-9._-]+", report_id):
        return {"status": "invalid", "error": "bad report_id"}
    report_path = _RESOLUTION_DIR / f"{report_id}.json"
    report = _read_report_file(report_path)
    if report is None:
        return {"status": "running", "report_id": report_id}
    return {"status": "ready", "report_id": report_id, "report": report}


def _run_benchmark() -> dict:
    """Run the Abyss Bench Layer 1 probe suite (deterministic, zero tokens).

    Invokes ``evals/abyssbench/runner.py probes --json`` from the hermes-agent
    tree and returns the per-probe results. Used by the health-tab benchmark
    button so a doctor's fixes are scored against the regression suite.
    """
    agent_root = Path(HERMES_HOME) / "hermes-agent"
    evals_dir = agent_root / "evals" / "abyssbench"
    if not evals_dir.exists():
        return {"status": "error", "error": f"abyssbench not found at {evals_dir}"}
    import subprocess
    env = os.environ.copy()
    env["HERMES_HOME"] = str(HERMES_HOME)
    env["HERMES_PROFILE_HOME"] = str(PROFILE_HOME)
    env["HERMES_AGENT_ROOT"] = str(agent_root)
    try:
        proc = subprocess.run(
            [sys.executable, str(evals_dir / "runner.py"), "probes", "--json"],
            cwd=str(agent_root), env=env, capture_output=True, text=True,
            timeout=180, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        return {"status": "error", "error": f"benchmark run failed: {exc}"}
    try:
        data = json.loads(proc.stdout or "{}")
    except Exception:
        data = {}
    data["status"] = "ok" if data.get("failed", 0) == 0 else "failures"
    data["stderr_tail"] = (proc.stderr or "")[-300:]
    return data


def _doctor_last() -> dict:
    """Return the most recent actionable report (resume support).

    Prefers the latest report that still has un-applied proposed fixes —
    either a completed doctor diagnosis (role=doctor) or a PARTIAL apply run
    (role=apply, status succeeded/failed, remaining proposed_fixes). Lets the
    UI load it without re-running the agent.
    """
    try:
        _RESOLUTION_DIR.mkdir(parents=True, exist_ok=True)
        candidates = []
        for f in _RESOLUTION_DIR.glob("doctor-*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            role = data.get("role")
            status = data.get("status")
            remaining = data.get("proposed_fixes") or []
            if role == "doctor" and status == "succeeded" and remaining:
                candidates.append((f.stat().st_mtime, f.stem, data))
            elif role == "apply" and status in ("succeeded", "failed") and remaining:
                candidates.append((f.stat().st_mtime, f.stem, data))
        if not candidates:
            return {"status": "none"}
        _, report_id, data = max(candidates, key=lambda x: x[0])
        return {"status": "ready", "report_id": report_id, "report": data}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def _doctor_apply_worker(report_path: Path, prompt: str) -> None:
    """Background thread for the apply phase: wait for the apply agent, then
    resolve every signal/incident the report says was actually fixed."""
    timeout_s = _AGENT_DEFAULT_TIMEOUT
    try:
        proc = _spawn_agent(prompt, report_path, role="apply")
    except Exception as exc:
        logger.error("Abyss doctor apply spawn failed: %s", exc)
        _write_report_file(report_path, {
            "schema": "abyss-resolution/1", "role": "apply", "status": "failed",
            "summary": f"apply agent spawn failed: {exc}", "error": str(exc),
        })
        return
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if proc.poll() is not None:
            break  # agent exited — finalize from whatever it wrote
        report = _read_report_file(report_path)
        # IMPORTANT: only a TERMINAL report (the agent marking itself done)
        # ends the wait. The agent writes INCREMENTALLY (fixes[] grows one by
        # one, status stays "in_progress") — breaking on the first partial
        # fixes[] finalized the run at 2/8 fixes (see the 08:07 run).
        if (
            isinstance(report, dict)
            and report.get("role") == "apply"
            and report.get("status") in ("succeeded", "failed")
        ):
            break
        time.sleep(3)
    report = _read_report_file(report_path)
    if report is None:
        try:
            proc.terminate()
        except Exception:
            pass
        _write_report_file(report_path, {
            "schema": "abyss-resolution/1", "role": "apply", "status": "failed",
            "summary": "apply agent timed out", "error": None,
        })
        return
    fixes = report.get("fixes")
    if not isinstance(fixes, list) or not fixes:
        # The agent exited WITHOUT writing fix outcomes (iteration cap, error,
        # or a lost final write). Write a TERMINAL failed state so the UI poll
        # can leave "applying…" — otherwise the report keeps showing the
        # doctor's status:succeeded and the dashboard polls forever.
        _write_report_file(report_path, {
            **report,
            "role": "apply",
            "status": "failed",
            "summary": "apply agent exited without recording fix outcomes (iteration cap / error); "
                       "the report was not updated with a fixes[] array",
            "error": "agent exited without writing fixes[]",
            "fixes": [],
        })
        _add_activity(
            action="doctor_applied",
            description="Doctor apply finished: 0 fixes recorded (agent exited without report update)",
            category="system",
            status="error",
            metadata={"report_id": report_path.stem, "applied": 0, "total": 0, "terminal": "failed"},
        )
        return
    applied = 0
    for fix in fixes:
        if fix.get("status") == "applied":
            applied += 1
            note = (fix.get("note") or fix.get("title") or "")[:2000]
            for sid in (fix.get("target_signals") or []):
                _mark_resolution("signals", sid, "succeeded", note)
            for iid in (fix.get("target_incidents") or []):
                _mark_resolution("incidents", iid, "succeeded", note)
    _add_activity(
        action="doctor_applied",
        description=f"Doctor apply finished: {applied}/{len(fixes)} fixes applied",
        category="system",
        status="completed" if applied == len(fixes) else "error",
        metadata={"report_id": report_path.stem, "applied": applied, "total": len(fixes)},
    )


def _dispatch_doctor_apply(report_id: str, body: dict) -> dict:
    """Apply the approved fixes from a doctor report (agent does the work)."""
    if not report_id or not re.fullmatch(r"[A-Za-z0-9._-]+", report_id):
        return {"error": "invalid report_id", "code": 400}
    report_path = _RESOLUTION_DIR / f"{report_id}.json"
    report = _read_report_file(report_path)
    if report is None or report.get("status") != "succeeded":
        return {"error": "doctor report not found or not ready", "code": 404}
    fixes = report.get("proposed_fixes") or []
    if not fixes:
        return {"error": "doctor report has no proposed fixes", "code": 400}
    approved = body.get("fix_ids") or [f.get("id") for f in fixes]
    selected = [f for f in fixes if f.get("id") in approved]
    if not selected:
        return {"error": "no approved fixes", "code": 400}
    prompt = (
        "You are the Abyss doctor apply phase. The user has APPROVED the following fixes.\n\n"
        "Load the 'abyss-doctor' skill and follow it exactly.\n\n"
        "APPROVED FIXES (JSON):\n" + json.dumps(_redact({"report_id": report_id, "fixes": selected}), indent=2)[:16000] + "\n\n"
        "TASK:\n"
        "1. Apply each approved fix on the backend. Use tools, edit files/configs, restart "
        "processes as needed. Verify each fix actually worked.\n"
        "2. For every fix that is reusable, save a skill named abyss-fix-<pattern> documenting it.\n"
        "3. UPDATE the existing report file at ABYSS_REPORT_PATH (same file) so it now has:\n"
        '{"fixes":[{"id":"fix-1","status":"applied|skipped|failed","note":"...","skill_saved":"...","target_signals":[],"target_incidents":[]}],'
        '"summary":"outcome","status":"succeeded|failed"}\n'
        "Keep the rest of the report intact (findings, proposed_fixes).\n"
        "4. WRITE THE REPORT INCREMENTALLY - do NOT leave it to the end. As soon as each "
        "fix is applied and verified, append its outcome to the fixes[] array in "
        "ABYSS_REPORT_PATH and save. If you run low on iterations, the partial fixes[] "
        "still unblocks the backend and the dashboard. A final save after all fixes is "
        "optional, not required.\n"
        "5. Your final chat response must be the one-line outcome summary.\n"
        "Do NOT touch the Abyss SQLite databases — the backend resolves signals/incidents from your report."
    )
    _add_activity(
        action="doctor_approve_dispatched",
        description=f"Apply agent dispatched for {len(selected)} approved fix(es)",
        category="system",
        metadata={"report_id": report_id, "fix_ids": approved},
    )
    # Reset the report to a NON-terminal apply state BEFORE the worker starts.
    # Re-approving a report that already has status succeeded/failed would make
    # the worker's terminal check fire instantly and finalize at 0 new fixes.
    report = _read_report_file(report_path) or {}
    _write_report_file(report_path, {
        **report,
        "role": "apply",
        "status": "in_progress",
        "summary": (report.get("summary") or "Apply phase started."),
    })
    threading.Thread(target=_doctor_apply_worker, args=(report_path, prompt), daemon=True).start()
    return {"status": "dispatched", "report_id": report_id, "fix_count": len(selected)}


def _cluster_incidents(alert: bool = True) -> list:
    """Group related signals into incidents (Raindrop pattern).

    Clustering rules:
    - 2+ signals in the same session (any type) -> incident
    - 3+ signals of the same type across sessions within a 60-minute window -> incident
    - Same pattern + session already has an open incident -> merge into it
      (bump signal_count, extend signal_ids)
    Returns a list of incident IDs created or updated.

    ``alert`` controls webhook alerting for newly-created incidents; pass
    False for startup maintenance so first-boot clustering doesn't spam.
    """
    _init_db()
    conn = _get_activity_conn()
    touched = []
    try:
        signal_rows = conn.execute("""
            SELECT id, session_id, signal_type, severity, timestamp
            FROM signals WHERE resolved = 0 AND acknowledged = 0 AND incident_id IS NULL
            ORDER BY timestamp DESC
        """).fetchall()

        if not signal_rows:
            return touched

        # Group 1: same session, 2+ signals
        session_groups: Dict[str, list] = {}
        for row in signal_rows:
            sid = row["session_id"] or "unknown"
            session_groups.setdefault(sid, []).append(dict(row))

        for sid, signals in session_groups.items():
            if len(signals) < 2:
                continue
            # Look for an existing open incident for this session+pattern
            existing = conn.execute(
                "SELECT id FROM incidents WHERE status = 'open' AND session_ids = ? AND pattern = ?",
                (sid, "multi_signal"),
            ).fetchone()
            signal_ids = [s["id"] for s in signals]
            max_sev = max((_SEVERITY_RANK.get(s["severity"], 1) for s in signals), default=1)
            max_sev_label = next((k for k, v in sorted(_SEVERITY_RANK.items(), key=lambda kv: kv[1], reverse=True) if v <= max_sev), "warning")
            types = sorted({s["signal_type"] for s in signals})
            if existing:
                conn.execute(
                    """UPDATE incidents SET signal_count = signal_count + ?, signal_ids = ?
                       WHERE id = ?""",
                    (len(signals), json.dumps(signal_ids), existing["id"]),
                )
                incident_id = existing["id"]
            else:
                cursor = conn.execute(
                    """INSERT INTO incidents
                       (timestamp, title, description, severity, signal_count, session_ids, pattern, status, created_at, signal_ids)
                       VALUES (?, ?, ?, ?, ?, ?, 'multi_signal', 'open', ?, ?)""",
                    (
                        datetime.now().isoformat(),
                        f"Signal cluster: {len(signals)} signals in session {str(sid)[:8]}",
                        f"Multiple signals detected: {', '.join(types)}",
                        max_sev_label,
                        len(signals),
                        sid,
                        datetime.now().isoformat(),
                        json.dumps(signal_ids),
                    )
                )
                incident_id = cursor.lastrowid
                if alert:
                    new_row = conn.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
                    if new_row:
                        _alert_on_incident(dict(new_row))
            conn.execute("UPDATE signals SET incident_id = ? WHERE id IN (%s)" % ",".join("?" * len(signal_ids)),
                         [incident_id] + signal_ids)
            conn.commit()
            touched.append(incident_id)

        # Group 2: same signal_type across sessions within a 60-min window (3+)
        type_windows: Dict[str, list] = {}
        for row in signal_rows:
            type_windows.setdefault(row["signal_type"], []).append(dict(row))

        for stype, signals in type_windows.items():
            if len(signals) < 3 or stype in ("self_diagnostic",):
                continue
            # Group by contiguous time windows (sorted ascending)
            ordered = sorted(signals, key=lambda s: s["timestamp"])
            windows = []
            current = []
            for s in ordered:
                if not current:
                    current = [s]
                    continue
                try:
                    prev_ts = datetime.fromisoformat(current[-1]["timestamp"])
                    cur_ts = datetime.fromisoformat(s["timestamp"])
                    gap = (cur_ts - prev_ts).total_seconds()
                except (ValueError, TypeError):
                    gap = 0
                if gap <= 3600:
                    current.append(s)
                else:
                    windows.append(current)
                    current = [s]
            if current:
                windows.append(current)

            for win in windows:
                if len(win) < 3:
                    continue
                signal_ids = [s["id"] for s in win]
                max_sev = max((_SEVERITY_RANK.get(s["severity"], 1) for s in win), default=1)
                max_sev_label = next((k for k, v in sorted(_SEVERITY_RANK.items(), key=lambda kv: kv[1], reverse=True) if v <= max_sev), "warning")
                sessions = sorted({s["session_id"] or "unknown" for s in win})
                existing = conn.execute(
                    "SELECT id FROM incidents WHERE status = 'open' AND pattern = ? AND session_ids = ?",
                    (f"{stype}_burst", ",".join(sessions)),
                ).fetchone()
                if existing:
                    conn.execute(
                        "UPDATE incidents SET signal_count = signal_count + ?, signal_ids = ? WHERE id = ?",
                        (len(signal_ids), json.dumps(signal_ids), existing["id"]),
                    )
                    incident_id = existing["id"]
                else:
                    cursor = conn.execute(
                        """INSERT INTO incidents
                           (timestamp, title, description, severity, signal_count, session_ids, pattern, status, created_at, signal_ids)
                           VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)""",
                        (
                            datetime.now().isoformat(),
                            f"Signal burst: {len(signal_ids)}x '{stype}' across {len(sessions)} session(s)",
                            f"Repeated {stype} signals within a 60-minute window",
                            max_sev_label,
                            len(signal_ids),
                            ",".join(sessions),
                            f"{stype}_burst",
                            datetime.now().isoformat(),
                            json.dumps(signal_ids),
                        )
                    )
                    incident_id = cursor.lastrowid
                    if alert:
                        new_row = conn.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
                        if new_row:
                            _alert_on_incident(dict(new_row))
                conn.execute("UPDATE signals SET incident_id = ? WHERE id IN (%s)" % ",".join("?" * len(signal_ids)),
                             [incident_id] + signal_ids)
                conn.commit()
                touched.append(incident_id)
    finally:
        conn.close()
    return list(dict.fromkeys(touched))


def _prune_data(days: int = 30) -> dict:
    """Delete activity/traces/signals/incidents older than ``days``.

    Returns counts of deleted rows per table. ``days <= 0`` is a no-op.
    """
    if days <= 0:
        return {"activity": 0, "traces": 0, "signals": 0, "incidents": 0}
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
    return counts


# ---------------------------------------------------------------------------
# Analytics: health score, trends, failure taxonomy, export, status
# ---------------------------------------------------------------------------

def get_health() -> dict:
    """Compute an overall agent health score (0-100) and a breakdown.

    Modeled on Raindrop-style health panels:
      - error rate      (tool/LLM failures vs total activity) — 40 pts
      - open signals    (unacknowledged anomalies)             — 25 pts
      - open incidents  (unresolved clusters)                  — 25 pts
      - recent activity (24h liveliness / starvation guard)    — 10 pts
    """
    _init_db()
    conn = _get_activity_conn()
    try:
        total = conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0]
        errors = conn.execute("SELECT COUNT(*) FROM activity WHERE status = 'error'").fetchone()[0]
        # Recency window: a backlog of old signals/incidents (e.g. a noisy cron
        # job) must not pin the score at "critical" forever. Only signals from
        # the last 7 days count against health.
        week_ago = (datetime.now() - timedelta(days=7)).isoformat()
        signal_open = conn.execute(
            "SELECT COUNT(*) FROM signals WHERE resolved = 0 AND timestamp >= ?",
            (week_ago,),
        ).fetchone()[0]
        incident_open = conn.execute(
            "SELECT COUNT(*) FROM incidents WHERE status IN ('open', 'acknowledged') AND created_at >= ?",
            (week_ago,),
        ).fetchone()[0]
        day_ago = (datetime.now() - timedelta(days=1)).isoformat()
        activity_24h = conn.execute(
            "SELECT COUNT(*) FROM activity WHERE timestamp >= ?", (day_ago,)
        ).fetchone()[0]

        # Error rate: 0% -> 40, 50%+ -> 0
        err_ratio = (errors / total) if total else 0.0
        error_score = max(0.0, 40.0 * (1.0 - min(1.0, err_ratio / 0.5)))

        # Signals: 0 open (7d) -> 25, 100+ open (7d) -> 0. Softer than the old
        # threshold of 20 — with per-tool-error recording, 20 open signals is
        # normal agent activity, not a health catastrophe.
        signal_score = max(0.0, 25.0 * (1.0 - min(1.0, signal_open / 100.0)))

        # Incidents: 0 open (7d) -> 25, 20+ open (7d) -> 0
        incident_score = max(0.0, 25.0 * (1.0 - min(1.0, incident_open / 20.0)))

        # Liveliness: >= 10 activities in 24h -> 10, none -> 0
        activity_score = max(0.0, 10.0 * min(1.0, activity_24h / 10.0))

        score = round(error_score + signal_score + incident_score + activity_score, 1)
        if score >= 90:
            level = "healthy"
        elif score >= 70:
            level = "fair"
        elif score >= 50:
            level = "degraded"
        else:
            level = "critical"

        return {
            "score": score,
            "level": level,
            "components": {
                "error_rate": round(err_ratio, 4),
                "error_score": round(error_score, 1),
                "signal_score": round(signal_score, 1),
                "incident_score": round(incident_score, 1),
                "activity_score": round(activity_score, 1),
            },
            "counts": {
                "total_activities": total,
                "errors": errors,
                "signals_open": signal_open,
                "incidents_open": incident_open,
                "activity_24h": activity_24h,
            },
            "generated_at": datetime.now().isoformat(),
        }
    finally:
        conn.close()


def get_trends(days: int = 7, bucket: str = "day") -> dict:
    """Bucket activity, errors, signals and incidents over a time window.

    ``bucket`` is one of "hour" | "day". Returns parallel arrays of
    timestamps + counts suitable for sparkline/bar rendering.
    """
    _init_db()
    conn = _get_activity_conn()
    try:
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        bucket_fmt = "%Y-%m-%d" if bucket == "day" else "%Y-%m-%d %H:00"

        activity_rows = conn.execute(
            "SELECT timestamp FROM activity WHERE timestamp >= ?", (cutoff,)
        ).fetchall()
        error_rows = conn.execute(
            "SELECT timestamp FROM activity WHERE timestamp >= ? AND status = 'error'", (cutoff,)
        ).fetchall()
        signal_rows = conn.execute(
            "SELECT timestamp FROM signals WHERE timestamp >= ?", (cutoff,)
        ).fetchall()
        incident_rows = conn.execute(
            "SELECT timestamp FROM incidents WHERE timestamp >= ?", (cutoff,)
        ).fetchall()

        def _bucket(rows):
            counts = {}
            for r in rows:
                try:
                    key = datetime.fromisoformat(r["timestamp"]).strftime(bucket_fmt)
                except (ValueError, TypeError):
                    key = (r["timestamp"] or "")[:16]
                counts[key] = counts.get(key, 0) + 1
            return counts

        # Build a contiguous series of buckets between cutoff and now
        buckets = []
        step = timedelta(hours=1) if bucket == "hour" else timedelta(days=1)
        cur = datetime.fromisoformat(cutoff[:19])
        now = datetime.now()
        while cur <= now:
            buckets.append(cur.strftime(bucket_fmt))
            cur += step

        a = _bucket(activity_rows)
        e = _bucket(error_rows)
        s = _bucket(signal_rows)
        i = _bucket(incident_rows)

        return {
            "bucket": bucket,
            "days": days,
            "timestamps": buckets,
            "activity": [a.get(b, 0) for b in buckets],
            "errors": [e.get(b, 0) for b in buckets],
            "signals": [s.get(b, 0) for b in buckets],
            "incidents": [i.get(b, 0) for b in buckets],
        }
    finally:
        conn.close()


def get_failures(limit: int = 15) -> dict:
    """Root-cause taxonomy: most frequent signal types, failing tools,
    and common error messages (normalized)."""
    _init_db()
    conn = _get_activity_conn()
    try:
        signal_rows = conn.execute(
            """SELECT s.signal_type, s.label, s.description, s.severity,
                      a.tool_name AS tool_name
               FROM signals s
               LEFT JOIN activity a ON a.id = s.activity_id
               ORDER BY s.timestamp DESC LIMIT 500"""
        ).fetchall()
        err_rows = conn.execute(
            "SELECT metadata, tool_name FROM activity WHERE status = 'error' ORDER BY timestamp DESC LIMIT 500"
        ).fetchall()

        type_counts: Dict[str, int] = {}
        tool_counts: Dict[str, int] = {}
        msg_counts: Dict[str, int] = {}

        for s in signal_rows:
            type_counts[s["signal_type"]] = type_counts.get(s["signal_type"], 0) + 1

        for r in err_rows:
            tool = (r["tool_name"] or "unknown").strip()
            if tool:
                tool_counts[tool] = tool_counts.get(tool, 0) + 1
            meta = {}
            try:
                meta = json.loads(r["metadata"] or "{}")
            except (ValueError, TypeError):
                pass
            msg = (meta.get("error_message") or "").strip()
            if msg:
                # Normalize: lowercase, trim to a fingerprint
                norm = " ".join(msg.lower().split())[:120]
                msg_counts[norm] = msg_counts.get(norm, 0) + 1

        return {
            "by_type": [{"type": k, "count": v} for k, v in
                        sorted(type_counts.items(), key=lambda kv: kv[1], reverse=True)[:limit]],
            "by_tool": [{"tool": k, "count": v} for k, v in
                        sorted(tool_counts.items(), key=lambda kv: kv[1], reverse=True)[:limit]],
            "by_message": [{"message": k, "count": v} for k, v in
                           sorted(msg_counts.items(), key=lambda kv: kv[1], reverse=True)[:limit]],
        }
    finally:
        conn.close()


def export_data() -> dict:
    """Full JSON snapshot of all Abyss tables — for backup/migration."""
    _init_db()
    conn = _get_activity_conn()
    try:
        def dump(table):
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
            return [dict(r) for r in rows]
        data = {
            "exported_at": datetime.now().isoformat(),
            "activity": dump("activity"),
            "signals": dump("signals"),
            "incidents": dump("incidents"),
        }
    finally:
        conn.close()
    tconn = _get_trace_conn()
    try:
        data["traces"] = [dict(r) for r in tconn.execute("SELECT * FROM traces").fetchall()]
    finally:
        tconn.close()
    return data


def get_status() -> dict:
    """Lightweight status summary for the desktop statusbar chip / polling."""
    _init_db()
    conn = _get_activity_conn()
    try:
        signal_open = conn.execute("SELECT COUNT(*) FROM signals WHERE resolved = 0").fetchone()[0]
        signal_total = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        incident_open = conn.execute("SELECT COUNT(*) FROM incidents WHERE status IN ('open', 'acknowledged')").fetchone()[0]
        day_ago = (datetime.now() - timedelta(days=1)).isoformat()
        activity_24h = conn.execute(
            "SELECT COUNT(*) FROM activity WHERE timestamp >= ?", (day_ago,)
        ).fetchone()[0]
        # Real severity breakdown of OPEN signals (the UI must not sample a
        # limited signal list and present it as totals — it used to fetch
        # /signals?limit=50 and show "50 SIG / 43 critical" when 800+ existed).
        severity_counts = {"critical": 0, "error": 0, "warning": 0, "info": 0}
        for sev, count in conn.execute(
            "SELECT severity, COUNT(*) FROM signals WHERE resolved = 0 GROUP BY severity"
        ).fetchall():
            sev = (sev or "info").lower()
            if sev in severity_counts:
                severity_counts[sev] = count
            else:
                severity_counts["info"] += count
        resolutions_running = conn.execute(
            "SELECT COUNT(*) FROM signals WHERE resolution_status = 'running'"
        ).fetchone()[0] + conn.execute(
            "SELECT COUNT(*) FROM incidents WHERE resolution_status = 'running'"
        ).fetchone()[0]
    finally:
        conn.close()
    health = get_health()
    return {
        "score": health["score"],
        "level": health["level"],
        "signals_open": signal_open,
        "signals_total": signal_total,
        "signals_critical": severity_counts["critical"],
        "signals_error": severity_counts["error"],
        "signals_warning": severity_counts["warning"],
        "signals_info": severity_counts["info"],
        "incidents_open": incident_open,
        "activity_24h": activity_24h,
        "resolutions_running": resolutions_running,
        "generated_at": datetime.now().isoformat(),
    }


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




def list_activity(limit: int = 50, category: Optional[str] = None, since: Optional[str] = None):
    """List activity feed entries."""
    _init_db()
    conn = _get_activity_conn()
    query = "SELECT * FROM activity"
    params = []

    if since:
        query += " WHERE timestamp >= ?"
        params.append(since)

    if category:
        if "WHERE" in query:
            query += " AND category = ?"
        else:
            query += " WHERE category = ?"
        params.append(category)

    query += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_session_trace(session_id: str, limit: int = 200):
    """Get the full trace/timeline for a session."""
    _init_db()
    conn = _get_trace_conn()
    rows = conn.execute(
        """SELECT * FROM traces
           WHERE session_id = ?
           ORDER BY timestamp ASC LIMIT ?""",
        (session_id, limit)
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


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

    conn.close()

    # Also pull from Hermes state.db for memory nodes
    state_db = os.path.join(PROFILE_HOME, "state.db")
    if os.path.exists(state_db):
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

            sconn.close()
        except Exception as e:
            logger.debug("Graph: state.db read failed: %s", e)

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
            start = params.get("start", datetime.now().isoformat())
            end = params.get("end", (datetime.now() + timedelta(days=7)).isoformat())
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

        elif path == "/export" and method == "GET":
            return export_data()

        elif path == "/status" and method == "GET":
            return get_status()

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
            query = "SELECT * FROM signals"
            query_params = []
            if session_filter:
                query += " WHERE session_id = ?"
                query_params.append(session_filter)
            query += " ORDER BY timestamp DESC LIMIT ?"
            query_params.append(limit)
            conn = _get_activity_conn()
            rows = conn.execute(query, query_params).fetchall()
            conn.close()
            return [dict(row) for row in rows]

        elif path == "/incidents" and method == "GET":
            limit = _int_param(params, "limit", 50)
            status_filter = params.get("status")
            query = "SELECT * FROM incidents"
            query_params = []
            if status_filter:
                query += " WHERE status = ?"
                query_params.append(status_filter)
            query += " ORDER BY timestamp DESC LIMIT ?"
            query_params.append(limit)
            conn = _get_activity_conn()
            rows = conn.execute(query, query_params).fetchall()
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

    # Read cron jobs from Hermes cron directory
    cron_dir = Path(PROFILE_HOME) / "cron"
    if cron_dir.exists():
        for f in cron_dir.iterdir():
            if f.is_file() and f.suffix == ".json":
                try:
                    data = json.loads(f.read_text())
                    schedule = data.get("schedule", "")
                    prompt = data.get("prompt", "")
                    results.append({
                        "id": f.stem,
                        "title": data.get("name", f.stem),
                        "description": prompt[:100] + "..." if len(prompt) > 100 else prompt,
                        "schedule": schedule,
                        "next_run": data.get("next_run", ""),
                        "enabled": data.get("enabled", True),
                        "deliver": data.get("deliver", "origin"),
                        "category": "cron",
                    })
                except Exception:
                    continue

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

    # 2. Search Hermes state.db
    state_db = os.path.join(PROFILE_HOME, "state.db")
    if os.path.exists(state_db):
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
                    kconn.close()
                except Exception:
                    pass

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
    rows = conn.execute(
        """SELECT DISTINCT session_id, MIN(timestamp) as first_ts,
                  MAX(timestamp) as last_ts, COUNT(*) as count
           FROM activity WHERE session_id IS NOT NULL
           GROUP BY session_id
           ORDER BY last_ts DESC LIMIT ?""",
        (limit,)
    ).fetchall()
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

    # Add trace data for each session
    tconn = _get_trace_conn()
    for session in sessions:
        sid = session["session_id"]
        traces = tconn.execute(
            "SELECT * FROM traces WHERE session_id = ? ORDER BY timestamp ASC LIMIT 100",
            (sid,)
        ).fetchall()
        session["traces"] = [dict(t) for t in traces]
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
  search <query>              Search activity, memories, sessions
  trace <session>            Show trace timeline for a session
  signals [--session=<sid>]  Show detected signals
  incidents [--status=<st>]  Show incidents
  ack <signal_id>            Acknowledge a signal
  resolve <signal_id>        Resolve a signal
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
"""

    sub = argv[0]

    if sub == "recent":
        n = 10
        category = None
        for arg in argv[1:]:
            if arg.startswith("--category="):
                category = arg.split("=", 1)[1]
            elif arg.isdigit():
                n = int(arg)

        activities = list_activity(limit=n, category=category)
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
        for arg in argv[1:]:
            if arg.startswith("--limit="):
                limit = int(arg.split("=", 1)[1])
            elif arg.startswith("--session="):
                session_arg = arg.split("=", 1)[1]

        conn = _get_activity_conn()
        query = "SELECT * FROM signals"
        params = []
        if session_arg:
            query += " WHERE session_id = ?"
            params.append(session_arg)
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
            lines.append(f"  [{ack}{res}] [{r['timestamp'][:19]}] {r['severity'].upper()}: {r['label']} — {r['description'][:80]}")
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

    if sub == "export":
        """Dump all data as JSON: /abyss export"""
        data = export_data()
        return json.dumps({
            "activity": len(data["activity"]),
            "signals": len(data["signals"]),
            "incidents": len(data["incidents"]),
            "traces": len(data["traces"]),
            "exported_at": data["exported_at"],
        })

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

        return "✓ All activity, signals, incidents, and trace data cleared."

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
