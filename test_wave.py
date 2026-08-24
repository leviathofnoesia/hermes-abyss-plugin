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
import time
from datetime import datetime, timedelta
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
    _session_activity_count,
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



def _run_script():
    global PASS, FAIL
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

    print("=== Empty-stream coalescing (repeat flood suppression) ===")
    # Same session keeps hitting zero-token streams: a context-window-exhausted
    # session once inserted one signal ROW per stream (628 rows in one session,
    # 2,270 unresolved total), flooding the Watch tab and pinning the health
    # score at 0. Within the coalesce window, repeated empty streams must bump
    # details.repeat_count on the existing unresolved signal, NOT insert rows.
    for i in range(3):
        _on_stream_start(turn_id=f"t2c{i}", session_id="s2", model="m1", provider="p1", surface="cli")
        _on_stream_end(turn_id=f"t2c{i}", session_id="s2", final_text="", finished=True)
    sig_rows2 = conn_rows(
        "SELECT * FROM signals WHERE signal_type='empty_stream' AND session_id='s2'"
    )
    check("empty_stream coalesced in same session", len(sig_rows2) == 1,
          f"rows={len(sig_rows2)}")
    if sig_rows2:
        details = json.loads(sig_rows2[0]["details"] or "{}")
        check("empty_stream repeat_count recorded",
              details.get("repeat_count", 0) == 3, json.dumps(details))
    # A DIFFERENT session's first empty stream must still get its own signal.
    _on_stream_start(turn_id="t3", session_id="s3", model="m1", provider="p1", surface="cli")
    _on_stream_end(turn_id="t3", session_id="s3", final_text="", finished=True)
    sig_rows3 = conn_rows(
        "SELECT * FROM signals WHERE signal_type='empty_stream' AND session_id='s3'"
    )
    check("empty_stream first-in-session inserts row", len(sig_rows3) == 1,
          f"rows={len(sig_rows3)}")

    print("=== Interrupted-stream signal (abnormal termination) ===")
    # A stream that ENDS without finishing (finished=False) or with an error
    # is a truncated-response failure class that previously produced NO
    # signal (only the streams row). Regression: on_stream_end with
    # finished=False must emit stream_interrupted; an explicit error must
    # emit stream_interrupted with error severity — and neither may fire
    # empty_stream.
    reset_wave()
    _on_stream_start(turn_id="i1", session_id="s8", model="m1", provider="p1", surface="cli")
    _on_stream_delta(turn_id="i1", session_id="s8", delta="partial", kind="text")
    _on_stream_end(turn_id="i1", session_id="s8", final_text="partial", finished=False)
    sig_i1 = conn_rows("SELECT * FROM signals WHERE signal_type='stream_interrupted'")
    check("stream_interrupted recorded for finished=False", len(sig_i1) == 1,
          json.dumps(sig_i1[0])[:200] if sig_i1 else "none")
    if sig_i1:
        check("stream_interrupted severity=warning", sig_i1[0]["severity"] == "warning",
              str(sig_i1[0]["severity"]))
    check("finished=False does NOT fire empty_stream",
          len(conn_rows("SELECT * FROM signals WHERE signal_type='empty_stream'")) == 0)

    # Error case: severity must be error and surface the masked error text.
    _on_stream_start(turn_id="i2", session_id="s9", model="m1", provider="p1", surface="cli")
    _on_stream_end(turn_id="i2", session_id="s9", final_text="", finished=True, error="stream boom")
    sig_i2 = conn_rows(
        "SELECT * FROM signals WHERE signal_type='stream_interrupted' AND session_id='s9'"
    )
    check("stream_interrupted recorded for errored stream", len(sig_i2) == 1,
          json.dumps(sig_i2[0])[:200] if sig_i2 else "none")
    if sig_i2:
        check("stream_interrupted severity=error", sig_i2[0]["severity"] == "error",
              str(sig_i2[0]["severity"]))
        check("stream_interrupted error surfaced in description",
              "stream boom" in (sig_i2[0]["description"] or ""),
              str(sig_i2[0]["description"])[:120])
    check("errored stream does NOT fire empty_stream",
          len(conn_rows("SELECT * FROM signals WHERE signal_type='empty_stream'")) == 0)

    print("=== Interrupted-stream coalescing (repeat flood suppression) ===")
    # Same session stream keeps dying: 3 more interrupted streams must bump
    # repeat_count on the one unresolved row (same flood class as
    # empty_stream), and severity must NOT downgrade from the earlier error.
    for i in range(3):
        _on_stream_start(turn_id=f"i2c{i}", session_id="s9", model="m1", provider="p1", surface="cli")
        _on_stream_delta(turn_id=f"i2c{i}", session_id="s9", delta="partial", kind="text")
        _on_stream_end(turn_id=f"i2c{i}", session_id="s9", final_text="partial", finished=False)
    sig_i2c = conn_rows(
        "SELECT * FROM signals WHERE signal_type='stream_interrupted' AND session_id='s9'"
    )
    check("stream_interrupted coalesced in same session", len(sig_i2c) == 1,
          f"rows={len(sig_i2c)}")
    if sig_i2c:
        details = json.loads(sig_i2c[0]["details"] or "{}")
        check("stream_interrupted repeat_count recorded",
              details.get("repeat_count", 0) == 3, json.dumps(details))
        check("stream_interrupted severity not downgraded",
              sig_i2c[0]["severity"] == "error", str(sig_i2c[0]["severity"]))

    print("=== Cap-evicted stream must NOT fire false empty_stream ===")
    # Under heavy concurrency (>512 parallel streams) _on_stream_start evicts
    # the oldest accumulator at the _STREAM_MAX cap. That stream's LATE
    # on_stream_end then arrives with NO accumulator: the old code re-created
    # a chars=0 placeholder and fired a FALSE empty_stream signal for a stream
    # that may have streamed plenty of tokens (tracking loss, not emptiness).
    # Regression: evicted ends must persist the row (flagged incomplete) and
    # stay silent, while a genuinely empty OBSERVED stream still signals.
    reset_wave()
    orig_max = wave._STREAM_MAX
    try:
        wave._STREAM_MAX = 1  # force eviction on the second start
        wave._STREAMS.clear()
        wave._EVICTED.clear()
        _on_stream_start(turn_id="e1", session_id="s-evict", model="m1", provider="p1", surface="cli")
        _on_stream_delta(turn_id="e1", session_id="s-evict", delta="real tokens", kind="text")
        _on_stream_start(turn_id="e2", session_id="s-evict-2", model="m2", provider="p2", surface="cli")
        check("cap eviction marks the evicted stream",
              ("s-evict", "e1") in wave._EVICTED, str(wave._EVICTED))
        # e1's end arrives late — accumulator already evicted.
        _on_stream_end(turn_id="e1", session_id="s-evict", final_text="real tokens", finished=True)
        rows_e = conn_rows("SELECT * FROM streams WHERE turn_id='e1'")
        check("evicted stream row still persisted", len(rows_e) == 1,
              json.dumps(rows_e[0])[:160] if rows_e else "none")
        if rows_e:
            check("evicted stream row notes telemetry incomplete",
                  "evicted" in (rows_e[0].get("error") or "").lower(),
                  str(rows_e[0].get("error"))[:120])
        sig_e = conn_rows(
            "SELECT * FROM signals WHERE signal_type='empty_stream' AND session_id='s-evict'"
        )
        check("evicted stream does NOT fire false empty_stream", len(sig_e) == 0,
              json.dumps(sig_e[0])[:160] if sig_e else "none")
        # The genuinely empty stream (start observed, zero tokens) STILL fires.
        _on_stream_end(turn_id="e2", session_id="s-evict-2", final_text="", finished=True)
        sig_e2 = conn_rows(
            "SELECT * FROM signals WHERE signal_type='empty_stream' AND session_id='s-evict-2'"
        )
        check("observed empty stream still signals", len(sig_e2) == 1,
              json.dumps(sig_e2[0])[:160] if sig_e2 else "none")
    finally:
        wave._STREAM_MAX = orig_max
        wave._STREAMS.clear()
        wave._EVICTED.clear()

    print("=== Stuck-stream sweep (orphaned accumulator detection) ===")
    reset_wave()
    wave._STREAMS.clear()
    # Simulate a stream that started > _STREAM_STUCK_AFTER ago and never
    # received on_stream_end (provider hang / dropped hook / killed process).
    # _on_stream_start must sweep it into a durable finished=0 row + a
    # stuck_stream signal instead of silently evicting it at the cap.
    wave._STREAMS[("hang-sess", "hang-turn")] = {
        "started": time.time() - wave._STREAM_STUCK_AFTER - 60,
        "chars": 12,
        "deltas": 3,
        "kinds": {"text": 2, "reasoning": 1},
        "iteration": 1,
        "model": "m-hang",
        "provider": "p-hang",
        "surface": "cli",
        "session_id": "hang-sess",
        "turn_id": "hang-turn",
        "first_token_ms": 250,
    }
    _on_stream_start(turn_id="ok-turn", session_id="ok-sess", model="m", provider="p", surface="cli")
    check("stale stream removed from accumulator", ("hang-sess", "hang-turn") not in wave._STREAMS)
    hang_rows = conn_rows("SELECT * FROM streams WHERE turn_id='hang-turn'")
    check("stuck stream persisted as finished=0", len(hang_rows) == 1 and hang_rows[0]["finished"] == 0,
          json.dumps(hang_rows[0])[:160] if hang_rows else "none")
    if hang_rows:
        check("stuck stream error noted", "stuck" in (hang_rows[0].get("error") or ""),
              str(hang_rows[0].get("error"))[:80])
        check("stuck stream partial telemetry preserved",
              hang_rows[0]["chars"] == 12 and hang_rows[0]["deltas"] == 3,
              f"chars={hang_rows[0].get('chars')} deltas={hang_rows[0].get('deltas')}")
    hang_sig = conn_rows("SELECT * FROM signals WHERE signal_type='stuck_stream'")
    check("stuck_stream signal recorded", len(hang_sig) == 1, json.dumps(hang_sig[0])[:160] if hang_sig else "none")
    if hang_sig:
        check("stuck_stream source=wave", hang_sig[0]["source"] == "wave")
        check("stuck_stream carries session", hang_sig[0]["session_id"] == "hang-sess")
    check("fresh stream still accumulates", ("ok-sess", "ok-turn") in wave._STREAMS)

    print("=== Stuck sweep keys on SILENCE, not age (long-stream regression) ===")
    # Regression (2026-08-24): a legitimate generation that has been streaming
    # for > _STREAM_STUCK_AFTER (big analysis turn, long doc generation) keeps
    # receiving on_stream_delta. The old age-only check (`now - started`)
    # swept it as "stuck" the next time ANY new stream started — false
    # stuck_stream warning, bogus finished=0 row, duplicate late-end row.
    # Liveness is now tracked per-accumulator via `last_seen`; an old stream
    # with recent deltas must survive the sweep, a genuinely silent one must
    # still be caught.
    wave._STREAMS.clear()
    wave._STREAMS[("active-sess", "active-turn")] = {
        "started": time.time() - wave._STREAM_STUCK_AFTER - 120,  # old
        "last_seen": time.time() - 3,                             # but live
        "chars": 5000,
        "deltas": 200,
        "kinds": {"text": 200},
        "iteration": 0,
        "model": "m-active",
        "provider": "p-active",
        "surface": "cli",
        "session_id": "active-sess",
        "turn_id": "active-turn",
        "first_token_ms": 900,
    }
    _on_stream_start(turn_id="trigger-turn", session_id="trigger-sess", model="m", provider="p", surface="cli")
    check("actively-streaming old stream NOT swept as stuck",
          ("active-sess", "active-turn") in wave._STREAMS,
          f"keys={list(wave._STREAMS.keys())}")
    active_rows = conn_rows("SELECT * FROM streams WHERE turn_id='active-turn'")
    check("active stream not persisted as stuck", len(active_rows) == 0,
          json.dumps(active_rows[0])[:160] if active_rows else "none")
    active_sig = conn_rows("SELECT * FROM signals WHERE signal_type='stuck_stream' AND session_id='active-sess'")
    check("no stuck_stream signal for active stream", len(active_sig) == 0,
          str(active_sig))
    # A stream SILENT for the threshold is still swept (core path unchanged);
    # its row now surfaces how long it was idle and how many deltas arrived.
    wave._STREAMS.clear()
    wave._STREAMS[("silent-sess", "silent-turn")] = {
        "started": time.time() - wave._STREAM_STUCK_AFTER - 60,
        "last_seen": time.time() - wave._STREAM_STUCK_AFTER - 60,
        "chars": 3,
        "deltas": 1,
        "kinds": {"text": 1},
        "iteration": 0,
        "model": "m-silent",
        "provider": "p-silent",
        "surface": "cli",
        "session_id": "silent-sess",
        "turn_id": "silent-turn",
        "first_token_ms": 700,
    }
    _on_stream_start(turn_id="trigger-turn2", session_id="trigger-sess2", model="m", provider="p", surface="cli")
    check("silent old stream still swept", ("silent-sess", "silent-turn") not in wave._STREAMS,
          f"keys={list(wave._STREAMS.keys())}")
    silent_rows = conn_rows("SELECT * FROM streams WHERE turn_id='silent-turn'")
    check("silent stream persisted as finished=0", len(silent_rows) == 1 and silent_rows[0]["finished"] == 0,
          json.dumps(silent_rows[0])[:160] if silent_rows else "none")
    if silent_rows:
        check("stuck row notes idle time + deltas",
              "idle" in (silent_rows[0].get("error") or "") and "1 delta" in (silent_rows[0].get("error") or ""),
              str(silent_rows[0].get("error"))[:120])
    wave._STREAMS.clear()

    print("=== _session_activity_count timezone-aware 24h window ===")
    # Regression: the 24h cutoff must be computed in LOCAL time to match
    # how activity timestamps are stored (datetime.now().isoformat()).
    # SQLite's datetime('now','-24 hours') is UTC with a space separator,
    # which widened the window by the UTC offset ('T' > ' ' ordering), so
    # sessions active 24-28h ago could trip the context-exhaustion hint.
    reset_wave()
    _now = datetime.now()
    _old_ts = (_now - timedelta(hours=26)).isoformat()   # outside window
    _edge_ts = (_now - timedelta(hours=10)).isoformat()  # inside window
    _conn = __init__._get_activity_conn()
    try:
        _conn.execute(
            "INSERT INTO activity (timestamp, action, category, status, session_id, tool_name) "
            "VALUES (?, 'probe', 'test', 'completed', 'tz-sess', 'write_file')",
            (_old_ts,),
        )
        _conn.execute(
            "INSERT INTO activity (timestamp, action, category, status, session_id, tool_name) "
            "VALUES (?, 'probe', 'test', 'completed', 'tz-sess', 'write_file')",
            (_edge_ts,),
        )
        _conn.commit()
    finally:
        _conn.close()
    _cnt = _session_activity_count("tz-sess")
    check("session_activity_count excludes >24h rows (local-time cutoff)",
          _cnt == 1, f"count={_cnt} (expect 1; old UTC cutoff returned 2)")
    _cnt_none = _session_activity_count("")
    check("session_activity_count empty session fail-open 0", _cnt_none == 0)
    _cnt_missing = _session_activity_count("no-such-session")
    check("session_activity_count unknown session 0", _cnt_missing == 0)

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

    print("=== Truncated-response signal (silent failure) ===")
    _on_pre_api_request(
    api_request_id="req-3", session_id="s1", turn_id="t3", model="m1",
    provider="p1", api_mode="chat", api_call_count=1,
    )
    _on_post_api_request(
    api_request_id="req-3", session_id="s1", model="m1", provider="p1",
    api_duration=2.0, finish_reason="length",
    usage={"input_tokens": 50, "output_tokens": 4096},
    )
    rows = conn_rows("SELECT * FROM api_requests WHERE api_request_id='req-3'")
    check("truncated api request closed completed", rows and rows[0]["status"] == "completed")
    check("finish_reason length recorded", rows and rows[0]["finish_reason"] == "length")
    sig_rows = conn_rows("SELECT * FROM signals WHERE signal_type='truncated_response'")
    check("truncated_response signal recorded", len(sig_rows) == 1,
          json.dumps(sig_rows[0])[:200] if sig_rows else "none")
    if sig_rows:
        check("truncated_response severity warning", sig_rows[0]["severity"] == "warning")
        check("truncated_response carries session", sig_rows[0]["session_id"] == "s1")
        check("truncated_response carries token count", "4096" in (sig_rows[0]["description"] or ""))
    # A normal 'stop' finish_reason must NOT fire truncated_response.
    _on_pre_api_request(
    api_request_id="req-4", session_id="s1", turn_id="t4", model="m1",
    provider="p1", api_mode="chat", api_call_count=1,
    )
    _on_post_api_request(
    api_request_id="req-4", session_id="s1", model="m1", provider="p1",
    api_duration=0.5, finish_reason="stop",
    usage={"input_tokens": 10, "output_tokens": 20},
    )
    sig_rows = conn_rows("SELECT * FROM signals WHERE signal_type='truncated_response'")
    check("stop finish_reason does not signal", len(sig_rows) == 1)

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

    print("=== Approval parallel pending (regression) ====")
    reset_wave()
    _on_pre_approval_request(command="rm -rf /tmp/a", pattern_key="dangerous_a", session_key="sk-2", surface="cli")
    _on_pre_approval_request(command="rm -rf /tmp/b", pattern_key="dangerous_b", session_key="sk-2", surface="cli")
    _on_post_approval_response(choice="deny", decided_by="user", session_key="sk-2", pattern_key="dangerous_a", surface="cli")
    rows = conn_rows("SELECT choice, pattern_key FROM approvals WHERE session_key='sk-2' ORDER BY id ASC")
    check("parallel approval: only matched row closed",
          rows and rows[0]["choice"] == "deny" and rows[1]["choice"] == "pending",
          json.dumps(rows))
    # Without a pattern_key, only the NEWEST pending row is closed (old code
    # updated every pending row for the session with one response).
    _on_post_approval_response(choice="allow", decided_by="user", session_key="sk-2", surface="cli")
    rows = conn_rows("SELECT choice FROM approvals WHERE session_key='sk-2' ORDER BY id ASC")
    check("parallel approval: newest-only close", rows and rows[0]["choice"] == "deny" and rows[1]["choice"] == "allow",
          json.dumps(rows))

    print("=== api_error + truncated_response coalescing (flood suppression) ===")
    # Same flood class as empty_stream: a provider outage retries every request
    # (one api_error ROW each) and a session pinned at its output budget hits
    # finish_reason='length' every request (one truncated_response ROW each).
    # Both must now coalesce into one unresolved signal with repeat_count.
    reset_wave()
    for i in range(3):
        _on_pre_api_request(
            api_request_id=f"req-e{i}", session_id="s-err", turn_id=f"t{i}", model="m1",
            provider="p1", api_mode="chat", api_call_count=1,
        )
        _on_api_request_error(
            api_request_id=f"req-e{i}", session_id="s-err", provider="p1", model="m1",
            status_code=429, retryable=True, error_type="rate_limit", error_message="too many",
        )
    sig_rows = conn_rows("SELECT * FROM signals WHERE signal_type='api_error' AND session_id='s-err'")
    check("api_error coalesced in same session", len(sig_rows) == 1, f"rows={len(sig_rows)}")
    if sig_rows:
        details = json.loads(sig_rows[0]["details"] or "{}")
        check("api_error repeat_count recorded", details.get("repeat_count", 0) == 2, json.dumps(details))
        check("api_error retryable stays warning", sig_rows[0]["severity"] == "warning")
    # A DIFFERENT session's first api_error still inserts its own row.
    _on_pre_api_request(api_request_id="req-e9", session_id="s-other", turn_id="t9",
                        model="m1", provider="p1", api_mode="chat", api_call_count=1)
    _on_api_request_error(
        api_request_id="req-e9", session_id="s-other", provider="p1", model="m1",
        status_code=429, retryable=True, error_type="rate_limit", error_message="too many",
    )
    sig_other = conn_rows("SELECT * FROM signals WHERE signal_type='api_error' AND session_id='s-other'")
    check("api_error first-in-session inserts row", len(sig_other) == 1, f"rows={len(sig_other)}")
    # Severity escalation: retryable 429 warning storm turns into a hard 500 —
    # the stored row must escalate to error, not hide behind repeat_count.
    _on_pre_api_request(api_request_id="req-e10", session_id="s-err", turn_id="t10",
                        model="m1", provider="p1", api_mode="chat", api_call_count=1)
    _on_api_request_error(
        api_request_id="req-e10", session_id="s-err", provider="p1", model="m1",
        status_code=500, retryable=False, error_type="server_error", error_message="boom",
    )
    sig_rows = conn_rows("SELECT * FROM signals WHERE signal_type='api_error' AND session_id='s-err'")
    check("api_error storm still one row", len(sig_rows) == 1, f"rows={len(sig_rows)}")
    if sig_rows:
        check("api_error severity escalated to error",
              sig_rows[0]["severity"] == "error", f"severity={sig_rows[0]['severity']}")
        details = json.loads(sig_rows[0]["details"] or "{}")
        check("api_error escalation keeps repeat_count", details.get("repeat_count", 0) == 3, json.dumps(details))
    # truncated_response: repeated finish_reason='length' must coalesce too.
    for i in range(3):
        _on_pre_api_request(
            api_request_id=f"req-t{i}", session_id="s-len", turn_id=f"tl{i}", model="m1",
            provider="p1", api_mode="chat", api_call_count=1,
        )
        _on_post_api_request(
            api_request_id=f"req-t{i}", session_id="s-len", model="m1", provider="p1",
            api_duration=2.0, finish_reason="length",
            usage={"input_tokens": 50, "output_tokens": 4096},
        )
    sig_rows = conn_rows("SELECT * FROM signals WHERE signal_type='truncated_response' AND session_id='s-len'")
    check("truncated_response coalesced in same session", len(sig_rows) == 1, f"rows={len(sig_rows)}")
    if sig_rows:
        details = json.loads(sig_rows[0]["details"] or "{}")
        check("truncated_response repeat_count recorded", details.get("repeat_count", 0) == 2, json.dumps(details))

    print("=== approval_denied correlates to session ===")
    reset_wave()
    _on_pre_approval_request(command="rm -rf /tmp/y", pattern_key="dangerous_y", session_key="sk-deny", surface="cli")
    _on_post_approval_response(choice="deny", decided_by="user", session_key="sk-deny", surface="cli")
    sig_rows = conn_rows("SELECT * FROM signals WHERE signal_type='approval_denied'")
    check("approval_denied signal recorded", len(sig_rows) == 1)
    check("approval_denied carries session_key", sig_rows and sig_rows[0]["session_id"] == "sk-deny",
              f"session_id={sig_rows[0]['session_id'] if sig_rows else 'none'}")

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

    print("=== Cross-process lock contention: retry + noise-free fail-open ===")
    reset_wave()
    import logging as _logging
    import sqlite3 as _sqlite3
    from abyss_wave import _wave_insert, _wave_update, _wave_signal

    # 1) A transient 'database is locked' on the FIRST attempt must be retried
    #    with a short backoff, and succeed on the second attempt. We force the
    #    first conn.execute to raise, then let the retry proceed normally.
    _orig_get = __init__._get_activity_conn
    _attempts = {"n": 0}

    class _FakeConnOnce:
        def __init__(self, real):
            self._real = real
        def execute(self, sql, args=()):
            if _attempts["n"] == 0 and sql.strip().upper().startswith("INSERT"):
                _attempts["n"] += 1
                raise _sqlite3.OperationalError("database is locked")
            return self._real.execute(sql, args)
        def commit(self): return self._real.commit()
        def close(self): return self._real.close()

    def _flaky_conn():
        conn = _orig_get()
        return _FakeConnOnce(conn)

    __init__._get_activity_conn = _flaky_conn
    try:
        rid = _wave_insert("plugin_events",
                          timestamp="2026-08-16T00:00:00",
                          namespace="abyss", event="locked_then_ok",
                          payload="{}")
    finally:
        __init__._get_activity_conn = _orig_get
    check("locked-then-ok insert retried and succeeded", rid is not None, f"rid={rid}")
    rows = conn_rows("SELECT * FROM plugin_events WHERE event='locked_then_ok'")
    check("locked-then-ok row persisted after retry", len(rows) == 1)

    # 2) On persistent lock failure, the log level must be DEBUG (not WARNING),
    #    because 'database is locked' under cross-process WAL contention is an
    #    expected transient race, not an operator-actionable event.
    class _Capture(_logging.Handler):
        def __init__(self):
            super().__init__()
            self.records = []
        def emit(self, r):
            self.records.append(r)
    h = _Capture()
    h.setLevel(_logging.DEBUG)
    wlog = _logging.getLogger("hermes.plugins.abyss.wave")
    wlog.addHandler(h)
    prev_level = wlog.level
    wlog.setLevel(_logging.DEBUG)

    def _always_locked_conn():
        class _LockedConn:
            def execute(self, sql, args=()):
                raise _sqlite3.OperationalError("database is locked")
            def commit(self): raise _sqlite3.OperationalError("database is locked")
            def close(self): pass
        return _LockedConn()

    __init__._get_activity_conn = _always_locked_conn
    try:
        import abyss_wave as _w
        _orig_sleep = _w._LOCK_RETRY_SLEEP
        _w._LOCK_RETRY_SLEEP = 0.0
        _wave_insert("plugin_events", timestamp="x", namespace="abyss",
                     event="persists_locked", payload="{}")
        _w._LOCK_RETRY_SLEEP = _orig_sleep
    finally:
        __init__._get_activity_conn = _orig_get
        wlog.removeHandler(h)
        wlog.setLevel(prev_level)
    warning_records = [r for r in h.records if r.levelno == _logging.WARNING]
    debug_records = [r for r in h.records if r.levelno == _logging.DEBUG]
    check("persistent locked -> no WARNING logged", len(warning_records) == 0,
          f"saw {[r.getMessage() for r in warning_records]}")
    check("persistent locked -> DEBUG logged (noise downgraded)", len(debug_records) >= 1,
          f"saw {len(debug_records)} debug")

    print("=== Wave retention pruning (#night-shift) ===")
    reset_wave()
    old_ts = (datetime.now() - timedelta(days=60)).isoformat()
    new_ts = datetime.now().isoformat()
    # Seed every wave table with one OLD row and one NEW row so both the
    # prune sweep and the keep-side are exercised across all 8 surfaces.
    _seed = {
        "plugin_events": dict(namespace="abyss", event="prune_probe", payload="{}"),
        "streams": dict(session_id="s-prune", turn_id="t-prune", model="m",
                        provider="p", surface="cli", iteration=1, finished=1),
        "api_requests": dict(session_id="s-prune", provider="p", model="m",
                             api_mode="chat", status="completed"),
        "subagents": dict(parent_session_id="s-prune", child_session_id="c-prune",
                         child_role="coder", status="completed"),
        "approvals": dict(surface="cli", pattern_key="terminal", choice="approved"),
        "commands": dict(surface="cli", command="echo", alias_used="", args_preview="hi"),
        "platform_events": dict(platform="buzz", event_type="connected", payload="{}"),
        "skills": dict(name="demo-skill", action="view", provenance="cron", details=""),
    }
    for table, fields in _seed.items():
        _wave_insert(table, timestamp=old_ts, **fields)
        _wave_insert(table, timestamp=new_ts, **fields)
    counts = wave.prune_wave_data(days=30)
    bad = {t: c for t, c in counts.items() if c != 1}
    check("prune_wave_data deletes 1 old row per wave table",
          len(bad) == 0, f"unexpected counts: {bad or counts}")
    for table in wave._WAVE_TABLES:
        rows = conn_rows(f"SELECT timestamp FROM {table}")
        check(f"prune keeps new {table} row", len(rows) == 1, f"{len(rows)} rows")
    zero = wave.prune_wave_data(0)
    check("prune_wave_data(0) no-op zeros",
          all(v == 0 for v in zero.values()) and len(zero) == len(wave._WAVE_TABLES),
          str(zero))
    # Core _prune_data must merge wave counts so /abyss prune shows them.
    _wave_insert("plugin_events", timestamp=old_ts, namespace="abyss",
                 event="prune_probe2", payload="{}")
    merged = __init__._prune_data(days=30)
    check("_prune_data merges wave counts",
          merged.get("plugin_events") == 1 and all(t in merged for t in wave._WAVE_TABLES),
          str({k: v for k, v in merged.items() if k in wave._WAVE_TABLES}))
    check("_prune_data wave zero-count on no-op",
          all(__init__._prune_data(0).get(t) == 0 for t in wave._WAVE_TABLES),
          str(__init__._prune_data(0))[:200])

    print("=== Wave clear (clean command parity) ===")
    _wave_insert("streams", timestamp=new_ts, session_id="s-clear", turn_id="t-clear",
                 model="m", provider="p", finished=1)
    _wave_insert("api_requests", timestamp=new_ts, session_id="s-clear", provider="p",
                 model="m", api_mode="chat", status="completed")
    cleared = wave.clear_wave_data()
    left = {t: len(conn_rows(f"SELECT id FROM {t}")) for t in wave._WAVE_TABLES}
    bad_left = {t: n for t, n in left.items() if n}
    check("clear_wave_data empties all wave tables",
          not bad_left, f"leftover: {bad_left or left}")
    check("clear_wave_data returns per-table counts",
          all(t in cleared for t in wave._WAVE_TABLES), str(list(cleared)[:4]))

    print(f"\n=== RESULT: {PASS} passed, {FAIL} failed ===")
    sys.exit(1 if FAIL else 0)


def test_runner():
    global PASS, FAIL
    PASS = 0  # reset in case pytest imports the file
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
