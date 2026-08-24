"""Test the Abyss FastAPI backend layer (dashboard/plugin_api.py).

Mounted the same way Hermes mounts it: at /api/plugins/abyss via the
exported ``router``. Uses an isolated temp data dir and an in-process
TestClient, so the live profile DB is never touched.
"""
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="abyss-api-test-")
os.environ["HERMES_PROFILE_HOME"] = _TMP
os.environ["ABYSS_RETENTION_DAYS"] = "365"

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import plugin_api  # noqa: E402

app = FastAPI()
app.include_router(plugin_api.router, prefix="/api/plugins/abyss")
client = TestClient(app)

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



def _run_script():
    global PASS, FAIL
    print("=== FastAPI endpoint tests ===")

    # POST /activity — the path that used to silently drop bodies
    r = client.post("/api/plugins/abyss/activity", json={
    "action": "tool_call_completed",
    "description": "Called web_search via REST",
    "category": "tool",
    "status": "completed",
    "session_id": "api-sess",
    "tool_name": "web_search",
    "args": {"query": "raindrop"},
    })
    check("POST /activity accepts body", r.status_code == 200 and r.json().get("id") is not None, str(r.json()))

    # GET /activity
    r = client.get("/api/plugins/abyss/activity", params={"limit": 5})
    check("GET /activity", r.status_code == 200 and len(r.json()) == 1)

    # GET /activity with session_id filter (drill one session's activity story)
    r = client.get("/api/plugins/abyss/activity", params={"session_id": "api-sess"})
    check("GET /activity session filter", r.status_code == 200 and len(r.json()) == 1)
    r = client.get("/api/plugins/abyss/activity", params={"session_id": "no-such-session"})
    check("GET /activity session filter empty", r.status_code == 200 and len(r.json()) == 0)

    # POST /activity with empty body must not crash
    r = client.post("/api/plugins/abyss/activity", json={})
    check("POST /activity empty body tolerated", r.status_code == 200, str(r.json()))

    # GET /stats
    r = client.get("/api/plugins/abyss/stats")
    data = r.json()
    check("GET /stats", r.status_code == 200 and data.get("total_activities", 0) >= 1)
    check("GET /stats health fields", "error_rate" in data and "signals_open" in data and "top_tools" in data)

    # GET /search
    r = client.get("/api/plugins/abyss/search", params={"q": "web_search"})
    check("GET /search", r.status_code == 200 and len(r.json()) >= 1)

    # GET /calendar
    r = client.get("/api/plugins/abyss/calendar")
    cal_data = r.json()
    check("GET /calendar", r.status_code == 200 and isinstance(cal_data, list))
    cal_rest = next((c for c in cal_data if c.get("session_id")), None)
    check("GET /calendar rows carry session_id (trace drill)",
          bool(cal_rest) and cal_rest["session_id"] == "api-sess", str(cal_rest))
    check("GET /calendar rows carry tool_name",
          bool(cal_rest) and cal_rest.get("tool_name") == "web_search", str(cal_rest))

    # GET /trace without session -> recent sessions
    r = client.get("/api/plugins/abyss/trace", params={"limit": 5})
    check("GET /trace (sessions)", r.status_code == 200 and isinstance(r.json(), list))

    # GET /trace with session
    r = client.get("/api/plugins/abyss/trace", params={"session_id": "api-sess"})
    check("GET /trace (session)", r.status_code == 200 and isinstance(r.json(), list))

    # GET /graph
    r = client.get("/api/plugins/abyss/graph", params={"limit": 50})
    g = r.json()
    check("GET /graph", r.status_code == 200 and "nodes" in g and "edges" in g)

    # POST self-diagnostic
    r = client.post("/api/plugins/abyss/signals/self-diagnostic", json={
    "session_id": "api-sess", "capability": "web_search", "gap": "429 rate limit",
    })
    check("POST self-diagnostic", r.status_code == 200 and r.json().get("status") == "recorded")

    # GET /signals
    r = client.get("/api/plugins/abyss/signals")
    sigs = r.json()
    check("GET /signals", r.status_code == 200 and len(sigs) >= 1)
    check("signals have details column", any("details" in s for s in sigs))

    # POST /signals/resolve-bulk (triage-safe cleanup)
    r = client.post("/api/plugins/abyss/signals/resolve-bulk", json={"signal_type": "rate_limit"})
    check("POST /signals/resolve-bulk", r.status_code == 200 and "resolved" in r.json() and "signal_ids" in r.json(), str(r.json()))
    r = client.post("/api/plugins/abyss/signals/resolve-bulk", json={})
    check("POST /signals/resolve-bulk requires filter", r.json().get("code") == 400, str(r.json()))
    # GET /signals

    # POST /incidents/cluster
    r = client.post("/api/plugins/abyss/incidents/cluster")
    check("POST /incidents/cluster", r.status_code == 200 and "incidents_created" in r.json())

    # GET /incidents
    r = client.get("/api/plugins/abyss/incidents")
    check("GET /incidents", r.status_code == 200 and isinstance(r.json(), list))

    # Triage endpoints (use real ids from the DB)
    sig_id = sigs[0]["id"]
    r = client.post(f"/api/plugins/abyss/signals/{sig_id}/acknowledge")
    check("POST signal acknowledge", r.status_code == 200 and r.json().get("status") == "acknowledged")
    r = client.post(f"/api/plugins/abyss/signals/{sig_id}/resolve")
    check("POST signal resolve", r.status_code == 200 and r.json().get("status") == "resolved")
    r = client.post("/api/plugins/abyss/signals/999999/acknowledge")
    check("POST signal ack 404", r.status_code == 200 and r.json().get("code") == 404)

    incs = client.get("/api/plugins/abyss/incidents").json()
    if incs:
        inc_id = incs[0]["id"]
        r = client.post(f"/api/plugins/abyss/incidents/{inc_id}/acknowledge")
        check("POST incident acknowledge", r.status_code == 200 and r.json().get("status") == "acknowledged")
        r = client.post(f"/api/plugins/abyss/incidents/{inc_id}/resolve")
        check("POST incident resolve", r.status_code == 200 and r.json().get("status") == "resolved")
        r = client.post(f"/api/plugins/abyss/incidents/{inc_id}/reopen")
        check("POST incident reopen", r.status_code == 200 and r.json().get("status") == "open")
    else:
        print("  [SKIP] incident triage (no incidents seeded)")

    # --- Triage filters over REST ---------------------------------------------
    # The Aug-2026 triage filters (type/severity/state on /signals,
    # severity/open on /incidents) shipped core-side with zero HTTP-contract
    # coverage: only test_plugin.py exercised them through handle_request().
    # These checks prove the FastAPI layer forwards every parameter end-to-end:
    # query string -> plugin_api._delegate -> handle_request -> SQL clauses.
    print("=== Triage filter contract (REST) ===")
    _AB = "/api/plugins/abyss"

    for _sess, _sev in (("api-triage-warn", "warning"), ("api-triage-warn", "warning"),
                        ("api-triage-err", "error"), ("api-triage-err", "error")):
        r = client.post(f"{_AB}/signals/self-diagnostic", json={
            "session_id": _sess, "capability": "triage_probe",
            "gap": f"{_sev} probe", "severity": _sev,
        })
        check(f"seed self-diagnostic {_sess}/{_sev}",
              r.status_code == 200 and r.json().get("status") == "recorded", str(r.json()))

    def _mine(rows, sess):
        return [row for row in rows if row.get("session_id") == sess]

    r = client.get(f"{_AB}/signals", params={"state": "open"})
    _warn = _mine(r.json(), "api-triage-warn")
    _err = _mine(r.json(), "api-triage-err")
    check("GET /signals state=open lists seeds", r.status_code == 200 and len(_warn) == 2 and len(_err) == 2,
          f"warn={len(_warn)} err={len(_err)}")
    _warn_ids = {s["id"] for s in _warn}
    _err_ids = {s["id"] for s in _err}

    r = client.get(f"{_AB}/signals", params={"type": "self_diagnostic", "state": "open"})
    _typed = _mine(r.json(), "api-triage-warn") + _mine(r.json(), "api-triage-err")
    check("GET /signals type filter",
          len(_typed) == 4 and all(s.get("signal_type") == "self_diagnostic" for s in _typed))

    r = client.get(f"{_AB}/signals", params={"severity": "error", "state": "open"})
    _got = {s["id"] for s in r.json()}
    check("GET /signals severity=error&state=open exact", _got == _err_ids,
          f"{sorted(_got)} vs {sorted(_err_ids)}")

    r = client.get(f"{_AB}/signals", params={"severity": "warning", "state": "unack"})
    _got = {s["id"] for s in r.json()}
    check("GET /signals severity=warning&state=unack exact", _got == _warn_ids)

    r = client.get(f"{_AB}/signals", params={
        "type": "self_diagnostic", "severity": "error", "state": "open"})
    _got = {s["id"] for s in r.json()}
    check("GET /signals type+severity+state combined", _got == _err_ids)

    # Unknown state -> clean 400 envelope. HTTP stays 200 by design: the
    # dispatcher never raises, callers read the JSON error/code envelope.
    r = client.get(f"{_AB}/signals", params={"state": "bogus"})
    check("GET /signals invalid state -> 400 envelope",
          r.status_code == 200 and r.json().get("code") == 400
          and "state" in r.json().get("error", ""), str(r.json()))

    # Cluster the four probes: one warning incident (warn pair) + one error
    # incident (err pair); each inherits its members' max severity.
    r = client.post(f"{_AB}/incidents/cluster")
    check("POST /incidents/cluster (probes)",
          r.status_code == 200 and "incidents_created" in r.json(), str(r.json()))

    r = client.get(f"{_AB}/incidents", params={"open": True})
    _inc = {i.get("session_ids"): i for i in r.json()
            if i.get("session_ids") in ("api-triage-warn", "api-triage-err")}
    check("GET /incidents open=1 lists probe incidents",
          set(_inc) == {"api-triage-warn", "api-triage-err"}, str(list(_inc)))
    _warn_inc = _inc.get("api-triage-warn") or {}
    _err_inc = _inc.get("api-triage-err") or {}
    check("probe incidents inherit max member severity",
          _warn_inc.get("severity") == "warning" and _err_inc.get("severity") == "error",
          f"warn={_warn_inc.get('severity')} err={_err_inc.get('severity')}")

    r = client.get(f"{_AB}/incidents", params={"severity": "error", "open": True})
    _got = {i["id"] for i in r.json()}
    check("GET /incidents severity=error&open=1 exact",
          bool(_err_inc) and _err_inc["id"] in _got
          and bool(_warn_inc) and _warn_inc["id"] not in _got)

    r = client.get(f"{_AB}/incidents", params={"status": "resolved", "open": True})
    check("GET /incidents open=1 + conflicting status -> 400 envelope",
          r.status_code == 200 and r.json().get("code") == 400, str(r.json()))

    # Acknowledging a signal drops it from state=unack but keeps it in state=open
    _acked = sorted(_warn_ids)[0]
    r = client.post(f"{_AB}/signals/{_acked}/acknowledge")
    check("POST acknowledge probe signal",
          r.status_code == 200 and r.json().get("status") == "acknowledged")
    r = client.get(f"{_AB}/signals", params={"severity": "warning", "state": "unack"})
    check("acked signal leaves state=unack",
          {s["id"] for s in r.json()} == _warn_ids - {_acked})
    r = client.get(f"{_AB}/signals", params={"severity": "warning", "state": "open"})
    check("acked signal stays in state=open",
          {s["id"] for s in r.json()} >= _warn_ids)

    # POST /prune
    r = client.post("/api/plugins/abyss/prune", json={"days": 365})
    check("POST /prune", r.status_code == 200 and r.json().get("status") == "ok" and "deleted" in r.json())

    # New analytics endpoints
    r = client.get("/api/plugins/abyss/health")
    d = r.json()
    check("GET /health", r.status_code == 200 and 0 <= d.get("score", -1) <= 100 and "components" in d)
    r = client.get("/api/plugins/abyss/trends", params={"days": 1, "bucket": "hour"})
    check("GET /trends", r.status_code == 200 and "timestamps" in r.json() and "activity" in r.json())
    r = client.get("/api/plugins/abyss/trends", params={"bucket": "day"})
    check("GET /trends default days", r.status_code == 200 and r.json().get("days") == 7)
    r = client.get("/api/plugins/abyss/failures", params={"limit": 5})
    check("GET /failures", r.status_code == 200 and {"by_type", "by_tool", "by_message"} <= set(r.json().keys()))
    r = client.get("/api/plugins/abyss/performance")
    p = r.json()
    check("GET /performance", r.status_code == 200 and {"totals", "tools", "models"} <= set(p.keys()))
    check("GET /performance totals shape", "tool_calls" in p.get("totals", {}) and "llm_requests" in p.get("totals", {}))
    r = client.get("/api/plugins/abyss/export")
    e = r.json()
    check("GET /export", r.status_code == 200 and {"activity", "signals", "incidents", "traces"} <= set(e.keys()))
    r = client.get("/api/plugins/abyss/status")
    s = r.json()
    check("GET /status", r.status_code == 200 and {"score", "level", "signals_open", "incidents_open"} <= set(s.keys()))

    # Unknown endpoint -> FastAPI-level 404 (router has no such route)
    r = client.get("/api/plugins/abyss/nope")
    check("unknown endpoint 404", r.status_code == 404, f"status {r.status_code}")

    print("=== Agent resolution + doctor endpoints (stub agent) ===")
    import json as _json
    import time as _time

    _stub = Path(_TMP) / "stub_agent_api.py"
    _stub.write_text('''import json, os
rp = os.environ.get("ABYSS_REPORT_PATH", "")
role = os.environ.get("ABYSS_AGENT_ROLE", "resolver")
report = {
    "schema": "abyss-resolution/1",
    "role": role,
    "report_id": os.path.basename(rp).replace(".json", "") if rp else "",
    "status": os.environ.get("ABYSS_STUB_STATUS", "succeeded"),
    "summary": os.environ.get("ABYSS_STUB_SUMMARY", "stub fixed it"),
    "findings": [],
    "actions_taken": [],
    "proposed_fixes": json.loads(os.environ.get("ABYSS_STUB_PROPOSED", "[]")),
    "fixes": json.loads(os.environ.get("ABYSS_STUB_FIXES", "[]")),
    "skills_saved": [],
    "error": None,
}
if rp:
    with open(rp, "w", encoding="utf-8") as f:
        json.dump(report, f)
print(report["summary"])
''', encoding="utf-8")
    os.environ["ABYSS_AGENT_CMD"] = _json.dumps([sys.executable, str(_stub)])
    os.environ["ABYSS_AGENT_TIMEOUT"] = "30"
    os.environ["ABYSS_STUB_STATUS"] = "succeeded"
    os.environ["ABYSS_STUB_SUMMARY"] = "stub fixed the issue"
    os.environ["ABYSS_STUB_PROPOSED"] = "[]"
    os.environ["ABYSS_STUB_FIXES"] = "[]"


    def _wait_for_api(pred, timeout=15):
        deadline = _time.time() + timeout
        while _time.time() < deadline:
            if pred():
                return True
            _time.sleep(0.2)
        return False


    # Fresh signal for the resolver agent
    r = client.post("/api/plugins/abyss/signals/self-diagnostic", json={
    "session_id": "api-res", "capability": "web_search", "gap": "429 rate limit",
    })
    sigs = client.get("/api/plugins/abyss/signals").json()
    sig_id = sigs[0]["id"]
    r = client.post(f"/api/plugins/abyss/signals/{sig_id}/resolve-agent")
    check("POST signal resolve-agent", r.status_code == 200 and r.json().get("status") == "dispatched", str(r.json()))
    ok = _wait_for_api(lambda: client.get("/api/plugins/abyss/signals").json()[0].get("resolution_status") == "succeeded")
    check("signal resolved by agent via REST", ok)
    row = client.get("/api/plugins/abyss/signals").json()[0]
    check("REST row carries note + resolved", row.get("resolved") == 1 and row.get("resolution_note") == "stub fixed the issue")

    # Fresh signal for the doctor flow
    r = client.post("/api/plugins/abyss/signals/self-diagnostic", json={
    "session_id": "api-doc", "capability": "web_search", "gap": "timeout",
    })
    sigs = client.get("/api/plugins/abyss/signals").json()
    doc_sig_id = sigs[0]["id"]
    os.environ["ABYSS_STUB_SUMMARY"] = "diagnosis complete"
    os.environ["ABYSS_STUB_PROPOSED"] = _json.dumps([
    {"id": "fix-1", "title": "fix timeout", "action": "raise timeout", "target_signals": [doc_sig_id], "target_incidents": []}
    ])
    r = client.post("/api/plugins/abyss/doctor/run")
    check("POST /doctor/run", r.status_code == 200 and r.json().get("status") == "dispatched", str(r.json()))
    rid = r.json()["report_id"]
    ok = _wait_for_api(lambda: client.get("/api/plugins/abyss/doctor/report", params={"report_id": rid}).json().get("status") == "ready")
    check("GET /doctor/report ready", ok)
    rep = client.get("/api/plugins/abyss/doctor/report", params={"report_id": rid}).json()
    check("doctor report proposes fix for target", rep["report"]["proposed_fixes"][0]["target_signals"] == [doc_sig_id], str(rep)[:200])
    os.environ["ABYSS_STUB_FIXES"] = _json.dumps([
    {"id": "fix-1", "status": "applied", "note": "raised timeout", "target_signals": [doc_sig_id], "target_incidents": []}
    ])
    os.environ["ABYSS_STUB_SUMMARY"] = "applied the fix"
    r = client.post("/api/plugins/abyss/doctor/approve", json={"report_id": rid})
    check("POST /doctor/approve", r.status_code == 200 and r.json().get("status") == "dispatched", str(r.json()))
    ok = _wait_for_api(lambda: client.get("/api/plugins/abyss/signals").json()[0].get("resolution_status") == "succeeded")
    check("approved fix applied via REST", ok)

    print()
    print(f"=== RESULT: {PASS} passed, {FAIL} failed ===")
    if FAIL:
        sys.exit(1)
    print("All Abyss API tests passed!")


def test_runner():
    global PASS, FAIL
    PASS = 0
    FAIL = 0
    import sys as _sys
    try:
        _run_script()
    except SystemExit as e:
        if e.code != 0:
            raise AssertionError(f"test_runner: non-zero exit {e.code}") from e
    if FAIL:
        raise AssertionError(f"test_runner: FAIL={FAIL}")


if __name__ == "__main__":
    _run_script()
    if FAIL:
        raise AssertionError(f"test_runner: FAIL={FAIL}")
