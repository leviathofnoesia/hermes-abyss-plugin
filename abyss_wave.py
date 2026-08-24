"""Abyss Wave — August 2026 plugin-interface expansion module.

Implements the new plugin surfaces that landed in the Hermes plugin
expansion wave (tracking issue NousResearch/hermes-agent#64182, tracker
comment 5275487630), wired into the Abyss observability plugin:

  - Manifest v2 (#64165)         declared in plugin.yaml (emits/listens,
                                 config_schema, license, homepage, tags)
  - Event bus (#64164)           ctx.emit() -> ``abyss:<event>`` with a
                                 durable audit row in the ``plugin_events``
                                 table (visible even with zero subscribers)
  - Lifecycle hooks (26+ wave)   on_session_reset/finalize, streaming
                                 (on_stream_start/delta/end), pre/post
                                 api_request, api_request_error,
                                 subagent_start/stop, pre/post approval,
                                 gateway_platform_event, pre_command,
                                 on_skill_lifecycle
  - Capabilities (#64228)        the manifest declares NO privileged
                                 capabilities (fail-closed default); all of
                                 this module's hooks are observers only
  - Config/state bridge (#64227) ctx.get_config/set_config for
                                 retention_days + webhook_url settings
                                 (env-var fallbacks preserved)
  - Redaction registry (#65449)  ctx.register_redaction_patterns() plus a
                                 local mask pass on stored payloads
  - Ownership ledger (#64229)    ctx.on_unload() clears ctx/stream state so
                                 force-reload (#64178) leaves no zombies
  - Plugin Doctor (#64230)       manifest/registration stay doctor-clean;
                                 `hermes plugins doctor` is a verification step

The module is deliberately self-contained: it imports the core lazily inside
functions so there is no import cycle with ``__init__``, and every hook
handler is an observer that fails open (isolated try/except per callback is
guaranteed by the host invoke_hook path; we still guard the DB writes).

New tables (created in activity.db): plugin_events, streams, api_requests,
subagents, approvals, commands, platform_events, skills.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger("hermes.plugins.abyss.wave")

# ---------------------------------------------------------------------------
# Context + settings bridge
# ---------------------------------------------------------------------------

# Set by wave_register(ctx). Emits are no-ops when None (e.g. test suites).
_CTX: Any = None

# Settings resolved from ctx.get_config() with env-var fallbacks.
SETTINGS: Dict[str, Any] = {
    "retention_days": int(os.environ.get("ABYSS_RETENTION_DAYS", "30") or 30),
    "webhook_url": os.environ.get("ABYSS_WEBHOOK_URL", ""),
    "stream_signals": True,
    "stream_signal_coalesce_minutes": int(
        os.environ.get("ABYSS_STREAM_COALESCE_MINUTES", "10") or 10
    ),
}

# Redaction patterns registered with the host redaction engine (#65449) and
# applied locally to stored payloads. Each must start with >= 2 literal chars.
_REDACTION_PATTERNS = [
    r"nvapi-[A-Za-z0-9_-]{20,}",
    r"sk-[A-Za-z0-9]{16,}",
    r"gh[pousr]_[A-Za-z0-9]{36,}",
    r"AKIA[0-9A-Z]{16}",
    r"Bearer\s+[A-Za-z0-9._~+/=-]{20,}",
]
_COMPILED_PATTERNS = [re.compile(p) for p in _REDACTION_PATTERNS]

# In-memory stream accumulator keyed by (session_id, turn_id). Bounded so a
# pathological stream cannot grow memory; on_stream_end pops the entry.
_STREAMS: Dict[tuple, Dict[str, Any]] = {}
_STREAM_MAX = 512
_STREAM_LOCK = threading.Lock()

# Keys whose in-memory accumulator was evicted at the _STREAM_MAX cap. When
# their late on_stream_end arrives (accumulator already gone), the stream row
# is STILL persisted but flagged as telemetry-incomplete, and NO empty_stream
# signal is fired — eviction is a tracking limit, not evidence the stream
# produced zero tokens. Without this, a healthy stream evicted under heavy
# concurrency (>512 parallel streams: subagent storms, multi-gateway) was
# re-created as a chars=0 placeholder by its own on_stream_end and fired a
# FALSE empty_stream signal that polluted the Watch tab and pinned the health
# score. Ends remove entries promptly; the cap trims the set if it ever grows
# past _STREAM_MAX * 2 (i.e. ends never arrive for a pathological burst).
_EVICTED: set = set()

# A stream accumulator that never received on_stream_end within this many
# seconds is presumed stuck (provider hang, dropped hook, killed process).
# _on_stream_start sweeps these and persists them as finished=0 rows PLUS a
# `stuck_stream` signal instead of letting them vanish silently at the cap.
_STREAM_STUCK_AFTER = 900.0

# Backoff (seconds) between the two attempts _wave_with_retry makes when the
# activity DB reports 'database is locked' — a transient cross-process WAL
# writer collision. busy_timeout=5000ms usually resolves the contention on its
# own; one retry with a short sleep absorbs the rare longer collision without
# resorting to a much larger busy_timeout that would block the hook hot path.
_LOCK_RETRY_SLEEP = 0.15


def _is_lock_error(exc: Exception) -> bool:
    """True for SQLite 'database is locked' / 'database table is locked'.

    These are transient, expected races under concurrent cross-process access
    (the live Hermes gateway + the dashboard webserver + cron scripts all share
    one activity.db). They are NOT operator-actionable and must not spam the
    logs at WARNING level — see _wave_with_retry / the write helpers below.
    """
    if isinstance(exc, sqlite3.OperationalError):
        msg = str(exc).lower()
        return "locked" in msg
    return False


_SEVERITY_RANK = {"info": 0, "warning": 1, "error": 2, "critical": 3, "fatal": 4}


def _severity_rank(severity: Any) -> int:
    """Rank a severity string for coalescing escalation; unknown -> warning."""
    return _SEVERITY_RANK.get(str(severity or "").lower(), 1)


def _wave_with_retry(op: str, table: str, runnable):
    """Run a single-statement DB write, retrying 'database is locked' once.

    Args:
        op: short label for the log message ("insert"/"update"/"signal").
        table: target wave table name, for the log message.
        runnable: callable(conn) -> result that performs the execute+commit on
            ``conn`` and returns whatever the caller wants to surface (lastrowid
            for inserts, rowcount>0 bool for updates). It must NOT close the
            connection — this helper owns the connection lifecycle.

    Transient lock contention is logged at DEBUG (not WARNING): cross-process
    writers on a shared WAL database collide regularly and the fail-open
    contract means the loss of one audit row is acceptable. Genuine programming
    errors (schema mismatch, malformed SQL) still surface at WARNING.
    """
    from __init__ import _get_activity_conn

    _ensure_tables()
    last_exc = None
    for attempt in (1, 2):
        conn = None
        try:
            conn = _get_activity_conn()
            return runnable(conn)
        except Exception as exc:
            last_exc = exc
            is_lock = _is_lock_error(exc)
            if attempt == 1 and is_lock:
                # Close this connection before sleeping so we don't hold a
                # half-open transaction across the backoff.
                continue
            # Either we already retried, or this is a non-lock failure.
            if is_lock:
                logger.debug(
                    "Abyss wave %s into %s deferred (database locked after retry): %s",
                    op, table, exc,
                )
            else:
                logger.warning("Abyss wave %s into %s failed: %s", op, table, exc)
            return None
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            if last_exc is not None and _is_lock_error(last_exc) and attempt == 1:
                time.sleep(_LOCK_RETRY_SLEEP)


def _mask_secrets(value: str) -> str:
    """Mask known secret formats in a stored string (local redaction pass)."""
    if not value:
        return value
    masked = value
    for pat in _COMPILED_PATTERNS:
        masked = pat.sub("***", masked)
    return masked


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

_WAVE_TABLES = (
    "plugin_events",
    "streams",
    "api_requests",
    "subagents",
    "approvals",
    "commands",
    "platform_events",
    "skills",
)


def prune_wave_data(days: int = 30) -> dict:
    """Delete wave-table rows older than ``days`` from the activity DB.

    Every wave surface (plugin_events, streams, api_requests, subagents,
    approvals, commands, platform_events, skills) appends a row per event and
    all of them share the activity DB ``timestamp`` column. The core
    ``_prune_data`` used to ignore these tables, so ``retention_days`` was
    silently no-op for the Aug-2026 wave surfaces and the tables grew
    unbounded. ``_prune_data`` calls this so retention applies everywhere.

    Returns per-table deleted-row counts; ``days <= 0`` is a no-op returning
    zero counts (same contract as the core pruner). Fail-open: any DB error
    returns the partial counts and never raises into a hook/command path.
    """
    counts = {table: 0 for table in _WAVE_TABLES}
    if days <= 0:
        return counts
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    try:
        from __init__ import _get_activity_conn

        conn = _get_activity_conn()
        try:
            for table in _WAVE_TABLES:
                cur = conn.execute(
                    f"DELETE FROM {table} WHERE timestamp < ?", (cutoff,)
                )
                counts[table] = cur.rowcount
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.debug("Abyss wave prune failed: %s", exc)
    return counts


def clear_wave_data() -> dict:
    """Delete every wave-table row (irreversible, mirrors ``/abyss clean``).

    ``/abyss clean`` previously cleared activity/signals/incidents/traces but
    left the wave tables intact, so a "clear all data" left stream/API/approval
    history behind. The clean command calls this so the wipe is actually a wipe.
    """
    counts = {table: 0 for table in _WAVE_TABLES}
    try:
        from __init__ import _get_activity_conn

        conn = _get_activity_conn()
        try:
            for table in _WAVE_TABLES:
                cur = conn.execute(f"DELETE FROM {table}")
                counts[table] = cur.rowcount
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.debug("Abyss wave clear failed: %s", exc)
    return counts


def wave_init_db(conn) -> None:
    """Create the wave tables on the activity DB (idempotent)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS plugin_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            namespace TEXT NOT NULL,
            event TEXT NOT NULL,
            payload TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON plugin_events(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_name ON plugin_events(event)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS streams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            session_id TEXT,
            turn_id TEXT,
            model TEXT,
            provider TEXT,
            surface TEXT,
            iteration INTEGER DEFAULT 0,
            started_at TEXT,
            ended_at TEXT,
            chars INTEGER DEFAULT 0,
            deltas INTEGER DEFAULT 0,
            kind_counts TEXT,
            first_token_ms INTEGER,
            finished INTEGER DEFAULT 1,
            error TEXT,
            duration_ms INTEGER
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_streams_ts ON streams(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_streams_turn ON streams(turn_id)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS api_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            session_id TEXT,
            turn_id TEXT,
            api_request_id TEXT,
            provider TEXT,
            model TEXT,
            api_mode TEXT,
            api_call_count INTEGER DEFAULT 0,
            status TEXT DEFAULT 'running',
            duration_ms INTEGER,
            input_tokens INTEGER,
            output_tokens INTEGER,
            finish_reason TEXT,
            error_type TEXT,
            error_message TEXT,
            retry_count INTEGER
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_api_ts ON api_requests(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_api_req ON api_requests(api_request_id)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS subagents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            parent_session_id TEXT,
            child_session_id TEXT,
            child_role TEXT,
            status TEXT DEFAULT 'running',
            duration_ms INTEGER,
            started_at TEXT,
            ended_at TEXT,
            summary_preview TEXT,
            goal_preview TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sub_ts ON subagents(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sub_child ON subagents(child_session_id)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS approvals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            surface TEXT,
            pattern_key TEXT,
            command_preview TEXT,
            choice TEXT DEFAULT 'pending',
            decided_by TEXT,
            session_key TEXT,
            decided_at TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_appr_ts ON approvals(timestamp)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS commands (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            surface TEXT,
            command TEXT,
            alias_used TEXT,
            args_preview TEXT,
            session_key TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cmd_ts ON commands(timestamp)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS platform_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            platform TEXT,
            event_type TEXT,
            payload TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_plat_ts ON platform_events(timestamp)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS skills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            name TEXT,
            action TEXT,
            provenance TEXT,
            details TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_skills_ts ON skills(timestamp)")


def _wave_insert(table: str, **fields: Any) -> Optional[int]:
    """Insert one row into a wave table. Thread-safe, fail-open.

    Deliberately does NOT take the core module's shared ``_lock``: the emit
    sites live inside critical sections that already hold it (e.g.
    ``_record_signals``), and that lock is a non-reentrant ``threading.Lock``
    — re-acquiring it from the same thread deadlocks. Wave tables are written
    as single autocommit statements under SQLite WAL with a busy_timeout, so
    concurrent writers serialize at the DB level instead.

    A transient ``database is locked`` (a cross-process WAL writer collision)
    is retried once with a short backoff via :func:`_wave_with_retry`, and the
    warning is downgraded to DEBUG because it is not operator-actionable.
    """
    cols = [k for k in fields.keys() if fields[k] is not None]
    vals = [fields[k] for k in cols]
    sql = (
        f"INSERT INTO {table} ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' * len(cols))})"
    )

    def _run(conn):
        cur = conn.execute(sql, vals)
        conn.commit()
        return cur.lastrowid

    return _wave_with_retry("insert", table, _run)


def _wave_update(table: str, set_fields: Dict[str, Any], where: str, where_args: list) -> bool:
    """Update rows in a wave table. Thread-safe, fail-open (see _wave_insert).

    Retries a transient ``database is locked`` once — see :func:`_wave_with_retry`.
    """
    cols = [k for k, v in set_fields.items() if v is not None]
    assignments = ", ".join(f"{k} = ?" for k in cols)
    sql = f"UPDATE {table} SET {assignments} WHERE {where}"
    args = [set_fields[k] for k in cols] + where_args

    def _run(conn):
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.rowcount > 0

    res = _wave_with_retry("update", table, _run)
    return bool(res)


_TABLES_READY = False


def _ensure_tables() -> None:
    """Create wave tables once if they do not exist yet (idempotent).

    In a real runtime ``_init_db()`` already calls ``wave_init_db()``, so this
    is a cheap safety net for callers that touch wave storage without going
    through the normal init path. Never runs DDL per-write.
    """
    global _TABLES_READY
    if _TABLES_READY:
        return
    try:
        from __init__ import _get_activity_conn

        conn = _get_activity_conn()
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='plugin_events'"
            ).fetchone()
            if row is None:
                wave_init_db(conn)
                conn.commit()
            _TABLES_READY = True
        finally:
            conn.close()
    except Exception as exc:
        logger.debug("Abyss wave table guard failed: %s", exc)


def _wave_rows(table: str, limit: int = 50, where: str = "", args: Optional[list] = None) -> list:
    """Read recent rows from a wave table."""
    try:
        from __init__ import _get_activity_conn

        _ensure_tables()
        conn = _get_activity_conn()
        try:
            sql = f"SELECT * FROM {table}"
            if where:
                sql += f" WHERE {where}"
            sql += " ORDER BY timestamp DESC, id DESC LIMIT ?"
            rows = conn.execute(sql, (args or []) + [limit]).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("Abyss wave read from %s failed: %s", table, exc)
        return []


def _wave_signal(
    signal_type: str,
    severity: str,
    label: str,
    description: str,
    session_id: str = "",
) -> Optional[int]:
    """Insert a classifier-style signal row with source 'wave'.

    Retries a transient ``database is locked`` once — see :func:`_wave_with_retry`.
    """
    ts = datetime.now().isoformat()
    sid = session_id or None

    def _run(conn):
        cur = conn.execute(
            """INSERT INTO signals
               (timestamp, signal_type, severity, label, description,
                session_id, source)
               VALUES (?, ?, ?, ?, ?, ?, 'wave')""",
            (ts, signal_type, severity, label, description, sid),
        )
        conn.commit()
        return cur.lastrowid

    return _wave_with_retry("signal", "signals", _run)


def _wave_signal_coalesced(
    signal_type: str,
    severity: str,
    label: str,
    description: str,
    session_id: str = "",
    coalesce_minutes: Optional[int] = None,
) -> Optional[int]:
    """Insert a wave signal, coalescing same-type repeats per session.

    A context-window-exhausted session emits an ``empty_stream`` signal for
    EVERY zero-token stream — hundreds of identical rows in one session (628
    observed in ``20260821_005355_cde5b755``; 2,270 unresolved rows total).
    Each row is a separate line in the Watch tab and inflates the open-signal
    count, pinning the health score's signal component at 0 while the true
    cause is a single context-window exhaustion.

    When the most recent unresolved signal of this type for the same session
    falls within ``coalesce_minutes`` (config -> ``stream_signal_coalesce_minutes``,
    default 10), this bumps its ``details.repeat_count`` / ``last_repeat_at``
    instead of inserting a new row. If the repeat's severity ranks HIGHER than
    the stored row's (e.g. warning -> error), the stored row's severity is
    escalated so a worsening failure is never hidden behind a repeat count.
    Returns the new/updated signal id, or None on failure (fail-open, matching
    :func:`_wave_signal`).
    """
    if not session_id:
        return _wave_signal(signal_type, severity, label, description, session_id)
    window = coalesce_minutes if coalesce_minutes is not None else int(
        SETTINGS.get("stream_signal_coalesce_minutes", 10) or 10
    )
    cutoff = (datetime.now() - timedelta(minutes=window)).isoformat()

    def _run(conn):
        row = conn.execute(
            """SELECT id, details, severity FROM signals
               WHERE signal_type = ? AND session_id = ?
                 AND resolved = 0 AND timestamp >= ?
               ORDER BY timestamp DESC, id DESC LIMIT 1""",
            (signal_type, session_id, cutoff),
        ).fetchone()
        if row is None:
            return None  # no recent unresolved signal -> caller inserts fresh
        try:
            details = json.loads(row["details"]) if row["details"] else {}
        except (ValueError, TypeError):
            details = {}
        details["repeat_count"] = int(details.get("repeat_count", 0)) + 1
        details["last_repeat_at"] = datetime.now().isoformat()
        # Severity escalation: a repeat that is WORSE than the original
        # signal must escalate the stored row, not hide behind repeat_count.
        # (e.g. a retryable 429 warning storm that turns into a hard 500
        # error — the unresolved row must reflect the worst observed state
        # so health scoring / Watch-tab triage see the real severity.)
        if _severity_rank(severity) > _severity_rank(row["severity"]):
            conn.execute(
                "UPDATE signals SET details = ?, severity = ? WHERE id = ?",
                (json.dumps(details), severity, row["id"]),
            )
        else:
            conn.execute(
                "UPDATE signals SET details = ? WHERE id = ?",
                (json.dumps(details), row["id"]),
            )
        conn.commit()
        return row["id"]

    merged = _wave_with_retry("coalesce", "signals", _run)
    if merged:
        return merged
    return _wave_signal(signal_type, severity, label, description, session_id)


# ---------------------------------------------------------------------------
# Event bus (#64164) — durable emit under the abyss: namespace
# ---------------------------------------------------------------------------

_EMIT_EVENT_RE = re.compile(r"^[a-z0-9_.-]{1,80}$")


def emit_abyss_event(event: str, payload: Optional[dict] = None) -> int:
    """Publish ``abyss:<event>`` on the plugin event bus and record a durable
    audit row. Returns the number of subscribers scheduled (0 is fine).

    Mirrors the host ``ctx.emit`` fail-closed contract: a namespaced name
    (containing ``:``) raises ``ValueError`` — a plugin may only emit bare
    event names under its own ``abyss:`` namespace.
    """
    if ":" in event:
        raise ValueError(
            f"Abyss may not emit {event!r}: emit only the bare event name; "
            "the namespace is forced to 'abyss:'"
        )
    if not _EMIT_EVENT_RE.match(event):
        logger.warning("Abyss refused to emit invalid event name %r", event)
        return 0
    payload = payload or {}
    # Durable audit row first (works without ctx, e.g. tests).
    _wave_insert(
        "plugin_events",
        timestamp=datetime.now().isoformat(),
        namespace="abyss",
        event=event,
        payload=_mask_secrets(json.dumps(payload, default=str))[:4000],
    )
    if _CTX is None:
        return 0
    try:
        return int(_CTX.emit(event, payload) or 0)
    except Exception as exc:
        logger.debug("Abyss event bus emit %s failed: %s", event, exc)
        return 0


def emit_from_ctx(ctx, event: str, payload: Optional[dict] = None) -> int:
    """Emit using an explicit ctx (used before wave_register stores it)."""
    global _CTX
    previous = _CTX
    _CTX = ctx
    try:
        return emit_abyss_event(event, payload)
    finally:
        _CTX = previous


# ---------------------------------------------------------------------------
# Lifecycle hook handlers — all observers, all fail-open
# ---------------------------------------------------------------------------


def _on_session_reset(
    session_id: str = "",
    platform: str = "",
    reason: str = "",
    old_session_id: str = "",
    new_session_id: str = "",
    **_,
) -> None:
    """Log session resets (gateway /new, /reset) to the activity feed."""
    from __init__ import _add_activity

    _add_activity(
        action="session_reset",
        description=f"Session reset via {reason or 'unknown'} "
        f"({str(old_session_id or '')[:8]} -> {str(new_session_id or '')[:8]})",
        category="session",
        status="completed",
        metadata={"reason": reason, "platform": platform},
        session_id=new_session_id or old_session_id or None,
    )


def _on_session_finalize(
    session_id: str = "",
    platform: str = "",
    reason: str = "",
    old_session_id: str = "",
    new_session_id: str = "",
    **_,
) -> None:
    """Log session finalization (boundary before a session is retired)."""
    from __init__ import _add_activity

    _add_activity(
        action="session_finalized",
        description=f"Session finalized ({reason or 'unknown'})",
        category="session",
        status="completed",
        metadata={"reason": reason, "platform": platform, "new_session_id": new_session_id},
        session_id=old_session_id or session_id or None,
    )


def _sweep_stuck_streams() -> int:
    """Persist + signal streams that went silent and never received on_stream_end.

    A stream that opened an accumulator but never delivered an on_stream_end
    (provider hang, dropped hook, killed process) used to be silently evicted
    at the _STREAM_MAX cap — no durable row, no signal. Now, whenever a new
    stream starts, we sweep accumulators SILENT for longer than
    _STREAM_STUCK_AFTER and record them as finished=0 streams rows plus a
    `stuck_stream` warning signal so the silent failure is observable.

    Liveness is measured from per-accumulator ``last_seen`` (updated on every
    on_stream_delta), NOT from ``started``: a legitimate long generation (e.g.
    a big analysis turn) streams tokens for > _STREAM_STUCK_AFTER and the old
    age-only check swept it as "stuck" the next time any new stream started,
    producing a false stuck_stream warning, a bogus finished=0 row, and a
    duplicate late-end row. Accumulators predating the last_seen field fall
    back to ``started`` so genuinely hung streams are still caught.
    """
    now = time.time()
    stuck = []
    with _STREAM_LOCK:
        for key, st in list(_STREAMS.items()):
            last_seen = st.get("last_seen", st.get("started", now))
            if now - last_seen > _STREAM_STUCK_AFTER:
                stuck.append((key, st))
        for key, _ in stuck:
            _STREAMS.pop(key, None)
    if not stuck:
        return 0
    for (session_id, turn_id), st in stuck:
        started = st.get("started", now)
        last_seen = st.get("last_seen", started)
        idle_s = max(0, now - last_seen)
        try:
            _wave_insert(
                "streams",
                timestamp=datetime.now().isoformat(),
                session_id=str(session_id or ""),
                turn_id=str(turn_id or ""),
                model=st.get("model", "") or "",
                provider=st.get("provider", "") or "",
                surface=st.get("surface", "") or "",
                iteration=st.get("iteration", 0),
                started_at=datetime.fromtimestamp(started).isoformat(),
                ended_at=datetime.fromtimestamp(now).isoformat(),
                chars=st.get("chars", 0),
                deltas=st.get("deltas", 0),
                kind_counts=json.dumps(st.get("kinds", {}), default=str),
                first_token_ms=st.get("first_token_ms"),
                finished=0,
                error=(
                    "stuck: no on_stream_end (provider hang or dropped hook) — "
                    f"idle {idle_s:.0f}s after {st.get('deltas', 0)} delta(s)"
                ),
                duration_ms=int((now - started) * 1000),
            )
            if SETTINGS.get("stream_signals", True):
                _wave_signal(
                    "stuck_stream",
                    "warning",
                    "Stuck LLM stream",
                    f"Streaming response never finished: session "
                    f"{str(session_id or '')[:8]}, turn {str(turn_id or '')[:8]} "
                    f"(idle {idle_s:.0f}s, no on_stream_end after "
                    f"{(now - started):.0f}s).",
                    session_id or "",
                )
        except Exception as exc:
            logger.debug("Abyss stuck-stream sweep failed for %s: %s", (session_id, turn_id), exc)
    return len(stuck)


def _on_stream_start(
    turn_id: str = "",
    iteration: int = 0,
    session_id: str = "",
    model: str = "",
    provider: str = "",
    surface: str = "",
    **_,
) -> None:
    """Open an in-memory accumulator for one streaming response."""
    # Sweep orphaned accumulators (streams that started but never received
    # on_stream_end) before adding the new one — see _sweep_stuck_streams.
    try:
        _sweep_stuck_streams()
    except Exception:
        pass
    key = (str(session_id or ""), str(turn_id or ""))
    now = time.time()
    with _STREAM_LOCK:
        _STREAMS[key] = {
            "started": now,
            "last_seen": now,
            "chars": 0,
            "deltas": 0,
            "kinds": {},
            "iteration": iteration,
            "model": model,
            "provider": provider,
            "surface": surface,
            "session_id": session_id or "",
            "turn_id": turn_id or "",
        }
        # Bound memory: evict the oldest entry beyond the cap. The evicted
        # key is tracked (bounded _EVICTED) so its late on_stream_end can
        # persist a telemetry-incomplete row WITHOUT firing a false
        # empty_stream signal — see _EVICTED.
        if len(_STREAMS) > _STREAM_MAX:
            oldest = min(_STREAMS, key=lambda k: _STREAMS[k]["started"])
            _STREAMS.pop(oldest, None)
            _EVICTED.add(oldest)
            while len(_EVICTED) > max(16, _STREAM_MAX * 2):
                _EVICTED.pop()


def _on_stream_delta(
    turn_id: str = "",
    session_id: str = "",
    delta: str = "",
    kind: str = "text",
    **_,
) -> None:
    """Accumulate one streaming delta (observer-only; never stored per-token)."""
    key = (str(session_id or ""), str(turn_id or ""))
    with _STREAM_LOCK:
        st = _STREAMS.get(key)
        if st is None:
            return
        st["last_seen"] = time.time()
        st["deltas"] += 1
        st["chars"] += len(delta or "")
        st["kinds"][kind] = st["kinds"].get(kind, 0) + 1
        if st["deltas"] == 1:
            st["first_token_ms"] = int((time.time() - st["started"]) * 1000)


def _session_activity_count(session_id: str) -> int:
    """Return the number of activity rows for *session_id* in the last 24h.

    Used by the empty-stream classifier to distinguish a transient provider
    hiccup (low activity) from context-window exhaustion (a heavily-used
    session that has run out of context). Fail-open: returns 0 on any
    DB error so the signal still fires - just without the extra hint.
    """
    if not session_id:
        return 0
    try:
        from __init__ import _get_activity_conn

        # Timezone-aware cutoff: stored activity timestamps are local-time
        # ISO strings (datetime.now().isoformat()), so the cutoff MUST be
        # computed in Python the same way. Comparing against SQLite's
        # datetime('now','-24 hours') (UTC, space separator) silently
        # widened the window by the UTC offset + the 'T'-vs-' ' ordering,
        # so sessions active 24-28h ago could trip the 3000-activity
        # context-exhaustion hint despite being quiet in the real last 24h.
        cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
        _ensure_tables()
        conn = _get_activity_conn()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM activity "
                "WHERE session_id = ? "
                "AND timestamp >= ?",
                (session_id, cutoff),
            ).fetchone()
            return int(row["c"]) if row else 0
        finally:
            conn.close()
    except Exception as exc:
        logger.debug("session activity count failed for %s: %s", session_id, exc)
        return 0


def _on_stream_end(
    turn_id: str = "",
    session_id: str = "",
    final_text: str = "",
    finished: bool = True,
    error: Optional[str] = None,
    **_,
) -> None:
    """Persist aggregated stream stats and detect empty-stream failures."""
    key = (str(session_id or ""), str(turn_id or ""))
    was_evicted = key in _EVICTED
    if was_evicted:
        _EVICTED.discard(key)
    with _STREAM_LOCK:
        st = _STREAMS.pop(key, None)
        had_accumulator = st is not None
    if st is None:
        st = {
            "started": time.time(),
            "chars": 0,
            "deltas": 0,
            "kinds": {},
            "iteration": 0,
            "model": "",
            "provider": "",
            "surface": "",
            "session_id": session_id or "",
            "turn_id": turn_id or "",
        }
        # Late end for a stream whose accumulator was evicted at the cap:
        # the end event is real, so persist the row — but flag the telemetry
        # as incomplete and never claim the stream was 'empty' (we simply
        # stopped tracking it; it may have streamed plenty of tokens).
        st["evicted"] = was_evicted
    ended = time.time()
    duration_ms = int((ended - st["started"]) * 1000)
    error_text = str(error)[:400] if error else None
    if not error_text and st.get("evicted"):
        error_text = (
            "accumulator evicted at stream cap (tracking limit) — "
            "telemetry incomplete"
        )
    _wave_insert(
        "streams",
        timestamp=datetime.now().isoformat(),
        session_id=st.get("session_id") or "",
        turn_id=st.get("turn_id") or "",
        model=st.get("model", "") or "",
        provider=st.get("provider", "") or "",
        surface=st.get("surface", "") or "",
        iteration=st.get("iteration", 0),
        started_at=datetime.fromtimestamp(st["started"]).isoformat(),
        ended_at=datetime.fromtimestamp(ended).isoformat(),
        chars=st.get("chars", 0),
        deltas=st.get("deltas", 0),
        kind_counts=json.dumps(st.get("kinds", {}), default=str),
        first_token_ms=st.get("first_token_ms"),
        finished=1 if finished else 0,
        error=_mask_secrets(error_text) if error_text else None,
        duration_ms=duration_ms,
    )
    # Signal: a "successful" stream that produced zero tokens is a silent
    # failure class traditional logging misses (Raindrop philosophy). Only
    # fire it when the stream was actually OBSERVED (its accumulator existed
    # at on_stream_end): a late end for a cap-evicted / never-started stream
    # has chars=0 only because tracking was lost, not because the provider
    # returned nothing — claiming 'empty' there is a false positive that
    # pollutes the Watch tab and pins the health score.
    if (
        SETTINGS.get("stream_signals", True)
        and had_accumulator
        and finished and not error
        and st.get("chars", 0) == 0
    ):
        # Classify the root cause. In sessions with >3000 activities in 24h
        # (e.g. 4042 in session 20260814_212312_f86a12), an empty stream is
        # almost always context-window exhaustion rather than a transient
        # provider error. Surface actionable guidance so the user knows to
        # consolidate memory or start a fresh session.
        hint = ""
        if session_id:
            _activity_count = _session_activity_count(session_id)
            if _activity_count > 3000:
                hint = (
                    " LIKELY CONTEXT-WINDOW EXHAUSTION: this session has "
                    f"{_activity_count} activities in 24h. Call memory "
                    "consolidate or start a fresh session."
                )
        _wave_signal_coalesced(
            "empty_stream",
            "warning",
            "Empty LLM stream",
            "Streaming response finished with zero text tokens "
            f"(session {str(session_id or '')[:8]}, turn {str(turn_id or '')[:8]})."
            + hint,
            session_id or "",
        )

    # Signal: a stream that ENDED without finishing (finished=False) or with
    # an explicit error is a truncated-response failure class nothing else
    # surfaces. on_stream_end fired but reported the generation did not
    # complete — the user sees a cut-off reply. stuck_stream owns the orphan
    # case (end hook never fired), truncated_response owns finish_reason
    # truncation at the API layer, and empty_stream owns zero-token NORMAL
    # finishes; this closes the remaining hole: abnormal stream termination.
    # Coalesced like empty_stream — a provider that drops every stream in a
    # session must bump repeat_count, not flood the signal feed.
    if SETTINGS.get("stream_signals", True) and (not finished or error):
        if error:
            _severity = "error"
            _label = "Stream error"
            _desc = (
                f"Streaming response errored mid-generation "
                f"(session {str(session_id or '')[:8]}, turn {str(turn_id or '')[:8]}): "
                f"{error_text or 'unknown error'}"
            )
        else:
            _severity = "warning"
            _label = "Stream interrupted"
            _desc = (
                f"Streaming response ended before finishing "
                f"(session {str(session_id or '')[:8]}, turn {str(turn_id or '')[:8]}), "
                f"{st.get('chars', 0) if had_accumulator else '?'} chars streamed"
            )
        _wave_signal_coalesced(
            "stream_interrupted",
            _severity,
            _label,
            _desc,
            session_id or "",
        )


def _on_pre_api_request(
    api_request_id: str = "",
    session_id: str = "",
    turn_id: str = "",
    model: str = "",
    provider: str = "",
    api_mode: str = "",
    api_call_count: int = 0,
    retry_count: Any = None,
    approx_input_tokens: Any = None,
    task_id: str = "",
    **_,
) -> None:
    """Record the start of an LLM API request."""
    _wave_insert(
        "api_requests",
        timestamp=datetime.now().isoformat(),
        session_id=session_id or "",
        turn_id=turn_id or "",
        api_request_id=api_request_id or "",
        provider=provider or "",
        model=model or "",
        api_mode=api_mode or "",
        api_call_count=int(api_call_count or 0),
        status="running",
        input_tokens=int(approx_input_tokens) if isinstance(approx_input_tokens, int) else None,
        retry_count=int(retry_count) if isinstance(retry_count, int) else None,
    )


def _usage_tokens(usage: Any) -> tuple:
    """Extract (input_tokens, output_tokens) from any usage-shaped object."""
    if isinstance(usage, dict):
        return (
            usage.get("input_tokens") or usage.get("prompt_tokens"),
            usage.get("output_tokens") or usage.get("completion_tokens"),
        )
    if usage is not None and hasattr(usage, "input_tokens"):
        return getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None)
    return None, None


def _on_post_api_request(
    api_request_id: str = "",
    session_id: str = "",
    model: str = "",
    provider: str = "",
    api_duration: float = 0.0,
    finish_reason: str = "",
    usage: Any = None,
    assistant_content_chars: int = 0,
    assistant_tool_call_count: int = 0,
    task_id: str = "",
    turn_id: str = "",
    **_,
) -> None:
    """Close an API request row with duration/tokens/finish reason."""
    in_tokens, out_tokens = _usage_tokens(usage)
    _wave_update(
        "api_requests",
        {
            "status": "completed",
            "duration_ms": int((api_duration or 0.0) * 1000),
            "finish_reason": str(finish_reason or "")[:60],
            "input_tokens": int(in_tokens) if isinstance(in_tokens, int) else None,
            "output_tokens": int(out_tokens) if isinstance(out_tokens, int) else None,
        },
        where="api_request_id = ? AND status = 'running'",
        where_args=[api_request_id or ""],
    )

    # Signal: an API response truncated by the OUTPUT budget is a silent
    # failure — the reply is cut off mid-generation and the agent continues
    # unaware (Raindrop philosophy: the failures logs miss). finish_reason
    # 'length'/'max_tokens' means max output tokens was hit; 'content_filter'
    # means the provider vetoed part of the reply. Both degrade the response
    # without raising an error, so traditional logging records nothing.
    _finish_l = str(finish_reason or "").lower()
    if _finish_l in ("length", "max_tokens", "content_filter"):
        # Coalesced: a session pinned at its output budget (finish_reason
        # 'length' every request) used to insert one signal ROW per request —
        # the same flood class as empty_stream (see _wave_signal_coalesced).
        # Per-request detail stays in api_requests; the signal tracks the
        # first occurrence + repeat_count so health scoring stays honest.
        _wave_signal_coalesced(
            "truncated_response",
            "warning",
            f"Response truncated ({finish_reason})",
            f"LLM finished with {finish_reason} on {model or 'unknown'} "
            f"({provider or 'unknown'}) — reply incomplete"
            + (f", {out_tokens} output tokens" if isinstance(out_tokens, int) else ""),
            session_id or "",
        )


def _on_api_request_error(
    api_request_id: str = "",
    session_id: str = "",
    provider: str = "",
    model: str = "",
    api_mode: str = "",
    api_call_count: int = 0,
    api_duration: float = 0.0,
    status_code: Any = None,
    retry_count: Any = None,
    retryable: Any = None,
    error_type: str = "",
    error_message: str = "",
    task_id: str = "",
    turn_id: str = "",
    **_,
) -> None:
    """Mark an API request failed and surface a signal."""
    err_text = str(error_message or "")[:400]
    _wave_update(
        "api_requests",
        {
            "status": "error",
            "duration_ms": int((api_duration or 0.0) * 1000),
            "error_type": str(error_type or "")[:80],
            "error_message": _mask_secrets(err_text) if err_text else None,
            "retry_count": int(retry_count) if isinstance(retry_count, int) else None,
        },
        where="api_request_id = ? AND status = 'running'",
        where_args=[api_request_id or ""],
    )
    severity = "warning" if retryable else "error"
    # Coalesced: a provider outage / rate-limit storm retries every request,
    # inserting N identical api_error ROWS per session (same flood class as
    # empty_stream). Per-request detail stays in api_requests; the signal
    # coalesces with repeat_count and ESCALATES severity if a later failure
    # is worse (warning 429 storm -> hard error 500).
    _wave_signal_coalesced(
        "api_error",
        severity,
        f"API error: {provider or 'unknown'} {status_code or ''}".strip(),
        f"{error_type or 'api_request_error'}: {err_text[:200]}" if err_text else "API request failed",
        session_id or "",
    )


def _on_subagent_start(
    parent_session_id: str = "",
    child_session_id: str = "",
    child_role: str = "",
    child_goal: str = "",
    parent_turn_id: str = "",
    parent_subagent_id: str = "",
    **_,
) -> None:
    """Record a subagent spawn (#65447)."""
    _wave_insert(
        "subagents",
        timestamp=datetime.now().isoformat(),
        parent_session_id=parent_session_id or "",
        child_session_id=str(child_session_id or ""),
        child_role=str(child_role or "")[:60],
        status="running",
        started_at=datetime.now().isoformat(),
        goal_preview=_mask_secrets(str(child_goal or "")[:300]),
    )


def _on_subagent_stop(
    child_session_id: str = "",
    child_role: str = "",
    child_summary: Any = None,
    child_status: str = "",
    duration_ms: int = 0,
    parent_session_id: str = "",
    parent_turn_id: str = "",
    tool_call_history: Any = None,
    **_,
) -> None:
    """Close a subagent row; surface a signal on failure."""
    status = str(child_status or "completed")[:40] or "completed"
    summary = str(child_summary or "")[:300]
    _wave_update(
        "subagents",
        {
            "status": status,
            "duration_ms": int(duration_ms or 0),
            "ended_at": datetime.now().isoformat(),
            "summary_preview": _mask_secrets(summary) if summary else None,
        },
        where="child_session_id = ? AND status = 'running'",
        where_args=[str(child_session_id or "")],
    )
    if status.lower() in ("error", "failed", "cancelled", "timeout"):
        _wave_signal(
            "subagent_failure",
            "warning",
            f"Subagent {status}",
            f"Subagent ({child_role or 'unknown'}) finished with status {status}.",
            parent_session_id or "",
        )


def _on_pre_approval_request(
    command: str = "",
    description: str = "",
    pattern_key: str = "",
    pattern_keys: Optional[list] = None,
    session_key: str = "",
    surface: str = "",
    **_,
) -> None:
    """Record an approval request (#64162 observability)."""
    keys = pattern_keys or []
    if not pattern_key and keys:
        pattern_key = ",".join(str(k) for k in keys)
    _wave_insert(
        "approvals",
        timestamp=datetime.now().isoformat(),
        surface=str(surface or "")[:40],
        pattern_key=str(pattern_key or "")[:200],
        command_preview=_mask_secrets(str(command or "")[:300]),
        choice="pending",
        session_key=str(session_key or "")[:200],
    )


def _on_post_approval_response(
    choice: str = "",
    decided_by: str = "",
    session_key: str = "",
    surface: str = "",
    command: str = "",
    pattern_key: str = "",
    pattern_keys: Optional[list] = None,
    **_,
) -> None:
    """Close the matching pending approval row; flag denials.

    Only the NEWEST pending row matching (session_key + optional pattern_key)
    is closed — the old query updated EVERY pending row for the session_key,
    so two outstanding approvals (parallel dangerous commands) had BOTH rows
    overwritten with the same choice, corrupting the audit trail for the
    command the user never actually saw. When pattern_key is available it
    narrows the match to the intended approval; otherwise the newest pending
    row for the session is the best available correlation.
    """
    _where = (
        "id = (SELECT id FROM approvals WHERE choice = 'pending' AND session_key = ?"
        + (" AND pattern_key = ?" if pattern_key else "")
        + " ORDER BY id DESC LIMIT 1)"
    )
    _args = [str(session_key or "")]
    if pattern_key:
        _args.append(str(pattern_key))
    _wave_update(
        "approvals",
        {
            "choice": str(choice or "unknown")[:40],
            "decided_by": str(decided_by or "")[:40],
            "decided_at": datetime.now().isoformat(),
        },
        where=_where,
        where_args=_args,
    )
    if str(choice or "").lower() in ("deny", "timeout"):
        _wave_signal(
            "approval_denied",
            "warning",
            f"Approval {choice}",
            f"Dangerous command was {choice} (surface {surface or 'unknown'}).",
            session_key or "",
        )


def _on_gateway_platform_event(
    platform: str = "",
    event_type: str = "",
    payload: Optional[dict] = None,
    **_,
) -> None:
    """Persist a normalized gateway platform-event envelope (#64176)."""
    _wave_insert(
        "platform_events",
        timestamp=datetime.now().isoformat(),
        platform=str(platform or "")[:80],
        event_type=str(event_type or "")[:120],
        payload=_mask_secrets(json.dumps(payload or {}, default=str))[:3000],
    )


def _on_pre_command(
    surface: str = "",
    command: str = "",
    alias_used: str = "",
    args_raw: str = "",
    session_key: str = "",
    platform: str = "",
    **_,
) -> None:
    """Record slash-command usage (#64204 observer)."""
    _wave_insert(
        "commands",
        timestamp=datetime.now().isoformat(),
        surface=str(surface or "")[:40],
        command=str(command or "")[:80],
        alias_used=str(alias_used or "")[:80],
        args_preview=_mask_secrets(str(args_raw or "")[:300]),
        session_key=str(session_key or "")[:200],
    )


def _on_skill_lifecycle(
    action: str = "",
    skill_name: str = "",
    provenance: str = "",
    task_id: str = "",
    session_id: str = "",
    use_count: Any = None,
    reused: Any = None,
    reuse_after_patch: Any = None,
    **_,
) -> None:
    """Record a successful skill lifecycle fact (local observability)."""
    details = {
        "task_id": str(task_id or "")[:80],
        "session_id": str(session_id or "")[:80],
        "use_count": use_count,
        "reused": reused,
        "reuse_after_patch": reuse_after_patch,
    }
    _wave_insert(
        "skills",
        timestamp=datetime.now().isoformat(),
        name=str(skill_name or "")[:160],
        action=str(action or "")[:40],
        provenance=str(provenance or "")[:40],
        details=json.dumps({k: v for k, v in details.items() if v is not None}, default=str)[:1000],
    )


# ---------------------------------------------------------------------------
# Registration (#64227/#64229/#65449/#64164)
# ---------------------------------------------------------------------------


def _on_unload() -> None:
    """Ownership-ledger cleanup: drop ctx + stream state on unload/force-reload."""
    global _CTX
    _CTX = None
    with _STREAM_LOCK:
        _STREAMS.clear()
    _EVICTED.clear()


def wave_register(ctx) -> None:
    """Wire all wave surfaces onto a live plugin context."""
    global _CTX
    _CTX = ctx

    # Config/state bridge (#64227): settings live in plugins.entries.abyss.settings
    # with env-var fallbacks so test suites and headless runs keep working.
    try:
        retention = ctx.get_config("retention_days", SETTINGS.get("retention_days"))
        if retention is not None:
            SETTINGS["retention_days"] = int(retention)
    except Exception:
        pass
    try:
        url = ctx.get_config("webhook_url", SETTINGS.get("webhook_url"))
        if url:
            SETTINGS["webhook_url"] = str(url)
    except Exception:
        pass
    try:
        stream_sig = ctx.get_config("stream_signals", SETTINGS.get("stream_signals"))
        if stream_sig is not None:
            SETTINGS["stream_signals"] = bool(stream_sig)
    except Exception:
        pass
    try:
        coalesce = ctx.get_config(
            "stream_signal_coalesce_minutes",
            SETTINGS.get("stream_signal_coalesce_minutes"),
        )
        if coalesce is not None:
            SETTINGS["stream_signal_coalesce_minutes"] = int(coalesce)
    except Exception:
        pass

    # Redaction registry (#65449): vendor token formats the host engine masks.
    try:
        ctx.register_redaction_patterns(list(_REDACTION_PATTERNS))
    except Exception as exc:
        logger.debug("Abyss redaction pattern registration failed: %s", exc)

    # Event bus (#64164): durable emits under the abyss: namespace.
    ctx.emit("wave_ready", {"version": "2.0.0"})

    # Ownership ledger (#64229): clean up on unload/force-reload (#64178).
    try:
        ctx.on_unload(_on_unload)
    except Exception as exc:
        logger.debug("Abyss on_unload registration failed: %s", exc)

    # Lifecycle hooks — observers only, all fail-open.
    ctx.register_hook("on_session_reset", _on_session_reset)
    ctx.register_hook("on_session_finalize", _on_session_finalize)
    ctx.register_hook("on_stream_start", _on_stream_start)
    ctx.register_hook("on_stream_delta", _on_stream_delta)
    ctx.register_hook("on_stream_end", _on_stream_end)
    ctx.register_hook("pre_api_request", _on_pre_api_request)
    ctx.register_hook("post_api_request", _on_post_api_request)
    ctx.register_hook("api_request_error", _on_api_request_error)
    ctx.register_hook("subagent_start", _on_subagent_start)
    ctx.register_hook("subagent_stop", _on_subagent_stop)
    ctx.register_hook("pre_approval_request", _on_pre_approval_request)
    ctx.register_hook("post_approval_response", _on_post_approval_response)
    ctx.register_hook("gateway_platform_event", _on_gateway_platform_event)
    ctx.register_hook("pre_command", _on_pre_command)
    ctx.register_hook("on_skill_lifecycle", _on_skill_lifecycle)


# ---------------------------------------------------------------------------
# Wave API surface
# ---------------------------------------------------------------------------


def list_wave_events(limit: int = 50) -> list:
    return _wave_rows("plugin_events", limit=limit)


def list_streams(limit: int = 50) -> list:
    return _wave_rows("streams", limit=limit)


def list_api_requests(limit: int = 50) -> list:
    return _wave_rows("api_requests", limit=limit)


def list_subagents(limit: int = 50) -> list:
    return _wave_rows("subagents", limit=limit)


def list_approvals(limit: int = 50) -> list:
    return _wave_rows("approvals", limit=limit)


def list_commands(limit: int = 50) -> list:
    return _wave_rows("commands", limit=limit)


def list_platform_events(limit: int = 50) -> list:
    return _wave_rows("platform_events", limit=limit)


def list_skills(limit: int = 50) -> list:
    return _wave_rows("skills", limit=limit)


def wave_summary() -> dict:
    """Per-table counts + most recent timestamp, for the wave view header."""
    try:
        from __init__ import _get_activity_conn

        _ensure_tables()
        conn = _get_activity_conn()
        try:
            out: Dict[str, Any] = {}
            for table in _WAVE_TABLES:
                row = conn.execute(
                    f"SELECT COUNT(*) AS c, MAX(timestamp) AS last FROM {table}"
                ).fetchone()
                out[table] = {
                    "count": int(row["c"] or 0),
                    "last": row["last"],
                }
            return {"tables": out}
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("Abyss wave summary failed: %s", exc)
        return {"tables": {}}


def wave_handle(method: str, path: str, params: dict = None, body: str = None):
    """Route /wave/* requests (delegated from the core handle_request)."""
    params = params or {}
    if method == "GET" and path == "/wave/events":
        return list_wave_events(limit=int(params.get("limit", 50)))
    if method == "GET" and path == "/wave/streams":
        return list_streams(limit=int(params.get("limit", 50)))
    if method == "GET" and path == "/wave/api":
        return list_api_requests(limit=int(params.get("limit", 50)))
    if method == "GET" and path == "/wave/subagents":
        return list_subagents(limit=int(params.get("limit", 50)))
    if method == "GET" and path == "/wave/approvals":
        return list_approvals(limit=int(params.get("limit", 50)))
    if method == "GET" and path == "/wave/commands":
        return list_commands(limit=int(params.get("limit", 50)))
    if method == "GET" and path == "/wave/platform":
        return list_platform_events(limit=int(params.get("limit", 50)))
    if method == "GET" and path == "/wave/skills":
        return list_skills(limit=int(params.get("limit", 50)))
    if method == "GET" and path == "/wave/summary":
        return wave_summary()

    if method == "POST" and path == "/wave/emit":
        # Debug/test surface: emit an event on the real bus + durable log.
        try:
            from __init__ import _coerce_body

            data = _coerce_body(body)
        except Exception:
            data = {}
        event = str(data.get("event", "")).strip()
        payload = data.get("payload")
        if not _EMIT_EVENT_RE.match(event):
            return {"error": "invalid event name", "code": 400}
        if payload is not None and not isinstance(payload, dict):
            return {"error": "payload must be a dict", "code": 400}
        n = emit_abyss_event(event, payload or {})
        return {"emitted": f"abyss:{event}", "subscribers": n, "status": "ok"}

    return {"error": f"Unknown wave endpoint: {method} {path}", "code": 404}
