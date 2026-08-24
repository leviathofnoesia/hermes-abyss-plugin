"""Comprehensive tests for the Abyss plugin backend (Raindrop-style observability).

Runs against an isolated temp data dir (HERMES_PROFILE_HOME) so the live
profile's activity.db / traces.db are never touched. Exercises:

  - activity CRUD + list filters
  - trace recording + session traces
  - signal detection (structured + text classifiers: error, timeout,
    rate_limit, slow_call, loop_detected, vague_reply, refusal)
  - self-diagnostics
  - incident clustering (session clusters + type bursts + merge/dedupe)
  - triage: acknowledge/resolve signals, incident status transitions
  - retention pruning
  - stats, graph, search, calendar
  - slash command handler
"""
import os
import sys
import json
import tempfile
from pathlib import Path

# Point the plugin at an isolated data dir BEFORE importing it.
_TMP = tempfile.mkdtemp(prefix="abyss-test-")
os.environ["HERMES_PROFILE_HOME"] = _TMP
os.environ["ABYSS_RETENTION_DAYS"] = "365"  # never auto-prune test data

sys.path.insert(0, str(Path(__file__).resolve().parent))

import __init__  # noqa: E402
from __init__ import (  # noqa: E402
    _init_db, _add_activity, _add_trace, _record_self_diagnostic,
    _cluster_incidents, _detect_signals, _acknowledge_signal, _resolve_signal,
    _update_incident_status, _prune_data, handle_request,
    list_activity, get_session_trace, get_stats, get_graph_data,
    global_search, list_calendar, _handle_slash,
    get_health, get_trends, get_failures, export_data, get_status,
    get_performance,
    get_trace_graph, get_trace_timeline, get_agents_overview,
    get_recent_sessions,
)
from abyss_signals import _SIGNAL_PATTERNS  # noqa: E402

_init_db()

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}{' — ' + detail if detail else ''}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}{' — ' + detail if detail else ''}")


def reset_db():
    """Clear all tables between test groups."""
    conn = __init__._get_activity_conn()
    for t in ("activity", "signals", "incidents"):
        conn.execute(f"DELETE FROM {t}")
    conn.execute("DELETE FROM sqlite_sequence")
    conn.commit()
    conn.close()
    tconn = __init__._get_trace_conn()
    tconn.execute("DELETE FROM traces")
    tconn.execute("DELETE FROM sqlite_sequence")
    tconn.commit()
    tconn.close()



def _run_script():
    global PASS, FAIL
    print("=== 1. Activity recording ===")
    reset_db()
    r1 = _add_activity("tool_call_completed", "Called web_search", "tool", "completed",
                   metadata={"tool": "web_search"}, session_id="sess-a", tool_name="web_search")
    r2 = _add_activity("llm_call_completed", "Completed gpt-4", "llm", "completed",
                   metadata={"model": "gpt-4"}, session_id="sess-a")
    check("activity insert returns id", isinstance(r1.get("id"), int) and r1["id"] > 0)
    check("activity insert returns timestamp", bool(r1.get("timestamp")))
    acts = list_activity(limit=10)
    check("list_activity returns both", len(acts) == 2, f"got {len(acts)}")
    check("list_activity newest-first", acts[0]["id"] > acts[1]["id"])
    tool_acts = list_activity(limit=10, category="tool")
    check("list_activity category filter", len(tool_acts) == 1 and tool_acts[0]["category"] == "tool")
    since_acts = list_activity(limit=10, since=acts[1]["timestamp"])
    check("list_activity since filter", len(since_acts) >= 1)

    print("=== 2. Traces ===")
    reset_db()
    _add_trace("sess-t", "session_start", {"source": "cli"})
    _add_trace("sess-t", "tool_call", {"tool": "terminal", "phase": "end"}, duration_ms=42)
    trace = get_session_trace("sess-t")
    check("trace recorded", len(trace) == 2, f"got {len(trace)}")
    check("trace duration preserved", trace[1]["duration_ms"] == 42)

    print("=== 3. Signal detection ===")
    reset_db()
    # structured error
    sigs = _detect_signals("web_search", "some result", "sess-s", "error", 0,
                       error_type="api_error", error_message="boom", duration_ms=0)
    check("structured error -> tool_error", any(s["signal_type"] == "tool_error" for s in sigs))
    # text-based error on a FAILED call still fires (structured branch)
    sigs = _detect_signals("terminal", "Traceback (most recent call last)", "sess-s", "error", 0)
    check("text error on failed call -> tool_error", any(s["signal_type"] == "tool_error" for s in sigs))
    # R1 regression: a COMPLETED call whose result merely mentions an error
    # (grep output, build logs, read-file contents) must NOT fire tool_error
    sigs = _detect_signals("terminal", "Traceback (most recent call last)", "sess-s", "completed", 0)
    check("completed call w/ 'Traceback' text -> no tool_error", not any(s["signal_type"] == "tool_error" for s in sigs), str(sigs))
    # timeout via structured + text
    sigs = _detect_signals("terminal", "", "sess-s", "completed", 0, error_type="timeout")
    check("structured timeout -> timeout", any(s["signal_type"] == "timeout" for s in sigs))
    sigs = _detect_signals("terminal", "operation timed out", "sess-s", "completed", 0)
    check("text timeout -> timeout", any(s["signal_type"] == "timeout" for s in sigs))
    # exit-124 triple-fire guard: exactly one timeout, no tool_error, no slow_call
    sigs = _detect_signals("terminal", "", "sess-s", "error", 0, error_message="exit 124", duration_ms=125000)
    types = [s["signal_type"] for s in sigs]
    check("exit 124 -> timeout only (no tool_error/slow_call)", types == ["timeout"], str(sigs))
    # bare exit code -> warning-severity tool_error with exit_code detail
    sigs = _detect_signals("terminal", "", "sess-s", "error", 0, error_message="exit 1")
    te = [s for s in sigs if s["signal_type"] == "tool_error"]
    check("bare 'exit 1' -> warning tool_error w/ exit_code", len(te) == 1 and te[0]["severity"] == "warning" and te[0]["details"].get("exit_code") == "1", str(sigs))
    # rate limit
    sigs = _detect_signals("web_search", "", "sess-s", "completed", 0,
                       error_type="rate_limit", error_message="429 Too Many Requests")
    check("structured rate_limit -> rate_limit", any(s["signal_type"] == "rate_limit" for s in sigs))
    # R3 regression: result text mentioning rate/credit on a COMPLETED call must not fire
    sigs = _detect_signals("read_file", "the quota is exhausted: 429 too many requests", "sess-s", "completed", 0)
    check("completed call w/ '429'/'quota' text -> no rate_limit", not any(s["signal_type"] == "rate_limit" for s in sigs), str(sigs))
    # slow call
    sigs = _detect_signals("terminal", "ok", "sess-s", "completed", 0, duration_ms=120000)
    check("slow duration -> slow_call", any(s["signal_type"] == "slow_call" for s in sigs))
    # vague reply
    sigs = _detect_signals("llm_call_completed", "ok", "sess-s", "completed", 0)
    check("short llm -> vague_reply", any(s["signal_type"] == "vague_reply" for s in sigs))
    # refusal
    sigs = _detect_signals("llm_call_completed", "I don't know how to do that.", "sess-s", "completed", 0)
    check("refusal phrase -> refusal", any(s["signal_type"] == "refusal" for s in sigs))
    # false-positive guard: benign output merely CONTAINING "error"/"timeout" words
    # (e.g. test logs with error_rate / "errors" / {"error": ...} keys) must NOT fire
    sigs = _detect_signals("terminal", "[PASS] error_rate=0.5 errors=2 by_message=429 rate limit", "sess-s", "completed", 0)
    check("benign 'error' substring -> no tool_error", not any(s["signal_type"] == "tool_error" for s in sigs), str(sigs))
    sigs = _detect_signals("terminal", '{"error": "json key", "timeout": 30}', "sess-s", "completed", 0)
    check("json key 'error' -> no tool_error", not any(s["signal_type"] == "tool_error" for s in sigs))
    sigs = _detect_signals("terminal", "timeout: 30s configured", "sess-s", "completed", 0)
    check("benign 'timeout' word -> no timeout signal", not any(s["signal_type"] == "timeout" for s in sigs), str(sigs))
    # real signatures still fire (on FAILED calls)
    sigs = _detect_signals("terminal", "bash: command not found", "sess-s", "error", 0)
    check("'command not found' -> tool_error", any(s["signal_type"] == "tool_error" for s in sigs))
    # dedupe: same type not double-counted
    sigs = _detect_signals("terminal", "error: timeout", "sess-s", "error", 0)
    types = [s["signal_type"] for s in sigs]
    check("no duplicate signal types", len(types) == len(set(types)), f"{types}")
    # loop detection: 3 identical tool calls in same session
    reset_db()
    for i in range(3):
        _add_activity("tool_call_completed", f"web_search #{i}", "tool", "completed",
                      session_id="sess-loop", tool_name="web_search", args={"q": "same"})
    sigs = _detect_signals("web_search", "result", "sess-loop", "completed", 3)
    check("3 identical calls -> loop_detected", any(s["signal_type"] == "loop_detected" for s in sigs))

    # context_loss: provider rejection with context-window evidence (structured)
    sigs = _detect_signals("web_search", "", "sess-s", "error", 0,
                       error_type="api_error",
                       error_message="maximum context length exceeded (128k tokens)")
    check("context-window error -> context_loss", any(s["signal_type"] == "context_loss" for s in sigs), str(sigs))
    sigs = _detect_signals("llm_call_completed", "", "sess-s", "error", 0,
                       error_type="context_window_exceeded", error_message="too many tokens")
    check("too many tokens -> context_loss", any(s["signal_type"] == "context_loss" for s in sigs), str(sigs))
    # no context_loss on COMPLETED calls whose result merely mentions context
    sigs = _detect_signals("read_file", "the context window is 128k tokens", "sess-s", "completed", 0)
    check("completed call w/ 'context window' text -> no context_loss", not any(s["signal_type"] == "context_loss" for s in sigs), str(sigs))
    # interaction guard: 'context window exhausted' -> context_loss but NOT rate_limit
    sigs = _detect_signals("web_search", "", "sess-s", "error", 0,
                       error_type="api_error", error_message="context window exhausted")
    types = [s["signal_type"] for s in sigs]
    check("ctx exhausted -> context_loss, not rate_limit", "context_loss" in types and "rate_limit" not in types, str(sigs))
    # adapter: a plain '429 Too Many Requests' still fires rate_limit (no ctx tokens)
    sigs = _detect_signals("web_search", "", "sess-s", "error", 0,
                       error_type="rate_limit", error_message="429 Too Many Requests")
    check("plain 429 -> rate_limit", any(s["signal_type"] == "rate_limit" for s in sigs), str(sigs))
    # persona drift: strong first-person identity claim on LLM response
    sigs = _detect_signals("llm_call_completed", "I am not an AI assistant. My real name is John.", "sess-s", "completed", 0)
    check("identity claim -> drift_detected", any(s["signal_type"] == "drift_detected" for s in sigs), str(sigs))
    # benign LLM responses never fire drift
    sigs = _detect_signals("llm_call_completed", "I am an AI assistant. Here is the summary you asked for.", "sess-s", "completed", 0)
    check("normal 'I am an AI' reply -> no drift_detected", not any(s["signal_type"] == "drift_detected" for s in sigs), str(sigs))
    # taxonomy completeness: every declared signal type has an emitter
    declared = {p[0] for p in _SIGNAL_PATTERNS}
    emitted = {"tool_error", "timeout", "rate_limit", "slow_call", "loop_detected",
               "vague_reply", "refusal", "empty_result", "context_loss", "drift_detected"}
    check("all declared signal types emitted", declared.issubset(emitted), f"unemitted: {declared - emitted}")

    print("=== 4. Self-diagnostics ===")
    reset_db()
    sid = _record_self_diagnostic("sess-d", "web_search", "rate limit exhausted", "warning")
    check("self-diagnostic recorded", sid is not None and sid > 0)
    sigs_rows = handle_request("GET", "/signals", {})
    check("self-diagnostic in /signals", any(s["signal_type"] == "self_diagnostic" for s in sigs_rows))

    print("=== 5. Incident clustering ===")
    reset_db()
    # session cluster: 2 signals in same session -> incident
    _record_self_diagnostic("sess-x", "tool_a", "gap one")
    _record_self_diagnostic("sess-x", "tool_b", "gap two")
    incs = _cluster_incidents()
    check("session cluster created", len(incs) == 1, f"got {incs}")
    inc_rows = handle_request("GET", "/incidents", {})
    check("incident persisted", len(inc_rows) == 1 and inc_rows[0]["pattern"] == "multi_signal")
    check("incident links signal ids", bool(inc_rows[0].get("signal_ids")))
    # re-cluster: no new incident for same session+pattern
    incs2 = _cluster_incidents()
    check("re-cluster dedupes", len(incs2) == 0, f"got {incs2}")
    # burst cluster: 3+ same type across sessions within 60 min
    reset_db()
    for i in range(3):
        _add_activity("tool_call_completed", f"timeout #{i}", "tool", "error",
                      session_id=f"burst-{i}", tool_name="web_search")
        __init__._record_signals(
            [{"signal_type": "timeout", "severity": "warning", "label": "Timeout",
              "description": "timeout", "details": {}}],
            f"burst-{i}", i + 1)
    incs3 = _cluster_incidents()
    inc_rows2 = handle_request("GET", "/incidents", {})
    check("type burst cluster created", any(r["pattern"] == "timeout_burst" for r in inc_rows2))

    # steal regression: Group 2 must NOT re-claim signals already clustered by
    # Group 1. Old code re-scanned the same snapshot and overwrote incident_id
    # on session-clustered signals, orphaning them from the session incident
    # (which then could never resolve them) and drifting signal_ids/count.
    reset_db()
    # Session with 2 signals -> multi_signal incident (Group 1 claims both).
    sig_a1 = __init__._record_signals(
        [{"signal_type": "timeout", "severity": "warning", "label": "Timeout",
          "description": "timeout in session", "details": {}}],
        "steal-sess", 9001)[0]["id"]
    sig_a2 = __init__._record_signals(
        [{"signal_type": "tool_error", "severity": "error", "label": "Tool Error",
          "description": "error in session", "details": {}}],
        "steal-sess", 9002)[0]["id"]
    # Three OTHER sessions each get a timeout -> Group 2 burst candidates.
    burst_ids = []
    for i in range(3):
        rec = __init__._record_signals(
            [{"signal_type": "timeout", "severity": "warning", "label": "Timeout",
              "description": f"burst timeout {i}", "details": {}}],
            f"steal-burst-{i}", 9003 + i)
        burst_ids.append(rec[0]["id"])
    _cluster_incidents()
    inc_rows_st = handle_request("GET", "/incidents", {})
    multi_st = next((r for r in inc_rows_st if r["pattern"] == "multi_signal"), None)
    burst_st = next((r for r in inc_rows_st if r["pattern"] == "timeout_burst"), None)
    check("steal regression: multi_signal + burst incidents coexist",
          multi_st is not None and burst_st is not None,
          str([r["pattern"] for r in inc_rows_st]))
    conn_st = __init__._get_activity_conn()
    try:
        sig_a1_inc = conn_st.execute(
            "SELECT incident_id FROM signals WHERE id = ?", (sig_a1,)).fetchone()
    finally:
        conn_st.close()
    check("steal regression: session timeout stays on multi_signal incident",
          multi_st is not None and sig_a1_inc and sig_a1_inc["incident_id"] == multi_st["id"],
          f"signal {sig_a1} -> {dict(sig_a1_inc) if sig_a1_inc else None}")
    burst_members = set(json.loads(burst_st["signal_ids"])) if burst_st else set()
    check("steal regression: burst excludes session-clustered timeout",
          burst_members == set(burst_ids), f"burst {burst_members} expected {set(burst_ids)}")
    # Resolving the session incident resolves BOTH its signals; burst untouched.
    _update_incident_status(multi_st["id"], "resolved")
    conn_st2 = __init__._get_activity_conn()
    try:
        a1_resolved = conn_st2.execute(
            "SELECT resolved FROM signals WHERE id = ?", (sig_a1,)).fetchone()
        a2_resolved = conn_st2.execute(
            "SELECT resolved FROM signals WHERE id = ?", (sig_a2,)).fetchone()
        b0_resolved = conn_st2.execute(
            "SELECT resolved FROM signals WHERE id = ?", (burst_ids[0],)).fetchone()
    finally:
        conn_st2.close()
    check("steal regression: session resolution resolves its timeout signal",
          a1_resolved and a1_resolved["resolved"] == 1)
    check("steal regression: session resolution resolves its error signal",
          a2_resolved and a2_resolved["resolved"] == 1)
    check("steal regression: burst signals untouched by session resolution",
          b0_resolved and b0_resolved["resolved"] == 0)

    # merge-union regression: merging a second batch must UNION signal_ids,
    # not replace them (old code dropped the first batch's membership while
    # signal_count kept growing -> count and members drifted apart).
    reset_db()
    m1 = __init__._record_signals(
        [{"signal_type": "tool_error", "severity": "error", "label": "Tool Error",
          "description": "m1", "details": {}}], "merge-sess", 9101)[0]["id"]
    m2 = __init__._record_signals(
        [{"signal_type": "tool_error", "severity": "error", "label": "Tool Error",
          "description": "m2", "details": {}}], "merge-sess", 9102)[0]["id"]
    _cluster_incidents()
    m3 = __init__._record_signals(
        [{"signal_type": "tool_error", "severity": "error", "label": "Tool Error",
          "description": "m3", "details": {}}], "merge-sess", 9103)[0]["id"]
    m4 = __init__._record_signals(
        [{"signal_type": "tool_error", "severity": "error", "label": "Tool Error",
          "description": "m4", "details": {}}], "merge-sess", 9104)[0]["id"]
    _cluster_incidents()
    merge_rows = handle_request("GET", "/incidents", {})
    merge_inc = next((r for r in merge_rows if r["pattern"] == "multi_signal"), None)
    check("merge-union regression: single incident after two batches",
          merge_inc is not None and len(merge_rows) == 1, str([r["pattern"] for r in merge_rows]))
    check("merge-union regression: all 4 signals linked",
          merge_inc is not None and set(json.loads(merge_inc["signal_ids"])) == {m1, m2, m3, m4},
          str(merge_inc.get("signal_ids") if merge_inc else None))
    check("merge-union regression: signal_count matches union",
          merge_inc is not None and merge_inc["signal_count"] == 4,
          str(merge_inc.get("signal_count") if merge_inc else None))

    print("=== 6. Triage ===")
    reset_db()
    _rec = _record_self_diagnostic("sess-t", "cap", "gap")
    row = _acknowledge_signal(_rec)
    check("acknowledge signal", row and row["acknowledged"] == 1)
    row = _resolve_signal(_rec)
    check("resolve signal", row and row["resolved"] == 1 and row["acknowledged"] == 1)
    _record_self_diagnostic("sess-t", "cap2", "gap2")
    _record_self_diagnostic("sess-t", "cap3", "gap3")
    inc_id = _cluster_incidents()[0]
    row = _update_incident_status(inc_id, "acknowledged")
    check("incident -> acknowledged", row and row["status"] == "acknowledged")
    row = _update_incident_status(inc_id, "resolved")
    check("incident -> resolved", row and row["status"] == "resolved")
    conn = __init__._get_activity_conn()
    remaining_open = conn.execute("SELECT COUNT(*) FROM signals WHERE resolved = 0").fetchone()[0]
    conn.close()
    check("resolve incident resolves linked signals", remaining_open == 0)
    row = _update_incident_status(inc_id, "open")
    check("incident -> reopen", row and row["status"] == "open")

    print("=== 7. Pruning ===")
    reset_db()
    old_ts = "2000-01-01T00:00:00"
    conn = __init__._get_activity_conn()
    conn.execute("INSERT INTO activity (timestamp, action) VALUES (?, 'old')", (old_ts,))
    conn.execute("INSERT INTO signals (timestamp, signal_type) VALUES (?, 'old_sig')", (old_ts,))
    conn.commit()
    conn.close()
    tconn = __init__._get_trace_conn()
    tconn.execute("INSERT INTO traces (session_id, event_type, timestamp) VALUES ('s', 'e', ?)", (old_ts,))
    tconn.commit()
    tconn.close()
    deleted = _prune_data(days=30)
    check("prune deletes old activity", deleted.get("activity") == 1, f"{deleted}")
    check("prune deletes old signals", deleted.get("signals") == 1, f"{deleted}")
    check("prune deletes old traces", deleted.get("traces") == 1, f"{deleted}")
    check("prune 0 days no-op", _prune_data(0)["activity"] == 0)

    print("=== 8. Stats / Graph / Search / Calendar ===")
    reset_db()
    _add_activity("tool_call_completed", "Called web_search", "tool", "completed",
              session_id="sess-g", tool_name="web_search")
    _add_activity("tool_call_completed", "Called terminal", "tool", "error",
              session_id="sess-g", tool_name="terminal")
    stats = get_stats()
    check("stats has total", stats["total_activities"] == 2)
    check("stats has error count", stats["errors"] == 1 and stats["error_rate"] == 0.5)
    check("stats has signals", "signals_total" in stats and "incidents_open" in stats)
    check("stats has top_tools", len(stats["top_tools"]) >= 1)
    check("stats has 24h activity", stats["activity_24h"] >= 1)
    graph = get_graph_data(limit=50)
    check("graph has nodes", len(graph["nodes"]) >= 2, f"{len(graph['nodes'])} nodes")
    check("graph has session node", any(n["type"] == "session" for n in graph["nodes"]))
    check("graph has tool node", any(n["type"] == "tool" for n in graph["nodes"]))
    sr = global_search("web_search", limit=5)
    check("search finds activity", any(r["source"] == "activity" for r in sr))
    cal = list_calendar("2000-01-01T00:00:00", "2100-01-01T00:00:00")
    check("calendar lists activities", any(c["category"] == "tool" for c in cal))
    cal_tool = next((c for c in cal if c["category"] == "tool"), None)
    check("calendar rows carry session_id (trace drill)",
          bool(cal_tool) and cal_tool.get("session_id") == "sess-g", str(cal_tool))
    check("calendar rows carry tool_name",
          bool(cal_tool) and cal_tool.get("tool_name") == "web_search", str(cal_tool))

    print("=== 8b. Calendar cron store (jobs.json + trace drill) ===")
    # The live Hermes cron store is a single jobs.json with a `jobs:` array;
    # older layouts used one <jobid>.json per job. Both must parse, and cron
    # chips must carry the session_id of the job's most recent Abyss-tracked
    # run so the UI's `trace ›` affordance lights up.
    cron_dir = Path(_TMP) / "cron"
    cron_dir.mkdir(exist_ok=True)
    (cron_dir / "jobs.json").write_text(json.dumps({
        "jobs": [
            {
                "id": "jobAAA", "name": "Night Shift A",
                "prompt": "Run the Abyss backend night shift.",
                "schedule": {"kind": "interval", "minutes": 60, "display": "every 1h"},
                "next_run_at": "2026-08-03T04:00:00",
                "enabled": True, "deliver": "origin",
            },
            {
                "id": "jobBBB", "name": "No Runs Yet",
                "prompt": "short",
                "schedule_display": "daily",
                "next_run_at": "2026-08-04T00:00:00",
                "enabled": False,
            },
        ]
    }, indent=2))
    # Legacy per-job file: no top-level id -> falls back to f.stem; string
    # schedule must survive (UI renders it directly).
    (cron_dir / "jobCCC.json").write_text(json.dumps({
        "name": "Legacy Job",
        "schedule": "every 30m",
        "next_run": "2026-08-05T00:00:00",
        "enabled": True,
    }))
    conn = __init__._get_activity_conn()
    for sid, ts in [
        ("cron_jobAAA_20260801_010000", "2026-08-01T01:00:00"),
        ("cron_jobAAA_20260802_020000", "2026-08-02T02:00:00"),
        ("cron_jobCCC_20260803_030000", "2026-08-03T03:00:00"),
    ]:
        conn.execute(
            "INSERT INTO activity (timestamp, action, description, category, status, session_id) "
            "VALUES (?, 'tool_call_completed', 'ran', 'tool', 'completed', ?)",
            (ts, sid),
        )
    conn.commit()
    conn.close()

    cal2 = list_calendar("2000-01-01T00:00:00", "2100-01-01T00:00:00")
    cron_rows = [c for c in cal2 if c["category"] == "cron"]
    check("calendar parses jobs.json array (no 'jobs' chip)",
          len(cron_rows) == 3 and not any(c["id"] == "jobs" for c in cron_rows), f"got {len(cron_rows)}")
    ca = next((c for c in cron_rows if c["id"] == "jobAAA"), None)
    check("calendar cron row schedule from schedule.display",
          bool(ca) and ca.get("schedule") == "every 1h", str(ca))
    check("calendar cron row next_run from next_run_at",
          bool(ca) and ca.get("next_run") == "2026-08-03T04:00:00", str(ca))
    check("calendar cron row carries MOST RECENT run session_id (trace drill)",
          bool(ca) and ca.get("session_id") == "cron_jobAAA_20260802_020000", str(ca))
    cb = next((c for c in cron_rows if c["id"] == "jobBBB"), None)
    check("calendar cron row without tracked run has empty session_id",
          bool(cb) and cb.get("session_id") == "" and cb.get("enabled") is False, str(cb))
    cc = next((c for c in cron_rows if c["id"] == "jobCCC"), None)
    check("calendar legacy per-job .json still parsed (f.stem id + string schedule)",
          bool(cc) and cc.get("schedule") == "every 30m" and cc.get("session_id") == "cron_jobCCC_20260803_030000", str(cc))
    conn = __init__._get_activity_conn()
    conn.execute("DELETE FROM activity WHERE session_id LIKE 'cron\\_%' ESCAPE '\\'")
    conn.commit()
    conn.close()

    print("=== 9. Slash commands ===")
    h = _handle_slash("stats")
    check("/abyss stats", "Total entries" in h and "Abyss Stats" in h)
    h = _handle_slash("recent 5")
    check("/abyss recent", "activity entr" in h)
    __init__._add_activity("drill test", "drill test desc", "tool", "completed",
                          session_id="drill-sess", tool_name="web_search")
    h = _handle_slash("recent --session=drill-sess")
    check("/abyss recent --session filters", "1 activity entr" in h and "drill test" in h, h[:160])
    h = _handle_slash("recent --session=no-such-session-xyz")
    check("/abyss recent --session empty", "No activity entries" in h, h[:160])
    h = _handle_slash("search web_search")
    check("/abyss search", "Search results" in h)
    h = _handle_slash("help")
    check("/abyss help", "Subcommands" in h and "ack" in h and "prune" in h)
    h = _handle_slash("nonsense")
    check("/abyss nonsense -> unknown", "Unknown subcommand" in h)

    print("=== 10. Health / Trends / Failures / Export / Status ===")
    reset_db()
    _add_activity("tool_call_completed", "ok", "tool", "error", session_id="h1",
              tool_name="web_search", metadata={"error_message": "429 rate limit exceeded"})
    _add_activity("tool_call_completed", "ok", "tool", "error", session_id="h2",
              tool_name="web_search", metadata={"error_message": "429 rate limit exceeded"})
    _add_activity("llm_call_completed", "done", "llm", "completed", session_id="h1")
    _record_self_diagnostic("h1", "web_search", "rate limited")
    health = get_health()
    check("health has score", 0 <= health["score"] <= 100)
    check("health has level", health["level"] in ("healthy", "fair", "degraded", "critical"))
    check("health counts errors", health["counts"]["errors"] == 2)
    trends = get_trends(days=1, bucket="hour")
    check("trends hourly buckets", len(trends["timestamps"]) > 0)
    check("trends activity sums", sum(trends["activity"]) >= 3)
    check("trends errors sum", sum(trends["errors"]) == 2)
    fail = get_failures(limit=10)
    check("failures by_type", any(x["type"] == "self_diagnostic" for x in fail["by_type"]))
    check("failures by_tool", any(x["tool"] == "web_search" for x in fail["by_tool"]))
    check("failures by_message", any("429" in x["message"] for x in fail["by_message"]))
    export = export_data()
    check("export activity rows", len(export["activity"]) == 3)
    check("export signals rows", len(export["signals"]) >= 1)
    check("export traces rows", "traces" in export)
    check("export wave key present", "wave" in export)
    check("export wave has streams table", "streams" in export.get("wave", {}))
    check("export wave includes api_requests", "api_requests" in export.get("wave", {}))
    check("export wave includes plugin_events", "plugin_events" in export.get("wave", {}))
    status = get_status()
    check("status chip fields", {"score", "level", "signals_open", "incidents_open", "activity_24h"} <= set(status.keys()))
    # triage timestamps
    conn = __init__._get_activity_conn()
    sig_row = conn.execute("SELECT id FROM signals WHERE signal_type = 'self_diagnostic' LIMIT 1").fetchone()
    conn.close()
    _res = _resolve_signal(sig_row["id"])
    check("resolve stamps resolved_at", bool(_res and _res.get("resolved_at")))
    h = _handle_slash("health")
    check("/abyss health", "Abyss Health" in h and "/100" in h)
    h = _handle_slash("trends 1 hour")
    check("/abyss trends", "Abyss trends" in h)
    h = _handle_slash("failures 5")
    check("/abyss failures", "Abyss failure taxonomy" in h)
    h = _handle_slash("export")
    check("/abyss export", '"activity"' in h)
    check("/abyss export wave summary", '"wave"' in h)
    h = _handle_slash("webhook")
    check("/abyss webhook (unset)", "not configured" in h)
    h = _handle_slash("webhook off")
    check("/abyss webhook off", "disabled" in h)
    # handle_request routes
    check("GET /health route", handle_request("GET", "/health", {}).get("score") is not None)
    check("GET /trends route", "timestamps" in handle_request("GET", "/trends", {"days": 1, "bucket": "hour"}))
    check("GET /failures route", "by_tool" in handle_request("GET", "/failures", {}))
    check("GET /export route", "activity" in handle_request("GET", "/export", {}))
    check("GET /status route", "score" in handle_request("GET", "/status", {}))
    # Windowed error-rate regression (R-2026-08-23): the health error-rate
    # component must use the same 7-day window as signals/incidents. An old
    # failure backlog (here: a single error from year 2000) must NOT drag
    # today's error_score; all-time totals stay visible in counts.
    reset_db()
    _add_activity("tool_call_completed", "clean work", "tool", "completed",
                  session_id="w1", tool_name="terminal")
    health = get_health()
    check("clean window error_score maxed", health["components"]["error_score"] == 40.0,
          f"{health['components']}")
    _conn = __init__._get_activity_conn()
    _conn.execute(
        "INSERT INTO activity (timestamp, action, description, category, status, session_id, tool_name) "
        "VALUES (?, 'old_err', 'stale failure', 'tool', 'error', 'w1', 'terminal')",
        ("2000-01-01T00:00:00",),
    )
    _conn.commit()
    _conn.close()
    health = get_health()
    check("counts expose 7d error fields", "errors_7d" in health["counts"] and "activity_7d" in health["counts"],
          f"{list(health['counts'].keys())}")
    check("old error excluded from 7d error rate", health["counts"]["errors_7d"] == 0,
          f"{health['counts']}")
    check("old error still in all-time errors", health["counts"]["errors"] == 1,
          f"{health['counts']}")
    check("stale failure does not drag error_score", health["components"]["error_score"] == 40.0,
          f"{health['components']}")

    print("=== 11. Agent-powered resolution + doctor (stub agent) ===")
    reset_db()
    import subprocess as _sp
    import time as _time

    # Stub agent: a tiny script that mimics the dispatched `hermes chat -q` agent.
    # It writes the report JSON to ABYSS_REPORT_PATH (like the real agent would)
    # and exits, so the backend worker finalizes exactly as in production.
    _stub = Path(_TMP) / "stub_agent.py"
    import textwrap
    _stub.write_text(textwrap.dedent('''
        import json, os
        rp = os.environ.get("ABYSS_REPORT_PATH", "")
        role = os.environ.get("ABYSS_AGENT_ROLE", "resolver")
        report = {
            "schema": "abyss-resolution/1",
            "role": role,
            "report_id": os.path.basename(rp).replace(".json", "") if rp else "",
            "status": os.environ.get("ABYSS_STUB_STATUS", "succeeded"),
            "summary": os.environ.get("ABYSS_STUB_SUMMARY", "stub fixed it"),
            "findings": [{"title": "stub finding", "detail": "stub", "evidence": "stub"}],
            "actions_taken": ["stubbed"],
            "proposed_fixes": json.loads(os.environ.get("ABYSS_STUB_PROPOSED", "[]")),
            "fixes": json.loads(os.environ.get("ABYSS_STUB_FIXES", "[]")),
            "skills_saved": ["abyss-fix-stub"],
            "error": None,
        }
        if rp:
            with open(rp, "w", encoding="utf-8") as f:
                json.dump(report, f)
        print(report["summary"])
    '''), encoding="utf-8")
    os.environ["ABYSS_AGENT_CMD"] = json.dumps([sys.executable, str(_stub)])
    os.environ["ABYSS_AGENT_TIMEOUT"] = "30"
    os.environ["ABYSS_STUB_STATUS"] = "succeeded"
    os.environ["ABYSS_STUB_SUMMARY"] = "stub fixed the issue"
    os.environ["ABYSS_STUB_PROPOSED"] = "[]"
    os.environ["ABYSS_STUB_FIXES"] = "[]"


    def _wait_for(pred, timeout=15):
        deadline = _time.time() + timeout
        while _time.time() < deadline:
            if pred():
                return True
            _time.sleep(0.2)
        return False


    # 11a. Signal resolve-agent -> succeeded
    sig_id = _record_self_diagnostic("rsess", "terminal", "boom")
    res = handle_request("POST", f"/signals/{sig_id}/resolve-agent", {})
    check("signal resolve-agent dispatches", res.get("status") == "dispatched" and bool(res.get("report_id")), str(res))
    ok = _wait_for(lambda: handle_request("GET", "/signals", {})[0].get("resolution_status") == "succeeded")
    check("resolver agent marks signal resolved", ok)
    srow = handle_request("GET", "/signals", {})[0]
    check("resolver stores note + resolved flag", srow["resolved"] == 1 and srow["resolution_note"] == "stub fixed the issue", str(srow))

    # 11b. Signal resolve-agent -> failed (stays open, note kept)
    os.environ["ABYSS_STUB_STATUS"] = "failed"
    os.environ["ABYSS_STUB_SUMMARY"] = "could not fix: no root cause found"
    sig2 = _record_self_diagnostic("rsess2", "cap", "gap")
    res = handle_request("POST", f"/signals/{sig2}/resolve-agent", {})
    check("failed signal resolve-agent dispatches", res.get("status") == "dispatched")
    ok = _wait_for(lambda: handle_request("GET", "/signals", {})[0].get("resolution_status") == "failed")
    check("failed resolver leaves signal open", ok)
    srow = handle_request("GET", "/signals", {})[0]
    check("failed resolver keeps resolved=0 + note", srow["resolved"] == 0 and "no root cause" in (srow["resolution_note"] or ""), str(srow))

    # 11c. Incident resolve-agent -> resolves incident + linked signals
    os.environ["ABYSS_STUB_STATUS"] = "succeeded"
    os.environ["ABYSS_STUB_SUMMARY"] = "fixed the incident cluster"
    sig_a = _record_self_diagnostic("isess", "c1", "g1")
    sig_b = _record_self_diagnostic("isess", "c2", "g2")
    inc_id = _cluster_incidents()[0]
    res = handle_request("POST", f"/incidents/{inc_id}/resolve-agent", {})
    check("incident resolve-agent dispatches", res.get("status") == "dispatched", str(res))
    ok = _wait_for(lambda: handle_request("GET", "/incidents", {})[0]["status"] == "resolved")
    check("incident resolved by agent", ok)
    irow = handle_request("GET", "/incidents", {})[0]
    check("incident stores note", irow["resolution_note"] == "fixed the incident cluster", str(irow))
    conn = __init__._get_activity_conn()
    remaining = conn.execute("SELECT COUNT(*) FROM signals WHERE resolved = 0 AND incident_id = ?", (inc_id,)).fetchone()[0]
    conn.close()
    check("linked signals resolved by incident", remaining == 0)

    # 11d. Doctor: run -> report ready -> approve -> apply resolves targets
    reset_db()
    sig_d = _record_self_diagnostic("dsess", "cap", "gap")
    os.environ["ABYSS_STUB_STATUS"] = "succeeded"
    os.environ["ABYSS_STUB_SUMMARY"] = "diagnosis complete"
    os.environ["ABYSS_STUB_PROPOSED"] = json.dumps([
    {"id": "fix-1", "title": "fix the thing", "action": "do it", "target_signals": [sig_d], "target_incidents": []}
    ])
    os.environ["ABYSS_STUB_FIXES"] = "[]"
    res = handle_request("POST", "/doctor/run", {})
    check("doctor run dispatches", res.get("status") == "dispatched" and bool(res.get("report_id")), str(res))
    rid = res["report_id"]
    ok = _wait_for(lambda: handle_request("GET", "/doctor/report", {"report_id": rid}).get("status") == "ready")
    check("doctor report becomes ready", ok)
    rep = handle_request("GET", "/doctor/report", {"report_id": rid})
    check("doctor report has proposed fixes", rep["report"]["proposed_fixes"][0]["target_signals"] == [sig_d], str(rep)[:200])
    # approve missing report -> 404
    r = handle_request("POST", "/doctor/approve", {}, json.dumps({"report_id": "nope"}))
    check("approve missing report -> 404", r.get("code") == 404)
    # approve -> apply stub writes fixes[] -> backend resolves target
    os.environ["ABYSS_STUB_FIXES"] = json.dumps([
    {"id": "fix-1", "status": "applied", "note": "done it", "skill_saved": "abyss-fix-stub", "target_signals": [sig_d], "target_incidents": []}
    ])
    os.environ["ABYSS_STUB_SUMMARY"] = "applied the fix"
    res2 = handle_request("POST", "/doctor/approve", {}, json.dumps({"report_id": rid}))
    check("doctor approve dispatches", res2.get("status") == "dispatched" and res2.get("fix_count") == 1, str(res2))
    ok = _wait_for(lambda: handle_request("GET", "/signals", {})[0].get("resolution_status") == "succeeded")
    check("approved fix resolves target signal", ok)
    srow = handle_request("GET", "/signals", {})[0]
    check("approved fix note stored", srow["resolved"] == 1 and "done it" in (srow["resolution_note"] or ""), str(srow))
    # bad report id shape
    r = handle_request("GET", "/doctor/report", {"report_id": "../evil"})
    check("doctor report rejects bad id", r.get("status") == "invalid")

    print("=== 12. Resolutions hygiene (retention sweep) ===")
    _tmp_hygiene = Path(_TMP) / "hygiene"
    _tmp_hygiene.mkdir(parents=True, exist_ok=True)
    _old = _tmp_hygiene / "doctor-old-1.json"
    _old.write_text('{"role":"doctor","status":"succeeded","summary":"old"}', encoding="utf-8")
    (_tmp_hygiene / "doctor-old-1.log").write_text("old", encoding="utf-8")
    _active = _tmp_hygiene / "doctor-active-1.json"
    _active.write_text('{"role":"doctor","status":"in_progress","summary":"active"}', encoding="utf-8")
    (_tmp_hygiene / "doctor-active-1.log").write_text("active", encoding="utf-8")
    _past = _time.time() - 40 * 86400
    os.utime(_old, (_past, _past))
    os.utime(_tmp_hygiene / "doctor-old-1.log", (_past, _past))
    _prev_dir = __init__._RESOLUTION_DIR
    __init__._RESOLUTION_DIR = _tmp_hygiene
    try:
        _pr = __init__._prune_resolutions(retention_days=30, keep_recent=0, safety_hours=0)
    finally:
        __init__._RESOLUTION_DIR = _prev_dir
    check("hygiene deletes old resolution artifacts", _pr.get("deleted", 0) >= 2 and not _old.exists())
    check("hygiene preserves in_progress reports", _active.exists())
    import shutil
    shutil.rmtree(_tmp_hygiene, ignore_errors=True)

    print("=== 13. Hardening regressions (release pass) ===")
    # 13a. Malformed numeric params -> clean 400, never a 500
    r = handle_request("GET", "/activity", {"limit": "abc"})
    check("bad limit -> 400", r.get("code") == 400, str(r)[:120])
    r = handle_request("GET", "/trends", {"days": "x"})
    check("bad days -> 400", r.get("code") == 400, str(r)[:120])
    r = handle_request("GET", "/signals", {"limit": None})
    check("null limit -> default ok", isinstance(r, list), str(r)[:120])

    # 13b. _coerce_body tolerates dict/bytes bodies (no 500)
    r = handle_request("POST", "/activity", {}, {"action": "dict-body-test", "category": "tool"})
    check("dict body accepted", isinstance(r, dict) and r.get("id"), str(r)[:120])
    r = handle_request("POST", "/prune", {}, json.dumps({"days": 0}).encode("utf-8"))
    check("bytes body accepted", r.get("status") == "ok", str(r)[:120])

    # 13c. Timeout classifier: bare-substring result text must not fire
    sigs = _detect_signals("read_file", "the build log says: request timed out after 30s", "s-h", "completed", 0)
    check("bare 'timed out' text -> no timeout signal", not any(s["signal_type"] == "timeout" for s in sigs), str(sigs))
    sigs = _detect_signals("terminal", "operation timed out", "s-h", "completed", 0)
    check("'operation timed out' -> timeout signal", any(s["signal_type"] == "timeout" for s in sigs))
    sigs = _detect_signals("terminal", "", "s-h", "completed", 0, error_type="timeout")
    check("structured timeout -> timeout signal", any(s["signal_type"] == "timeout" for s in sigs))

    # 13d. _resolve_agent_cmd: '-q' at end / followed by a flag must not crash
    _prev_cmd = os.environ.get("ABYSS_AGENT_CMD", "")
    try:
        os.environ["ABYSS_AGENT_CMD"] = json.dumps(["hermes", "chat", "-q"])
        argv = __init__._resolve_agent_cmd("probe prompt")
        check("-q last -> prompt appended", argv[-1] == "probe prompt", str(argv))
        os.environ["ABYSS_AGENT_CMD"] = json.dumps(["hermes", "chat", "-q", "-s", "skill"])
        argv = __init__._resolve_agent_cmd("probe prompt 2")
        check("-q before flag -> flag kept + prompt appended", "-s" in argv and argv[-1] == "probe prompt 2", str(argv))
    finally:
        if _prev_cmd:
            os.environ["ABYSS_AGENT_CMD"] = _prev_cmd
        else:
            os.environ.pop("ABYSS_AGENT_CMD", None)

    # 13e. Stale 'running' resolution (crashed backend) re-dispatches; fresh does not
    os.environ["ABYSS_STUB_STATUS"] = "succeeded"
    sig_stale = _record_self_diagnostic("stale-sess", "cap", "gap")
    conn = __init__._get_activity_conn()
    conn.execute(
    "UPDATE signals SET resolution_status = 'running', resolution_started_at = ? WHERE id = ?",
    (__init__.datetime.fromtimestamp(_time.time() - 7200).isoformat(), sig_stale),
    )
    conn.commit()
    conn.close()
    r = handle_request("POST", f"/signals/{sig_stale}/resolve-agent", {})
    check("stale running -> re-dispatched", r.get("status") == "dispatched", str(r))
    sig_fresh = _record_self_diagnostic("fresh-sess", "cap", "gap")
    conn = __init__._get_activity_conn()
    conn.execute(
    "UPDATE signals SET resolution_status = 'running', resolution_started_at = ? WHERE id = ?",
    (__init__.datetime.now().isoformat(), sig_fresh),
    )
    conn.commit()
    conn.close()
    r = handle_request("POST", f"/signals/{sig_fresh}/resolve-agent", {})
    check("fresh running -> already_running", r.get("status") == "already_running", str(r))

    print()
    print("=== 14. Trace graph-node system + timelines (Raindrop trajectories) ===")
    reset_db()
    gsid = "graph-sess-1"
    _add_trace(gsid, "llm_call", {"model": "deepseek", "phase": "end"})
    _add_trace(gsid, "tool_call", {"tool": "web_search", "tool_call_id": "t1", "phase": "start"})
    _add_trace(gsid, "tool_call", {"tool": "web_search", "tool_call_id": "t1", "phase": "end", "status": "ok", "duration_ms": 80})
    _add_trace(gsid, "tool_call", {"tool": "terminal", "tool_call_id": "t2", "phase": "start"})
    _add_trace(gsid, "tool_call", {"tool": "terminal", "tool_call_id": "t2", "phase": "end", "status": "error", "error_type": "RuntimeError", "error_message": "boom", "duration_ms": 120})
    _add_trace(gsid, "llm_call", {"model": "deepseek", "phase": "end"})
    _add_trace(gsid, "tool_call", {"tool": "read_file", "tool_call_id": "t3", "phase": "start"})
    _add_trace(gsid, "tool_call", {"tool": "read_file", "tool_call_id": "t3", "phase": "end", "status": "ok", "duration_ms": 40})
    # Session list is activity-based; give the fixture a presence there too.
    _add_activity("tool_call_completed", "graph fixture", "tool", "completed",
                  metadata={"tool": "read_file"}, session_id=gsid, tool_name="read_file")

    g = get_trace_graph(gsid, limit=100)
    check("graph has root session node", any(n["type"] == "session" for n in g["nodes"]))
    check("graph has 2 llm nodes", sum(1 for n in g["nodes"] if n["type"] == "llm") == 2)
    tools_g = [n for n in g["nodes"] if n["type"] == "tool"]
    check("graph merges start+end into 3 tool nodes", len(tools_g) == 3, str(len(tools_g)))
    check("graph marks error node", sum(1 for n in tools_g if n["status"] == "error") == 1)
    check("graph stats.errors == 1", g["stats"]["errors"] == 1, str(g["stats"]))
    check("graph edges attach tools to llm turn", any(e["source"].startswith("node_llm_") for e in g["edges"]))
    check("graph stats has full key set", all(k in g["stats"] for k in ("tools", "ok", "errors", "open", "llms")))

    tl = get_trace_timeline(gsid, limit=100)
    check("timeline has reasoning/tools/failures lanes", {l["id"] for l in tl["lanes"]} == {"reasoning", "tools", "failures"})
    tools_l = next(l for l in tl["lanes"] if l["id"] == "tools")
    check("timeline tools lane has 3", len(tools_l["nodes"]) == 3, str(len(tools_l["nodes"])))
    fail_l = next(l for l in tl["lanes"] if l["id"] == "failures")
    check("timeline failures lane == 1", len(fail_l["nodes"]) == 1, str(len(fail_l["nodes"])))
    check("timeline total_ms positive", tl["total_ms"] > 0)

    ov = get_agents_overview(limit=20)
    row = next((a for a in ov["agents"] if a["session_id"] == gsid), None)
    check("agents overview includes session", row is not None)
    check("agents overview error_count == 1", bool(row) and row["error_count"] == 1, str(row))
    check("agents overview has_errors True", bool(row) and row["has_errors"] is True)

    # Session list carries per-session health aggregates (picker badges).
    sess = get_recent_sessions(limit=10)
    srow = next((s for s in sess if s["session_id"] == gsid), None)
    check("recent sessions include health fields",
          bool(srow) and all(k in (srow or {}) for k in ("error_count", "has_errors", "llm_count", "trace_count")))
    check("recent sessions error_count == 1", bool(srow) and srow["error_count"] == 1, str(srow))
    check("recent sessions llm_count == 2", bool(srow) and srow["llm_count"] == 2, str(srow))


    print()
    print("=== 15. Performance latency percentiles (/performance) ===")
    reset_db()
    psid = "perf-sess-1"
    # Tool latency fixtures: three web_search calls (100/200/900 ms), one
    # failing terminal call (5000 ms) that must count as an error.
    for d in (100, 200, 900):
        _add_trace(psid, "tool_call", {"tool": "web_search", "tool_call_id": f"w{d}", "phase": "end", "status": "ok", "duration_ms": d}, duration_ms=d)
    _add_trace(psid, "tool_call", {"tool": "terminal", "tool_call_id": "tX", "phase": "end", "status": "error", "error_type": "RuntimeError", "duration_ms": 5000}, duration_ms=5000)
    # API request fixtures: two completed deepseek calls + one failed provider.
    _conn = __init__._get_activity_conn()
    _conn.execute("DELETE FROM api_requests")
    _conn.execute(
        "INSERT INTO api_requests (timestamp, session_id, model, provider, status, duration_ms, input_tokens, output_tokens) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (__init__.datetime.now().isoformat(), psid, "deepseek", "opencode-go", "completed", 1200, 100, 50),
    )
    _conn.execute(
        "INSERT INTO api_requests (timestamp, session_id, model, provider, status, duration_ms, input_tokens, output_tokens) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (__init__.datetime.now().isoformat(), psid, "deepseek", "opencode-go", "completed", 900, 80, 40),
    )
    _conn.execute(
        "INSERT INTO api_requests (timestamp, session_id, model, provider, status, duration_ms, input_tokens, output_tokens) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (__init__.datetime.now().isoformat(), psid, "claude", "anthropic", "error", 300, 0, 0),
    )
    _conn.commit()
    _conn.close()

    perf = get_performance(days=7, limit=20)
    check("performance has tools/models/totals", {"tools", "models", "totals"} <= set(perf.keys()))
    check("performance totals tool_calls == 4", perf["totals"]["tool_calls"] == 4, str(perf["totals"]))
    check("performance totals tool_errors == 1", perf["totals"]["tool_errors"] == 1, str(perf["totals"]))
    ws = next((t for t in perf["tools"] if t["tool"] == "web_search"), None)
    check("performance web_search p50 == 200", bool(ws) and ws["p50_ms"] == 200, str(ws))
    check("performance web_search p95 == 900", bool(ws) and ws["p95_ms"] == 900, str(ws))
    check("performance web_search max == 900", bool(ws) and ws["max_ms"] == 900, str(ws))
    check("performance web_search errors == 0", bool(ws) and ws["errors"] == 0, str(ws))
    term = next((t for t in perf["tools"] if t["tool"] == "terminal"), None)
    check("performance terminal errors == 1", bool(term) and term["errors"] == 1, str(term))
    check("performance tools sorted by p95 desc", all(
        perf["tools"][i]["p95_ms"] >= perf["tools"][i + 1]["p95_ms"]
        for i in range(len(perf["tools"]) - 1)
    ), str([t["p95_ms"] for t in perf["tools"]]))
    check("performance totals llm_requests == 3", perf["totals"]["llm_requests"] == 3, str(perf["totals"]))
    check("performance totals llm_errors == 1", perf["totals"]["llm_errors"] == 1, str(perf["totals"]))
    check("performance totals input_tokens == 180", perf["totals"]["input_tokens"] == 180, str(perf["totals"]))
    ds = next((m for m in perf["models"] if m["model"] == "deepseek"), None)
    check("performance deepseek p95 == 1200", bool(ds) and ds["p95_ms"] == 1200, str(ds))
    check("performance models include provider", bool(ds) and ds["provider"] == "opencode-go", str(ds))
    # REST route + slash command
    check("GET /performance route", "tools" in handle_request("GET", "/performance", {"days": 7, "limit": 20}))
    h = _handle_slash("performance")
    check("/abyss performance", "Abyss performance" in h and "Slowest tools" in h, h[:160])

    # Regression: totals must count the FULL window, not just the top-N
    # subset. With >20 distinct tools, the old code summed counts AFTER
    # ``tools[:limit]`` truncation, so totals undercounted (e.g. 30 calls
    # reported as 25). Seed 25 distinct tools (1 call each) + 5 extra on
    # tool_00 = 30 more calls on top of the 4 section fixtures = 34 total;
    # limit=20 must still report totals=34 while tools array stays at 20.
    for i in range(25):
        _add_trace(psid, "tool_call", {"tool": f"many_{i:02d}", "tool_call_id": f"m{i}", "phase": "end", "status": "ok", "duration_ms": 1000}, duration_ms=1000)
    for _ in range(5):
        _add_trace(psid, "tool_call", {"tool": "many_00", "tool_call_id": "m00x", "phase": "end", "status": "ok", "duration_ms": 1000}, duration_ms=1000)
    perf2 = get_performance(days=7, limit=20)
    check("performance totals full-window (34 calls, limit=20)",
          len(perf2["tools"]) == 20 and perf2["totals"]["tool_calls"] == 34,
          str(perf2["totals"]))

    print()
    print("=== 16. Bulk signal resolution (triage-safe cleanup) ===")
    reset_db()
    # Fixtures: 3 open signals in cron sessions + 1 open in a live session +
    # 1 already-resolved cron signal. The two cron session signals belong to
    # one incident that must auto-close when close_empty_incidents=True.
    conn = __init__._get_activity_conn()
    now = __init__.datetime.now().isoformat()
    old = (__init__.datetime.now() - __init__.timedelta(days=10)).isoformat()
    ids = []
    _ins = lambda st, sess, ts, inc=None: (  # noqa: E731
        conn.execute(
            "INSERT INTO signals (timestamp, signal_type, severity, label, description, session_id, source, acknowledged, resolved, incident_id) "
            "VALUES (?, ?, 'warning', 'x', 'x', ?, 'classifier', 0, 0, ?)",
            (ts, st, sess, inc),
        ).lastrowid
    )
    ids.append(_ins("tool_error", "cron_1", old))
    ids.append(_ins("timeout", "cron_1", old))
    ids.append(_ins("tool_error", "cron_2", old))
    live_id = _ins("rate_limit", "live-sess", old)
    resolved_id = _ins("tool_error", "cron_3", __init__.datetime.now().isoformat())
    conn.execute(
        "UPDATE signals SET resolved = 1, acknowledged = 1 WHERE id = ?", (resolved_id,)
    )
    # Incident claiming the two cron_1 signals
    inc_cur = conn.execute(
        "INSERT INTO incidents (timestamp, title, description, severity, signal_count, session_ids, pattern, status, created_at, signal_ids) "
        "VALUES (?, 'cluster', 'desc', 'warning', 3, 'cron_1', 'multi_signal', 'open', ?, '[]')",
        (now, now),
    )
    inc_id = inc_cur.lastrowid
    conn.execute("UPDATE signals SET incident_id = ? WHERE id IN (?, ?)", (inc_id, ids[0], ids[1]))
    conn.commit()
    conn.close()

    # 1) Filter by session_prefix — resolves cron_1+cron_2 signals only.
    r = __init__._resolve_signals_bulk(session_prefix="cron_")
    check("bulk resolve by session_prefix", r.get("resolved") == 3, str(r))
    check("bulk resolve live session untouched",
          __init__._get_activity_conn().execute("SELECT resolved FROM signals WHERE id = ?", (live_id,)).fetchone()["resolved"] == 0)
    check("bulk resolve skips already-resolved",
          r["signal_ids"] and resolved_id not in r["signal_ids"], str(r))

    # 2) close_empty_incidents: an incident whose last open signal is resolved
    #    by the batch auto-closes (dedicated open signal under cron_4).
    conn = __init__._get_activity_conn()
    b_sig = _ins("tool_error", "cron_4", old)
    inc_b = conn.execute(
        "INSERT INTO incidents (timestamp, title, description, severity, signal_count, session_ids, pattern, status, created_at, signal_ids) "
        "VALUES (?, 'cluster', 'desc', 'warning', 1, 'cron_4', 'multi_signal', 'open', ?, '[]')",
        (now, now),
    ).lastrowid
    conn.execute("UPDATE signals SET incident_id = ? WHERE id = ?", (inc_b, b_sig))
    conn.commit()
    conn.close()
    r2 = __init__._resolve_signals_bulk(session_prefix="cron_4", close_empty_incidents=True)
    check("bulk resolve counts signal", r2.get("resolved") == 1, str(r2))
    _aconn = __init__._get_activity_conn()
    inc_state = _aconn.execute("SELECT status FROM incidents WHERE id = ?", (inc_b,)).fetchone()["status"]
    _aconn.close()
    check("bulk resolve close_empty_incidents", inc_state == "resolved", str(inc_state))

    # 3) Missing filter -> 400 error (operator footgun guard).
    r3 = __init__._resolve_signals_bulk()
    check("bulk resolve requires filter", r3.get("code") == 400 and "filter" in r3.get("error", ""), str(r3))

    # 4) resolve everything under cron_ via REST route (session_prefix + close).
    rr = handle_request("POST", "/signals/resolve-bulk", body=json.dumps({"session_prefix": "cron_", "close_empty_incidents": True}))
    check("POST /signals/resolve-bulk route", rr.get("status") is None and "resolved" in rr and rr["resolved"] >= 0, str(rr))
    rr400 = handle_request("POST", "/signals/resolve-bulk", body=json.dumps({}))
    check("POST /signals/resolve-bulk rejects empty body", rr400.get("code") == 400, str(rr400))

    # 5) Slash command: /abyss resolve-stale
    sh = _handle_slash("resolve-stale 1 cron_")
    check("/abyss resolve-stale", "Bulk-resolved" in sh, sh[:160])
    sh_helper = _handle_slash("help")
    check("help lists resolve-stale", "resolve-stale" in sh_helper, sh_helper[:200])

    print("=== 17. Status liveness metadata (last-activity / last-signal / last-error) ===")
    reset_db()
    st_empty = get_status()
    check("status liveness None on empty DB",
          st_empty.get("last_activity_at") is None
          and st_empty.get("last_signal_at") is None
          and st_empty.get("last_error_at") is None,
          str({k: st_empty.get(k) for k in ("last_activity_at", "last_signal_at", "last_error_at")}))
    r_ok = _add_activity("tool_call_completed", "ok call", "tool", "completed",
                         metadata={"tool": "web_search"}, session_id="sess-live", tool_name="web_search")
    r_err = _add_activity("tool_call_failed", "bad call", "tool", "error",
                          metadata={"tool": "terminal", "error_message": "boom"},
                          session_id="sess-live", tool_name="terminal")
    _record_self_diagnostic("sess-live", "terminal", "gap detected", "warning")
    st = get_status()
    ts_act = r_err["timestamp"]
    check("status last_activity_at == newest activity", st.get("last_activity_at") == ts_act, str(st.get("last_activity_at")))
    check("status last_error_at == error activity", st.get("last_error_at") == ts_act, str(st.get("last_error_at")))
    check("status last_signal_at set", bool(st.get("last_signal_at")), str(st.get("last_signal_at")))
    check("status last_signal_at after last activity", st.get("last_signal_at") >= ts_act, f"{st.get('last_signal_at')} >= {ts_act}")
    st_rest = handle_request("GET", "/status")
    check("status liveness keys present via REST",
          all(k in st_rest for k in ("last_activity_at", "last_signal_at", "last_error_at")),
          str([k for k in st_rest.keys() if k.startswith("last_")]))
    check("status REST liveness matches direct call",
          st_rest.get("last_activity_at") == st.get("last_activity_at")
          and st_rest.get("last_signal_at") == st.get("last_signal_at")
          and st_rest.get("last_error_at") == st.get("last_error_at"), "")

    print("=== 18. Signal list tool context (activity JOIN for /signals) ===")
    reset_db()
    # classifier-produced signal linked to a failing activity row
    r_act = _add_activity("tool_call_failed", "terminal boom", "tool", "error",
                          metadata={"error_message": "exit 1"},
                          session_id="sess-tc", tool_name="terminal")
    sigs = __init__._detect_and_record_signals(
        "terminal", "", "sess-tc", "error", r_act["id"], error_message="exit 1")
    check("signal recorded from failing call", len(sigs) >= 1, str(sigs))
    rows = handle_request("GET", "/signals")
    check("/signals rows carry tool_name from activity JOIN",
          any(r.get("tool_name") == "terminal" for r in rows),
          str(rows[0] if rows else None))
    check("/signals rows carry tool_action from activity JOIN",
          any(r.get("tool_action") == "tool_call_failed" for r in rows),
          str(rows[0] if rows else None))
    # self-diagnostic (activity_id NULL) must survive the LEFT JOIN intact
    _record_self_diagnostic("sess-tc", "web_search", "rate limited", "warning")
    rows = handle_request("GET", "/signals")
    sd = next((r for r in rows if r.get("signal_type") == "self_diagnostic"), None)
    check("self-diagnostic retained with NULL tool context",
          bool(sd) and sd.get("tool_name") is None and sd.get("tool_action") is None, str(sd))
    check("/signals session filter still works with JOIN",
          len(handle_request("GET", "/signals", {"session_id": "sess-tc"})) == len(rows),
          str(len(rows)))
    # REST parity: FastAPI layer delegates to the same handler
    st_rest = handle_request("GET", "/signals", {"limit": 10})
    check("/signals REST contract enriched keys",
          all({"tool_name", "tool_action"} <= set(r.keys()) for r in st_rest), "")

    print()
    print(f"=== RESULT: {PASS} passed, {FAIL} failed ===")

    if FAIL:
        sys.exit(1)
    print("All Abyss backend tests passed!")


if __name__ == "__main__":
    _run_script()


def test_runner():
    global PASS, FAIL
    PASS = 0
    FAIL = 0
    import sys as _sys
    _run_script()
    if FAIL:
        raise AssertionError(f"test_runner: FAIL={FAIL}")