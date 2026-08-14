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
)

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
check("type burst cluster created", any(inc_rows2["pattern"] == "timeout_burst"
                                        for inc_rows2 in handle_request("GET", "/incidents", {})))

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

print("=== 9. Slash commands ===")
h = _handle_slash("stats")
check("/abyss stats", "Total entries" in h and "Abyss Stats" in h)
h = _handle_slash("recent 5")
check("/abyss recent", "activity entr" in h)
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

print("=== 11. Agent-powered resolution + doctor (stub agent) ===")
reset_db()
import subprocess as _sp
import time as _time

# Stub agent: a tiny script that mimics the dispatched `hermes chat -q` agent.
# It writes the report JSON to ABYSS_REPORT_PATH (like the real agent would)
# and exits, so the backend worker finalizes exactly as in production.
_stub = Path(_TMP) / "stub_agent.py"
_stub.write_text('''
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
''', encoding="utf-8")
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
print(f"=== RESULT: {PASS} passed, {FAIL} failed ===")
if FAIL:
    sys.exit(1)
print("All Abyss backend tests passed!")
