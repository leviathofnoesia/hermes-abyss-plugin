"""Tests for the Abyss Wave module — the August 2026 plugin-interface expansion.

Runs against an isolated temp data dir (HERMES_PROFILE_HOME) so the live
profile's activity.db / traces.db are never touched. Exercises:

  - wave table creation (plugin_events, streams, api_requests, subagents,
    approvals, commands, platform_events, skills)
  - streaming hooks (on_stream_start/delta/end) + empty-stream signal
  - API request telemetry (pre/post/error lifecycle)
  - subagent lifecycle (start/stop + failure signal)
  - approval audit (pre/post + deny signal)
  - pre_command / gateway_platform_event / on_skill_lifecycle observers
  - event bus (#64164): durable plugin_events rows + ctx.emit() subscriber
    count, bare-name namespace enforcement
  - session reset/finalize observers
  - redaction masking (#65449) on stored previews
  - /wave/* REST endpoints through handle_request
  - ownership-ledger cleanup (on_unload clears ctx/stream state)
"""
import os
import sys
import json
import tempfile
from pathlib import Path

# Point the plugin at an isolated data dir BEFORE importing it.
_TMP = tempfile.mkdtemp(prefix="abyss-wave-test-")
os.environ["HERMES_PROFILE_HOME"] = _TMP
os.environ["ABYSS_RETENTION_DAYS"] = "365"  # never auto-prune test data

sys.path.insert(0, str(Path(__file__).resolve().parent))

import __init__  # noqa: E402
from __init__ import _init_db, handle_request  # noqa: E402

import abyss_wave as wave  # noqa: E402
from abyss_wave import (  # noqa: E402
    _mask_secrets,
    _on_api_request_error,
    _on_gateway_platform_event,
    _on_post_api_request,
    _on_post_approval_response,
    _on_pre_api_request,
    _on_pre_approval_request,
    _on_pre_command,
    _on_session_finalize,
    _on_session_reset,
    _on_skill_lifecycle,
    _on_stream_delta,
    _on_stream_end,
    _on_stream_start,
    _on_subagent_start,
    _on_subagent_stop,
    emit_abyss_event,
    wave_handle,
    wave_summary,
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


def conn_rows(sql, args=()):
    c = __init__._get_activity_conn()
    try:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
    finally:
        c.close()


def reset_wave():
    c = __init__._get_activity_conn()
    for t in wave._WAVE_TABLES + ("signals", "activity"):
        try:
            c.execute(f"DELETE FROM {t}")
        except Exception:
            pass
    c.commit()
    c.close()


class FakeCtx:
    """Minimal stand-in for the plugin context: only the emit surface."""

    def __init__(self):
        self.emitted = []

    def emit(self, event, payload=None):
        self.emitted.append((event, payload))
        return 1  # pretend one subscriber


print("=== Wave tables ===")
_init_db()
tables = {r["name"] for r in conn_rows(
    "SELECT name FROM sqlite_master WHERE type='table'")}
check("plugin_events table", "plugin_events" in tables)
check("streams table", "streams" in tables)
check("api_requests table", "api_requests" in tables)
check("subagents table", "subagents" in tables)
check("approvals table", "approvals" in tables)
check("commands table", "commands" in tables)
check("platform_events table", "platform_events" in tables)
check("skills table", "skills" in tables)

print("=== Streaming hooks (on_stream_start/delta/end) ===")
reset_wave()
_on_stream_start(turn_id="t1", session_id="s1", model="m1", provider="p1", surface="cli", iteration=2)
_on_stream_delta(turn_id="t1", session_id="s1", delta="Hello ", kind="text")
_on_stream_delta(turn_id="t1", session_id="s1", delta="world", kind="text")
_on_stream_delta(turn_id="t1", session_id="s1", delta="thinking", kind="reasoning")
_on_stream_end(turn_id="t1", session_id="s1", final_text="Hello world", finished=True)
rows = conn_rows("SELECT * FROM streams WHERE turn_id='t1'")
check("stream row persisted", len(rows) == 1, json.dumps(rows[0])[:160] if rows else "none")
if rows:
    r = rows[0]
    check("stream chars accumulated", r["chars"] == 19, f"chars={r['chars']} (6+5+8 raw deltas incl. reasoning)")
    check("stream deltas counted", r["deltas"] == 3, f"deltas={r['deltas']}")
    check("stream kinds tracked", "text" in json.loads(r["kind_counts"]) and "reasoning" in json.loads(r["kind_counts"]))
    check("stream first_token_ms set", r["first_token_ms"] is not None)
    check("stream duration_ms set", r["duration_ms"] >= 0)
    check("stream finished=1", r["finished"] == 1)

print("=== Empty-stream signal (silent failure) ===")
_on_stream_start(turn_id="t2", session_id="s2", model="m1", provider="p1", surface="cli")
_on_stream_end(turn_id="t2", session_id="s2", final_text="", finished=True)
sig_rows = conn_rows("SELECT * FROM signals WHERE signal_type='empty_stream'")
check("empty_stream signal recorded", len(sig_rows) == 1, json.dumps(sig_rows[0])[:160] if sig_rows else "none")
if sig_rows:
    check("empty_stream source=wave", sig_rows[0]["source"] == "wave")

print("=== API request telemetry ===")
reset_wave()
_on_pre_api_request(
    api_request_id="req-1", session_id="s1", turn_id="t1", model="m1",
    provider="p1", api_mode="chat", api_call_count=1, approx_input_tokens=100,
)
rows = conn_rows("SELECT * FROM api_requests WHERE api_request_id='req-1'")
check("api request row created running", len(rows) == 1 and rows[0]["status"] == "running")
_on_post_api_request(
    api_request_id="req-1", session_id="s1", model="m1", provider="p1",
    api_duration=1.5, finish_reason="stop", usage={"input_tokens": 120, "output_tokens": 40},
)
rows = conn_rows("SELECT * FROM api_requests WHERE api_request_id='req-1'")
check("api request closed completed", rows and rows[0]["status"] == "completed")
check("api request duration recorded", rows and rows[0]["duration_ms"] == 1500)
check("api request tokens recorded", rows and rows[0]["input_tokens"] == 120 and rows[0]["output_tokens"] == 40)
_on_pre_api_request(
    api_request_id="req-2", session_id="s1", turn_id="t2", model="m1",
    provider="p1", api_mode="chat", api_call_count=2,
)
_on_api_request_error(
    api_request_id="req-2", session_id="s1", provider="p1", model="m1",
    status_code=429, retryable=True, error_type="rate_limit", error_message="too many",
)
rows = conn_rows("SELECT * FROM api_requests WHERE api_request_id='req-2'")
check("api error row status error", rows and rows[0]["status"] == "error")
sig_rows = conn_rows("SELECT * FROM signals WHERE signal_type='api_error'")
check("api_error signal recorded", len(sig_rows) == 1)
check("api_error retryable -> warning", sig_rows and sig_rows[0]["severity"] == "warning")

print("=== Subagent lifecycle ===")
reset_wave()
_on_subagent_start(
    parent_session_id="parent-1", child_session_id="child-1", child_role="leaf",
    child_goal="investigate",
)
rows = conn_rows("SELECT * FROM subagents WHERE child_session_id='child-1'")
check("subagent row created running", len(rows) == 1 and rows[0]["status"] == "running")
_on_subagent_stop(
    child_session_id="child-1", child_role="leaf", child_status="completed",
    duration_ms=5000, child_summary="all done",
)
rows = conn_rows("SELECT * FROM subagents WHERE child_session_id='child-1'")
check("subagent closed completed", rows and rows[0]["status"] == "completed")
check("subagent duration recorded", rows and rows[0]["duration_ms"] == 5000)
_on_subagent_start(parent_session_id="parent-2", child_session_id="child-2", child_role="leaf")
_on_subagent_stop(child_session_id="child-2", child_role="leaf", child_status="failed", duration_ms=100)
sig_rows = conn_rows("SELECT * FROM signals WHERE signal_type='subagent_failure'")
check("subagent failure signal recorded", len(sig_rows) == 1)

print("=== Approval audit ===")
reset_wave()
_on_pre_approval_request(command="rm -rf /tmp/x", pattern_key="dangerous_rm", session_key="sk-1", surface="cli")
_on_post_approval_response(choice="deny", decided_by="user", session_key="sk-1", surface="cli")
rows = conn_rows("SELECT * FROM approvals WHERE session_key='sk-1'")
check("approval row closed with choice", rows and rows[0]["choice"] == "deny")
check("approval command masked/truncated", rows and len(rows[0]["command_preview"] or "") <= 300)
sig_rows = conn_rows("SELECT * FROM signals WHERE signal_type='approval_denied'")
check("approval deny signal recorded", len(sig_rows) == 1)

print("=== pre_command / platform events / skill lifecycle ===")
reset_wave()
_on_pre_command(surface="cli", command="abyss", alias_used="/abyss", args_raw="stats", session_key="sk-1")
rows = conn_rows("SELECT * FROM commands")
check("command row recorded", len(rows) == 1 and rows[0]["command"] == "abyss")
_on_gateway_platform_event(platform="telegram", event_type="message_edited", payload={"mid": 1})
rows = conn_rows("SELECT * FROM platform_events")
check("platform event recorded", len(rows) == 1 and rows[0]["event_type"] == "message_edited")
_on_skill_lifecycle(action="created", skill_name="test-skill", provenance="hub", use_count=1, reused=False)
rows = conn_rows("SELECT * FROM skills")
check("skill lifecycle recorded", len(rows) == 1 and rows[0]["action"] == "created")

print("=== Session reset / finalize observers ===")
reset_wave()
_on_session_reset(session_id="new-1", old_session_id="old-1", reason="new_session", platform="cli")
rows = conn_rows("SELECT * FROM activity WHERE action='session_reset'")
check("session_reset activity recorded", len(rows) == 1)
_on_session_finalize(session_id="old-1", reason="new_session", platform="cli")
rows = conn_rows("SELECT * FROM activity WHERE action='session_finalized'")
check("session_finalize activity recorded", len(rows) == 1)

print("=== Event bus (#64164) ===")
reset_wave()
wave._CTX = None
n = emit_abyss_event("signal_detected", {"signal_id": 7})
check("emit without ctx returns 0 subscribers", n == 0)
rows = conn_rows("SELECT * FROM plugin_events WHERE event='signal_detected'")
check("durable event row written without ctx", len(rows) == 1)
check("event payload stored", rows and json.loads(rows[0]["payload"]).get("signal_id") == 7)

fake = FakeCtx()
wave._CTX = fake
n = emit_abyss_event("incident_clustered", {"incident_id": 1})
check("emit with ctx returns subscriber count", n == 1)
check("ctx.emit called with bare name", fake.emitted and fake.emitted[0][0] == "incident_clustered")

# Namespace enforcement: emitting a namespaced name is rejected (fail-closed).
try:
    emit_abyss_event("hermes:evil")
    rejected = False
except ValueError:
    rejected = True
check("namespaced emit rejected", rejected)

print("=== Redaction masking (#65449) ===")
check("nvapi key masked", _mask_secrets("token nvapi-abc12345678901234567890123 x") == "token *** x")
check("sk- key masked", _mask_secrets("key sk-abcdefghijklmnopqrstuvwxyz123") == "key ***")
check("aws key masked", _mask_secrets("AKIAABCDEFGHIJKLMNOP") == "***")
# Durable event-bus payloads are masked before storage (hardening pass)
wave._CTX = None
emit_abyss_event("secret_evt", {"key": "sk-abc12345678901234567890123456", "ok": 1})
rows = conn_rows("SELECT * FROM plugin_events WHERE event='secret_evt'")
check("event payload secret masked", rows and "sk-" not in (rows[0]["payload"] or ""), json.dumps(rows[0]["payload"])[:120] if rows else "none")
check("event payload benign fields kept", rows and json.loads(rows[0]["payload"]).get("ok") == 1)
# Platform-event payloads are masked before storage (hardening pass)
_on_gateway_platform_event(platform="telegram", event_type="secret_msg", payload={"token": "sk-abc12345678901234567890123456"})
rows = conn_rows("SELECT * FROM platform_events WHERE event_type='secret_msg'")
check("platform payload secret masked", rows and "sk-" not in (rows[0]["payload"] or ""), json.dumps(rows[0]["payload"])[:120] if rows else "none")
wave._CTX = fake

print("=== /wave/* REST endpoints ===")
reset_wave()
emit_abyss_event("test_event", {"a": 1})
r = wave_handle("GET", "/wave/events", {"limit": 10})
check("GET /wave/events returns list", isinstance(r, list) and r and r[0]["event"] == "test_event")
r = wave_handle("GET", "/wave/streams", {"limit": 10})
check("GET /wave/streams returns list", isinstance(r, list))
r = wave_handle("GET", "/wave/api", {"limit": 10})
check("GET /wave/api returns list", isinstance(r, list))
r = wave_handle("GET", "/wave/subagents", {"limit": 10})
check("GET /wave/subagents returns list", isinstance(r, list))
r = wave_handle("GET", "/wave/approvals", {"limit": 10})
check("GET /wave/approvals returns list", isinstance(r, list))
r = wave_handle("GET", "/wave/commands", {"limit": 10})
check("GET /wave/commands returns list", isinstance(r, list))
r = wave_handle("GET", "/wave/platform", {"limit": 10})
check("GET /wave/platform returns list", isinstance(r, list))
r = wave_handle("GET", "/wave/skills", {"limit": 10})
check("GET /wave/skills returns list", isinstance(r, list))
r = wave_handle("GET", "/wave/summary")
check("GET /wave/summary has tables", isinstance(r, dict) and "tables" in r and "streams" in r["tables"])
r = wave_handle("GET", "/wave/nope")
check("unknown wave endpoint 404", isinstance(r, dict) and r.get("code") == 404)

print("=== POST /wave/emit through handle_request ===")
wave._CTX = fake
r = handle_request("POST", "/wave/emit", body=json.dumps({"event": "from_http", "payload": {"x": 1}}))
check("POST /wave/emit returns emitted", isinstance(r, dict) and r.get("emitted") == "abyss:from_http", json.dumps(r)[:160])
check("POST /wave/emit fired ctx.emit", any(e[0] == "from_http" for e in fake.emitted))
rows = conn_rows("SELECT * FROM plugin_events WHERE event='from_http'")
check("POST /wave/emit durable row", len(rows) == 1)

print("=== Ownership ledger cleanup (#64229) ===")
wave._CTX = fake
_on_stream_start(turn_id="t9", session_id="s9", model="m", provider="p", surface="cli")
check("stream state has t9", ("s9", "t9") in wave._STREAMS)
wave._on_unload()
check("on_unload clears ctx", wave._CTX is None)
check("on_unload clears stream state", not wave._STREAMS)

print(f"\n=== RESULT: {PASS} passed, {FAIL} failed ===")
sys.exit(1 if FAIL else 0)
