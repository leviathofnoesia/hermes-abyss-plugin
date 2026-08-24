"""Abyss analytics — health score, trends, failure taxonomy, export, status.

Extracted from the plugin god-file (Clean Architecture, use-case layer).
All core dependencies (``_init_db``, ``_get_activity_conn``,
``_get_trace_conn``) are imported lazily inside the functions, exactly like
abyss_wave.py, so there is no import cycle with ``__init__``.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timedelta
from typing import Dict


def get_health() -> dict:
    """Compute an overall agent health score (0-100) and a breakdown.

    Modeled on Raindrop-style health panels:
      - error rate      (tool/LLM failures in the last 7 days vs window
                         activity — the rate is windowed to the same 7-day
                         horizon as signals/incidents so an old failure
                         backlog cannot keep dragging today's score) — 40 pts
      - open signals    (unacknowledged anomalies)             — 25 pts
      - open incidents  (unresolved clusters)                  — 25 pts
      - recent activity (24h liveliness / starvation guard)    — 10 pts
    """
    from __init__ import _get_activity_conn, _init_db

    _init_db()
    conn = _get_activity_conn()
    try:
        total = conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0]
        errors = conn.execute("SELECT COUNT(*) FROM activity WHERE status = 'error'").fetchone()[0]
        # Recency window: a backlog of old signals/incidents (e.g. a noisy cron
        # job) must not pin the score at "critical" forever. Only signals from
        # the last 7 days count against health. The error-rate component uses
        # the same 7-day window: previously it read all-time totals, so one bad
        # night would depress the score for weeks even after the agent returned
        # to health. All-time totals stay available in ``counts`` for context.
        week_ago = (datetime.now() - timedelta(days=7)).isoformat()
        total_7d = conn.execute(
            "SELECT COUNT(*) FROM activity WHERE timestamp >= ?", (week_ago,)
        ).fetchone()[0]
        errors_7d = conn.execute(
            "SELECT COUNT(*) FROM activity WHERE status = 'error' AND timestamp >= ?",
            (week_ago,),
        ).fetchone()[0]
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

        # Error rate (7d window): 0% -> 40, 50%+ -> 0
        err_ratio = (errors_7d / total_7d) if total_7d else 0.0
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
                "errors_7d": errors_7d,
                "activity_7d": total_7d,
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
    from __init__ import _get_activity_conn, _init_db

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
    from __init__ import _get_activity_conn, _init_db

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


def get_performance(days: int = 7, limit: int = 20) -> dict:
    """Latency percentiles for tools and LLM models over a window.

    Observability gap closed (2026-08-23): every tool call already stores
    ``duration_ms`` in the traces table and every API request stores
    model/provider/duration/tokens in the wave ``api_requests`` table, but no
    surface exposed the tail. Raindrop-style dashboards want "is the agent
    slow right now?" — this returns:

      - ``tools``:  per-tool call count, error count, p50/p90/p95/max
        duration_ms from the traces table (tool_call end events)
      - ``models``: per-model(provider) request count, error count, p50/p90/
        p95/max duration_ms plus token totals from api_requests
      - ``totals``: window totals for the UI chips

    ``days`` window, ``limit`` top-N per array (sorted by p95 desc).
    """
    from __init__ import _get_activity_conn, _get_trace_conn, _init_db

    _init_db()
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()

    def _pct(sorted_vals, pct):
        """Nearest-rank percentile on an ascending list (empty -> 0)."""
        if not sorted_vals:
            return 0
        idx = max(0, min(len(sorted_vals) - 1, int(math.ceil(pct / 100.0 * len(sorted_vals))) - 1))
        return sorted_vals[idx]

    # --- tool latency from traces (end-phase tool_call events) --------------
    tool_durs: Dict[str, list] = {}
    tool_errs: Dict[str, int] = {}
    tconn = _get_trace_conn()
    try:
        trows = tconn.execute(
            """SELECT event_data, duration_ms FROM traces
               WHERE event_type = 'tool_call' AND timestamp >= ?
                 AND duration_ms IS NOT NULL AND duration_ms > 0""",
            (cutoff,),
        ).fetchall()
    finally:
        tconn.close()
    for r in trows:
        try:
            ed = json.loads(r["event_data"] or "{}")
        except (ValueError, TypeError):
            ed = {}
        if ed.get("phase") != "end":
            continue
        tool = (ed.get("tool") or "unknown").strip() or "unknown"
        tool_durs.setdefault(tool, []).append(int(r["duration_ms"] or 0))
        if ed.get("status") == "error":
            tool_errs[tool] = tool_errs.get(tool, 0) + 1

    tools = []
    for tool, durs in tool_durs.items():
        s = sorted(durs)
        tools.append({
            "tool": tool,
            "count": len(durs),
            "errors": tool_errs.get(tool, 0),
            "p50_ms": _pct(s, 50),
            "p90_ms": _pct(s, 90),
            "p95_ms": _pct(s, 95),
            "max_ms": s[-1] if s else 0,
        })
    tools.sort(key=lambda x: x["p95_ms"], reverse=True)
    all_tools = tools  # full window (pre-truncation) — totals must not undercount
    tools = tools[:limit]

    # --- model latency from wave api_requests -------------------------------
    model_map: Dict[str, dict] = {}
    conn = _get_activity_conn()
    try:
        try:
            arows = conn.execute(
                """SELECT model, provider, status, duration_ms,
                          input_tokens, output_tokens FROM api_requests
                   WHERE timestamp >= ? AND duration_ms IS NOT NULL
                     AND duration_ms > 0""",
                (cutoff,),
            ).fetchall()
        except sqlite3.OperationalError:
            arows = []  # wave tables may not exist on a pre-wave DB
        for r in arows:
            model = (r["model"] or "unknown").strip() or "unknown"
            prov = (r["provider"] or "").strip() or "unknown"
            key = f"{model}|{prov}"
            entry = model_map.setdefault(key, {
                "model": model, "provider": prov, "count": 0, "errors": 0,
                "durations": [], "input_tokens": 0, "output_tokens": 0,
            })
            entry["count"] += 1
            if (r["status"] or "") != "completed":
                entry["errors"] += 1
            entry["durations"].append(int(r["duration_ms"] or 0))
            if isinstance(r["input_tokens"], int):
                entry["input_tokens"] += r["input_tokens"]
            if isinstance(r["output_tokens"], int):
                entry["output_tokens"] += r["output_tokens"]
    finally:
        conn.close()

    models = []
    for key, entry in model_map.items():
        s = sorted(entry["durations"])
        models.append({
            "model": entry["model"],
            "provider": entry["provider"],
            "count": entry["count"],
            "errors": entry["errors"],
            "p50_ms": _pct(s, 50),
            "p90_ms": _pct(s, 90),
            "p95_ms": _pct(s, 95),
            "max_ms": s[-1] if s else 0,
            "input_tokens": entry["input_tokens"],
            "output_tokens": entry["output_tokens"],
        })
    models.sort(key=lambda x: x["p95_ms"], reverse=True)
    all_models = models  # full window (pre-truncation) — totals must not undercount
    models = models[:limit]

    return {
        "days": days,
        "generated_at": datetime.now().isoformat(),
        "totals": {
            "tool_calls": sum(t["count"] for t in all_tools),
            "tool_errors": sum(t["errors"] for t in all_tools),
            "llm_requests": sum(m["count"] for m in all_models),
            "llm_errors": sum(m["errors"] for m in all_models),
            "input_tokens": sum(m["input_tokens"] for m in all_models),
            "output_tokens": sum(m["output_tokens"] for m in all_models),
        },
        "tools": tools,
        "models": models,
    }


def export_data() -> dict:
    """Full JSON snapshot of all Abyss tables — for backup/migration.

    Includes the core activity/signals/incidents/traces tables plus every
    wave table (streams, api_requests, subagents, approvals, commands,
    platform_events, skills, plugin_events) under ``wave``. The wave tables
    were added in the Aug-2026 expansion (#64182) and previously fell out of
    the backup entirely: an export taken today lost the richest telemetry
    (streaming stats, token/usage data, subagent runs, approval audit,
    platform events) while the UI kept showing them — so a restore silently
    produced a database that contradicted the UI. Pre-wave databases that
    predate a given wave table still export: missing tables are reported as
    ``{"_missing": [table, ...]}`` rather than failing the whole export.
    """
    from __init__ import _get_activity_conn, _get_trace_conn, _init_db

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
            "wave": {},
        }
        # Wave tables (#64182). Same graceful degradation as get_performance:
        # a pre-wave DB (or a DB created before a new wave table was added)
        # simply omits that table instead of aborting the backup.
        try:
            from abyss_wave import _WAVE_TABLES
            wave_tables = _WAVE_TABLES
        except Exception:
            wave_tables = ()
        missing = []
        for table in wave_tables:
            try:
                data["wave"][table] = dump(table)
            except sqlite3.OperationalError:
                missing.append(table)
        if missing:
            data["wave"]["_missing"] = missing
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
    from __init__ import _get_activity_conn, _init_db

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
        # Liveness metadata: activity_24h only says "was there activity in the
        # last 24h" — it cannot tell an operator HOW LONG the agent has been
        # silent (gateway down, hooks dropped, plugin misload). Expose the
        # last observed event timestamps so the statusbar/UI can render
        # "last event N minutes ago" and the health chip can distinguish a
        # quiet-but-alive agent from a dead one.
        last_activity = conn.execute(
            "SELECT timestamp FROM activity ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        last_signal = conn.execute(
            "SELECT timestamp FROM signals ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        last_error = conn.execute(
            "SELECT timestamp FROM activity WHERE status = 'error' ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
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
        "last_activity_at": last_activity[0] if last_activity else None,
        "last_signal_at": last_signal[0] if last_signal else None,
        "last_error_at": last_error[0] if last_error else None,
        "generated_at": datetime.now().isoformat(),
    }
