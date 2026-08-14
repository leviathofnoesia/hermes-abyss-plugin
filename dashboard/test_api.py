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
check("GET /calendar", r.status_code == 200 and isinstance(r.json(), list))

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
