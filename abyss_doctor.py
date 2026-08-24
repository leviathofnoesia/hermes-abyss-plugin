"""Abyss doctor — full overarching diagnosis with user-approval-gated fixes.

Extracted from the plugin god-file (Clean Architecture, use-case layer).
The doctor agent is dispatched via the shared spawn infrastructure in
``abyss_agent`` (``_spawn_agent``, report IO, ``_mark_resolution``,
``_redact``). Core dependencies (``_init_db``, ``_get_activity_conn``,
``_add_activity``, ``_RESOLUTION_DIR``, ``_AGENT_DEFAULT_TIMEOUT``,
``HERMES_HOME``, ``PROFILE_HOME``, ``get_health``, ``get_failures``) are
imported lazily inside the functions, exactly like abyss_wave.py — no import
cycle with ``__init__``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger("hermes.plugins.abyss.doctor")


def _doctor_context() -> dict:
    """Everything the doctor agent needs to form an overarching diagnosis."""
    from __init__ import _get_activity_conn, _init_db, get_failures, get_health
    from abyss_agent import _redact

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
    from __init__ import _AGENT_DEFAULT_TIMEOUT, _add_activity
    from abyss_agent import _read_report_file, _spawn_agent, _write_report_file

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
    from __init__ import _RESOLUTION_DIR, _add_activity

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
    from __init__ import _RESOLUTION_DIR
    from abyss_agent import _read_report_file

    if not report_id or not re.fullmatch(r"[A-Za-z0-9._-]+", report_id):
        return {"status": "invalid", "error": "bad report_id"}
    report_path = _RESOLUTION_DIR / f"{report_id}.json"
    report = _read_report_file(report_path)
    if report is None:
        return {"status": "running", "report_id": report_id}
    return {"status": "ready", "report_id": report_id, "report": report}


def _doctor_log(report_id: str) -> dict:
    """Stream endpoint: live tail of the spawned agent's captured stdout.

    The agent's stdout is captured to a sibling ``.log`` (see
    ``abyss_agent._spawn_agent``); the UI polls this to render a live
    transcript under the "doctor dispatched" state so the operator can watch
    the agent work instead of staring at a silent "running".
    """
    from __init__ import _RESOLUTION_DIR

    if not report_id or not re.fullmatch(r"[A-Za-z0-9._-]+", report_id):
        return {"status": "invalid", "error": "bad report_id"}
    log_path = _RESOLUTION_DIR / f"{report_id}.log"
    try:
        data = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        data = ""
    return {"status": "ok", "report_id": report_id, "bytes": len(data), "log": data[-4000:]}


def _resolve_agent_root(home) -> "Path":
    """Locate the hermes-agent tree.

    ``HERMES_HOME`` is the install root in normal runs, but under
    ``hermes serve --profile <name>`` the CLI pre-parse reassigns it to the
    profile dir (``.../profiles/<name>``), where ``hermes-agent`` does not
    live. Walk up at most four levels to the ancestor that actually contains
    the ``hermes-agent`` tree (the install root), falling back to the legacy
    ``HERMES_HOME / "hermes-agent"`` if none is found.
    """
    candidate = home
    for _ in range(4):
        agent = candidate / "hermes-agent"
        if agent.is_dir():
            return agent
        candidate = candidate.parent
    return home / "hermes-agent"


def _run_benchmark() -> dict:
    """Run the Abyss Bench Layer 1 probe suite (deterministic, zero tokens).

    Invokes ``evals/abyssbench/runner.py probes --json`` from the hermes-agent
    tree and returns the per-probe results. Used by the health-tab benchmark
    button so a doctor's fixes are scored against the regression suite.
    """
    from __init__ import HERMES_HOME, PROFILE_HOME

    agent_root = _resolve_agent_root(Path(HERMES_HOME))
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
    from __init__ import _RESOLUTION_DIR

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
    from __init__ import _AGENT_DEFAULT_TIMEOUT, _add_activity
    from abyss_agent import _mark_resolution, _read_report_file, _spawn_agent, _write_report_file

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
    from __init__ import _RESOLUTION_DIR, _add_activity
    from abyss_agent import _read_report_file, _redact, _write_report_file

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
