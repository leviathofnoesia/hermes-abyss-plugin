"""Abyss incidents — signal/incident triage + clustering use cases.

Extracted from the plugin god-file (Clean Architecture, use-case layer).
Triage (acknowledge / resolve / status transitions) and Raindrop-style
incident clustering share the severity ranking. Core dependencies
(``_init_db``, ``_get_activity_conn``, ``_alert_on_incident``) are imported
lazily inside the functions, exactly like abyss_wave.py — no import cycle
with ``__init__``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, Optional

logger = logging.getLogger("hermes.plugins.abyss.incidents")

_SEVERITY_RANK = {"critical": 4, "error": 3, "warning": 2, "info": 1}


def _acknowledge_signal(signal_id: int, note: str = "") -> Optional[dict]:
    """Mark a signal acknowledged. Returns the updated row or None."""
    from __init__ import _get_activity_conn, _init_db

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
    from __init__ import _get_activity_conn, _init_db

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


def _resolve_signals_bulk(
    session_prefix: Optional[str] = None,
    signal_type: Optional[str] = None,
    older_than_days: Optional[int] = None,
    close_empty_incidents: bool = False,
) -> dict:
    """Bulk-resolve open signals using plugin triage semantics.

    Filters (ANDed): ``session_prefix`` (session_id LIKE prefix%), 
    ``signal_type``, ``older_than_days`` (timestamp cutoff). All matched
    open signals get ``acknowledged=1 resolved=1`` with timestamps — the same
    transition `_resolve_signal` applies to one signal, so downstream counters
    (health score, open-signal counts, incident signal_count) stay consistent.

    When ``close_empty_incidents`` is True, incidents that end up with zero
    open signals after the batch are transitioned to ``resolved`` too — this is
    the machine-readable form of the old manual
    ``UPDATE signals SET resolved=1 WHERE session_id LIKE 'cron_%'`` cleanup,
    which used to leave their parent incidents permanently open (stale
    incidents pinned the health score at "critical" forever).

    Returns a summary dict:
        {"resolved": N, "signal_ids": [...], "incidents_closed": [...]}
    or {"error": ..., "code": 400/500} when no filter is given or a DB error
    occurs (fail-open, matching the plugin's other triage helpers).
    """
    from __init__ import _get_activity_conn, _init_db

    # Require at least one filter — a bare bulk resolve of EVERYTHING would be
    # an operator footgun; the caller must be explicit about scope.
    if not (session_prefix or signal_type or older_than_days):
        return {"error": "at least one filter required (session_prefix, signal_type, older_than_days)", "code": 400}

    _init_db()
    conn = _get_activity_conn()
    try:
        clauses = ["resolved = 0"]
        params: list = []
        if session_prefix:
            clauses.append("session_id LIKE ?")
            params.append(f"{session_prefix}%")
        if signal_type:
            clauses.append("signal_type = ?")
            params.append(signal_type)
        if older_than_days:
            cutoff = (datetime.now() - timedelta(days=older_than_days)).isoformat()
            clauses.append("timestamp < ?")
            params.append(cutoff)
        where = " AND ".join(clauses)

        # Collect matching signal ids + the incidents they belong to FIRST,
        # then apply the triage transition.
        rows = conn.execute(
            f"SELECT id, incident_id FROM signals WHERE {where}", params
        ).fetchall()
        signal_ids = [r["id"] for r in rows]
        affected_incidents = {r["incident_id"] for r in rows if r["incident_id"] is not None}

        resolved = 0
        if signal_ids:
            now = datetime.now().isoformat()
            placeholders = ", ".join("?" * len(signal_ids))
            conn.execute(
                f"""UPDATE signals
                    SET acknowledged = 1, resolved = 1,
                        acknowledged_at = ?, resolved_at = ?
                    WHERE id IN ({placeholders})""",
                [now, now] + signal_ids,
            )
            resolved = len(signal_ids)

        incidents_closed = []
        if close_empty_incidents and affected_incidents:
            now = datetime.now().isoformat()
            for inc_id in affected_incidents:
                remaining = conn.execute(
                    "SELECT COUNT(*) AS c FROM signals WHERE incident_id = ? AND resolved = 0",
                    (inc_id,),
                ).fetchone()["c"]
                if remaining == 0:
                    conn.execute(
                        "UPDATE incidents SET status = 'resolved', resolved_at = ? WHERE id = ?",
                        (now, inc_id),
                    )
                    incidents_closed.append(inc_id)
        conn.commit()
        return {
            "resolved": resolved,
            "signal_ids": signal_ids,
            "incidents_closed": incidents_closed,
        }
    except sqlite3.Error as e:
        logger.error("Bulk signal resolve failed: %s", e)
        return {"error": str(e), "code": 500}
    finally:
        conn.close()


def _update_incident_status(incident_id: int, status: str) -> Optional[dict]:
    """Transition an incident to a new status (open/acknowledged/resolved/closed).

    When resolved/closed, also resolves all linked open signals.
    """
    from __init__ import _get_activity_conn, _init_db

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


def _merge_signal_ids(prev: Optional[str], new_ids: list) -> str:
    """Union an incident's existing ``signal_ids`` JSON with a new batch.

    The old code REPLACED ``signal_ids`` on merge, so an incident that absorbed
    a second batch of signals silently dropped its original member IDs while
    ``signal_count`` kept growing — count and membership drifted apart. Union
    preserves every linked signal, in first-seen order, deduped.
    """
    try:
        prev_ids = json.loads(prev) if prev else []
        if not isinstance(prev_ids, list):
            prev_ids = []
    except (ValueError, TypeError):
        prev_ids = []
    merged = list(dict.fromkeys([int(i) for i in prev_ids] + [int(i) for i in new_ids]))
    return json.dumps(merged)


def _cluster_incidents(alert: bool = True) -> list:
    """Group related signals into incidents (Raindrop pattern).

    Clustering rules:
    - 2+ signals in the same session (any type) -> incident
    - 3+ signals of the same type across sessions within a 60-minute window -> incident
    - Same pattern + session already has an open incident -> merge into it
      (bump signal_count, union signal_ids)
    - Group 2 only sees signals NOT already claimed by a Group 1 session
      cluster (fix: the old code re-scanned the full snapshot and overwrote
      ``incident_id`` on session-clustered signals, stealing them from the
      session incident so resolving it left them open forever).
    Returns a list of incident IDs created or updated.

    ``alert`` controls webhook alerting for newly-created incidents; pass
    False for startup maintenance so first-boot clustering doesn't spam.
    """
    from __init__ import _alert_on_incident, _get_activity_conn, _init_db

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
        assigned_ids = set()
        session_groups: Dict[str, list] = {}
        for row in signal_rows:
            sid = row["session_id"] or "unknown"
            session_groups.setdefault(sid, []).append(dict(row))

        for sid, signals in session_groups.items():
            if len(signals) < 2:
                continue
            # Look for an existing open incident for this session+pattern
            existing = conn.execute(
                "SELECT id, signal_ids FROM incidents WHERE status = 'open' AND session_ids = ? AND pattern = ?",
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
                    (len(signals), _merge_signal_ids(existing["signal_ids"], signal_ids), existing["id"]),
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
            conn.execute("UPDATE signals SET incident_id = ? WHERE id IN (%s)" % ", ".join("?" * len(signal_ids)),
                         [incident_id] + signal_ids)
            assigned_ids.update(signal_ids)
            conn.commit()
            touched.append(incident_id)

        # Group 2: same signal_type across sessions within a 60-min window (3+)
        # Only signals NOT claimed by a Group 1 session cluster are candidates;
        # otherwise a signal in both clusters has its incident_id overwritten,
        # orphaning it from the session incident (which then can never resolve
        # it and keeps a stale signal_ids/signal_count forever).
        type_windows: Dict[str, list] = {}
        for row in signal_rows:
            if row["id"] in assigned_ids:
                continue
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
                    "SELECT id, signal_ids FROM incidents WHERE status = 'open' AND pattern = ? AND session_ids = ?",
                    (f"{stype}_burst", ",".join(sessions)),
                ).fetchone()
                if existing:
                    conn.execute(
                        "UPDATE incidents SET signal_count = signal_count + ?, signal_ids = ? WHERE id = ?",
                        (len(signal_ids), _merge_signal_ids(existing["signal_ids"], signal_ids), existing["id"]),
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
