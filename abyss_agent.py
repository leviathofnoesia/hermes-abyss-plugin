"""Abyss resolver agent — spawn infrastructure + resolution use case.

Extracted from the plugin god-file (Clean Architecture, use-case layer).
Owns the dispatched-agent process registry (``_AGENT_PROCS``) and its atexit
cleanup, the ``hermes chat -q`` command construction, report-file IO, and the
signal/incident resolver orchestration. All core dependencies (``_init_db``,
``_get_activity_conn``, ``_add_activity``, ``_RESOLUTION_DIR``,
``_AGENT_DEFAULT_TIMEOUT``, ``HERMES_HOME``, ``PROFILE_HOME``) are imported
lazily inside the functions, exactly like abyss_wave.py — no import cycle
with ``__init__``.
"""

from __future__ import annotations

import atexit as _atexit
import json
import logging
import os
import shlex
import shutil
import sqlite3
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("hermes.plugins.abyss.agent")


def _redact(value: Any, limit: int = 200) -> Any:
    """Redact secret-looking keys and truncate long values before embedding
    tool arguments / error payloads into an agent prompt."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            kl = str(k).lower()
            if any(t in kl for t in ("token", "secret", "password", "api_key", "apikey", "authorization", "auth")):
                out[k] = "***"
            else:
                out[k] = _redact(v, limit)
        return out
    if isinstance(value, (list, tuple)):
        return [_redact(v, limit) for v in value[:20]]
    if value is None:
        return None
    s = str(value)
    return s if len(s) <= limit else s[:limit] + "…"


def _resolve_agent_cmd(prompt: str) -> list:
    """Build the command list that runs the Abyss agent.

    Honors the ``ABYSS_AGENT_CMD`` override (either a JSON list of argv or a
    shlex string; the prompt is placed after ``-q`` if present, else appended)
    — used by the test suites to stub the agent. The default is the `hermes`
    CLI with the abyss-doctor skill preloaded and quiet mode, so it runs as
    the same profile (free Nous model by default).
    """
    from __init__ import HERMES_HOME

    override = os.environ.get("ABYSS_AGENT_CMD", "").strip()
    if override:
        argv = None
        if override.startswith("["):
            try:
                argv = json.loads(override)
            except (ValueError, TypeError):
                argv = None
        if argv is None:
            argv = shlex.split(override)
        if "-q" in argv:
            idx = argv.index("-q")
            # Only replace the arg AFTER -q when it is actually the prompt;
            # -q at the end (or followed by another flag) appends instead of
            # crashing with IndexError or clobbering the flag.
            if idx + 1 < len(argv) and not argv[idx + 1].startswith("-"):
                argv[idx + 1] = prompt
            else:
                argv.append(prompt)
        else:
            argv.append(prompt)
        return argv

    # Resolve the Hermes CLI. The binary is named `hermes-agent` (the `hermes`
    # entry-point is absent on some installs and may not be on the backend's
    # PATH), so try both names on PATH, then known locations under the INSTALL
    # ROOT — not HERMES_HOME, which points at the profile dir under
    # `serve --profile <name>` and would never contain hermes-agent/.
    exe = shutil.which("hermes") or shutil.which("hermes-agent")
    if not exe:
        root = Path(HERMES_HOME)
        for _ in range(4):  # walk up to the ancestor that holds hermes-agent/
            if (root / "hermes-agent").is_dir():
                break
            root = root.parent
        candidates = [
            root / "hermes-agent" / "venv" / "Scripts" / "hermes.exe",
            root / "hermes-agent" / "venv" / "Scripts" / "hermes-agent.exe",
            root / "hermes-agent" / "venv" / "bin" / "hermes",
            root / "hermes-agent" / "venv" / "bin" / "hermes-agent",
            root / "bin" / "hermes",
            root / "bin" / "hermes-agent",
        ]
        exe = next((str(c) for c in candidates if Path(c).exists()), None)
    if not exe:
        raise RuntimeError("hermes CLI not found; set ABYSS_AGENT_CMD to the agent command")
    # --max-turns 300: dispatched agents routinely die at the default 60-turn
    # cap before finishing multi-fix apply runs (observed: "2/8 applied" twice
    # because the agent hit "Reached maximum iterations (60)"). The prompts
    # time-box investigation, so a larger budget cannot spin forever.
    return [exe, "chat", "-q", prompt, "-s", "abyss-doctor", "-Q", "--max-turns", "300"]


def _prune_resolutions(retention_days: int = 30, keep_recent: int = 20, safety_hours: int = 1) -> dict:
    """Hygiene: bound the resolutions dir.

    Deletes resolution ``.json``/``.log`` pairs older than ``retention_days``,
    keeping at least the newest ``keep_recent`` per kind (doctor-/signal-/
    incident-/other-). Never touches files modified within ``safety_hours``
    (an active run is being written) or reports still in an
    in_progress/running state (the UI/worker polls those).
    """
    from __init__ import _RESOLUTION_DIR

    deleted = 0
    if not _RESOLUTION_DIR.exists():
        return {"deleted": 0}
    cutoff = time.time() - retention_days * 86400
    safety = time.time() - safety_hours * 3600
    by_kind: dict = {}
    for f in _RESOLUTION_DIR.glob("*.json"):
        kind = f.stem.split("-")[0] if "-" in f.stem else "other"
        by_kind.setdefault(kind, []).append(f)
    for kind, files in by_kind.items():
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for i, f in enumerate(files):
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            if mtime >= cutoff or mtime > safety or i < keep_recent:
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("status") in ("in_progress", "running"):
                    continue
            except Exception:
                pass
            for ext in (".json", ".log"):
                try:
                    (f.parent / (f.stem + ext)).unlink()
                    deleted += 1
                except OSError:
                    pass
    return {"deleted": deleted}


def _spawn_agent(prompt: str, report_path: Path, role: str = "resolver") -> "subprocess.Popen":
    """Spawn the Abyss agent as an independent, background `hermes` process.

    The child inherits this process's HERMES_HOME / HERMES_PROFILE_HOME so it
    runs as the same profile (free Nous model) and loads the same skills. The
    report path is passed via ``ABYSS_REPORT_PATH`` and stdout/stderr are
    captured to a sibling ``.log`` file (no console window on Windows).

    Every spawned child is registered in ``_AGENT_PROCS`` and terminated by an
    atexit hook if this process exits while the child is still alive — a
    dispatched agent must NEVER outlive its backend (stray agents hold file
    handles on the install and block Hermes self-updates).
    """
    from __init__ import HERMES_HOME, PROFILE_HOME

    cmd = _resolve_agent_cmd(prompt)
    env = os.environ.copy()
    env["HERMES_HOME"] = str(HERMES_HOME)
    env["HERMES_PROFILE_HOME"] = str(PROFILE_HOME)
    env["ABYSS_REPORT_PATH"] = str(report_path)
    env["ABYSS_AGENT_ROLE"] = role
    env["PYTHONUNBUFFERED"] = "1"
    log_path = report_path.with_suffix(".log")
    logf = open(log_path, "wb", buffering=0)
    kwargs = {
        "stdout": logf,
        "stderr": subprocess.STDOUT,
        "env": env,
        "cwd": str(PROFILE_HOME),
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    _prune_agent_procs()  # drop finished children first (never grows unbounded)
    try:
        _prune_resolutions()  # lazy hygiene: bound the resolutions dir on activity
    except Exception:
        pass
    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except Exception:
        # Never leak the open .log handle (it holds a file lock on Windows).
        try:
            logf.close()
        except Exception:
            pass
        raise
    _AGENT_PROCS.add(proc)
    return proc


# Registry of live dispatched agents + cleanup on process exit. A child that
# outlives the backend (backend killed, test run aborted) lingers holding file
# handles on the install — the Hermes updater then refuses to run ("another
# Hermes process is using this installation"). Kill survivors on exit.
# Finished children are PRUNED on every spawn so the registry (and each
# child's open .log stream) cannot accumulate for the backend's lifetime.
_AGENT_PROCS: set = set()


def _prune_agent_procs() -> None:
    for proc in list(_AGENT_PROCS):
        if proc.poll() is not None:  # finished — release its .log stream handle
            try:
                if getattr(proc, "stdout", None) is not None:
                    proc.stdout.close()
            except Exception:
                pass
            _AGENT_PROCS.discard(proc)


def _cleanup_agent_procs() -> None:
    for proc in list(_AGENT_PROCS):
        if proc.poll() is None:  # still running
            try:
                proc.terminate()
            except Exception:
                pass
    _AGENT_PROCS.clear()


_atexit.register(_cleanup_agent_procs)


def _read_report_file(report_path: Path) -> Optional[dict]:
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _write_report_file(report_path: Path, report: dict) -> None:
    try:
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.error("Abyss: failed to write report %s: %s", report_path, exc)


def _mark_resolution(kind: str, obj_id: int, status: str, note: str = "") -> None:
    """Record the outcome of an agent resolution run on a signal/incident.

    ``status == "succeeded"`` also resolves the object (and, for incidents,
    every linked signal); ``failed`` leaves it open so the user can retry.
    """
    from __init__ import _get_activity_conn, _init_db

    _init_db()
    now = datetime.now().isoformat()
    note = (note or "")[:2000]
    conn = _get_activity_conn()
    try:
        if kind == "signals":
            conn.execute(
                """UPDATE signals SET resolution_status = ?, resolution_note = ?,
                       resolution_finished_at = ?, resolved = ?, acknowledged = 1, resolved_at = ?
                   WHERE id = ?""",
                (status, note, now, 1 if status == "succeeded" else 0,
                 now if status == "succeeded" else None, obj_id),
            )
        else:
            conn.execute(
                """UPDATE incidents SET resolution_status = ?, resolution_note = ?,
                       resolution_finished_at = ?, status = ?, resolved_at = ?
                   WHERE id = ?""",
                (status, note, now,
                 "resolved" if status == "succeeded" else "open",
                 now if status == "succeeded" else None, obj_id),
            )
            if status == "succeeded":
                conn.execute(
                    "UPDATE signals SET resolved = 1, acknowledged = 1, resolved_at = ? WHERE incident_id = ?",
                    (now, obj_id),
                )
        conn.commit()
    except sqlite3.Error as exc:
        logger.error("Failed to mark resolution %s %s: %s", kind, obj_id, exc)
    finally:
        conn.close()


def _resolution_context(kind: str, row: dict) -> dict:
    """Build the evidence context handed to the resolver agent."""
    from __init__ import _get_activity_conn, _init_db

    _init_db()
    conn = _get_activity_conn()
    try:
        ctx = {"kind": kind, "id": row["id"]}
        if kind == "signals":
            ctx["signal"] = _redact(dict(row))
            linked = None
            if row.get("activity_id"):
                linked = conn.execute("SELECT * FROM activity WHERE id = ?", (row["activity_id"],)).fetchone()
            ctx["linked_activity"] = _redact(dict(linked)) if linked else None
            errs = conn.execute(
                """SELECT id, timestamp, action, tool_name, status, metadata
                   FROM activity WHERE session_id = ? AND status = 'error'
                   ORDER BY timestamp DESC LIMIT 8""",
                (row.get("session_id") or "",),
            ).fetchall()
            ctx["session_errors"] = [_redact(dict(r)) for r in errs]
        else:
            ctx["incident"] = _redact(dict(row))
            sig_ids = []
            try:
                sig_ids = json.loads(row.get("signal_ids") or "[]")
            except (ValueError, TypeError):
                sig_ids = []
            sigs = []
            if sig_ids:
                placeholders = ",".join("?" for _ in sig_ids)
                sigs = conn.execute(
                    f"SELECT * FROM signals WHERE id IN ({placeholders}) ORDER BY timestamp DESC LIMIT 30",
                    sig_ids,
                ).fetchall()
            ctx["linked_signals"] = [_redact(dict(s)) for s in sigs]
            sessions = []
            for s in sigs[:10]:
                sid = s["session_id"]
                if sid and sid not in sessions:
                    sessions.append(sid)
            errs = []
            for sid in sessions[:3]:
                errs.extend(conn.execute(
                    """SELECT id, timestamp, action, tool_name, status, metadata
                       FROM activity WHERE session_id = ? AND status = 'error'
                       ORDER BY timestamp DESC LIMIT 5""",
                    (sid,),
                ).fetchall())
            ctx["session_errors"] = [_redact(dict(r)) for r in errs[:15]]
        return ctx
    finally:
        conn.close()


def _build_resolver_prompt(kind: str, row: dict, context: dict, report_path: Path) -> str:
    ctx_json = json.dumps(context, indent=2)[:20000]
    kind_label = "signal" if kind == "signals" else "incident"
    return (
        "You are the Abyss resolver: an autonomous Hermes agent that diagnoses and FIXES "
        "agent-observability issues detected by the Abyss plugin.\n\n"
        "Load the 'abyss-doctor' skill and follow it exactly.\n\n"
        f"OBSERVED {kind_label.upper()} CONTEXT (JSON):\n{ctx_json}\n\n"
        "TASK:\n"
        "1. Diagnose the root cause from the context AND the live system. You have full "
        "tool access: read files, inspect HERMES_HOME logs, check processes, edit configs "
        "and plugin code.\n"
        "2. Actually FIX the root cause on the backend — do not only describe it.\n"
        "3. If the fix is reusable, save a skill named abyss-fix-<pattern> documenting it.\n"
        "4. Write your report to ABYSS_REPORT_PATH as JSON with this exact schema:\n"
        '{"schema":"abyss-resolution/1","role":"resolver","report_id":"' + report_path.stem + '",'
        '"status":"succeeded|failed","summary":"one-line summary",'
        '"findings":[{"title":"...","detail":"...","evidence":"..."}],'
        '"actions_taken":["..."],"skills_saved":["..."],"error":null}\n'
        "5. Your final chat response must be the one-line summary of what you fixed.\n"
        "6. TIME-BOX: spend at most ~8 tool actions on investigation. If you cannot "
        "reach a verified fix, write a PARTIAL report with status 'failed' and your "
        "findings so far — do not keep investigating forever.\n"
        "Do NOT mark anything resolved yourself — the backend does that from your report."
    )


def _resolution_finalize(kind: str, obj_id: int, report_path: Path, report: Optional[dict]) -> None:
    from __init__ import _add_activity

    ok = bool(report and report.get("status") == "succeeded")
    note = (report or {}).get("summary") or (report or {}).get("error") or "agent completed"
    _mark_resolution(kind, obj_id, "succeeded" if ok else "failed", note)
    _add_activity(
        action="resolution_completed" if ok else "resolution_failed",
        description=f"Agent {'resolved' if ok else 'failed to resolve'} {kind[:-1]} {obj_id}: {note[:120]}",
        category="system",
        status="completed" if ok else "error",
        metadata={"kind": kind, "obj_id": obj_id, "note": note[:500], "report": str(report_path)},
    )


def _resolution_worker(kind: str, obj_id: int, report_path: Path, prompt: str) -> None:
    """Background thread: run the resolver agent, then finalize from its report."""
    from __init__ import _AGENT_DEFAULT_TIMEOUT

    timeout_s = _AGENT_DEFAULT_TIMEOUT
    try:
        proc = _spawn_agent(prompt, report_path, role="resolver")
    except Exception as exc:
        logger.error("Abyss resolver spawn failed: %s", exc)
        _mark_resolution(kind, obj_id, "failed", f"agent spawn failed: {exc}")
        return
    deadline = time.time() + timeout_s
    report = None
    while time.time() < deadline:
        if proc.poll() is not None:
            report = _read_report_file(report_path)
            if report is not None:
                break
            try:
                log_tail = report_path.with_suffix(".log").read_text(encoding="utf-8", errors="replace")[-1500:]
            except Exception:
                log_tail = ""
            report = {
                "schema": "abyss-resolution/1", "role": "resolver", "status": "failed",
                "summary": f"agent exited without report (code {proc.returncode})",
                "error": log_tail[-500:],
            }
            break
        report = _read_report_file(report_path)
        # Only a TERMINAL report ends the wait while the agent still runs —
        # an incremental status:"running" write must not finalize the run
        # early (same contract as the doctor apply worker).
        if report is not None and report.get("status") in ("succeeded", "failed"):
            break
        time.sleep(3)
    if report is None or report.get("status") not in ("succeeded", "failed"):
        try:
            proc.terminate()
        except Exception:
            pass
        report = {
            "schema": "abyss-resolution/1", "role": "resolver", "status": "failed",
            "summary": f"agent timed out after {timeout_s // 60} min",
            "error": None,
        }
    _resolution_finalize(kind, obj_id, report_path, report)


def _dispatch_resolution(kind: str, obj_id: int) -> dict:
    """Dispatch a free-Nous Hermes agent to diagnose + fix a signal/incident."""
    from __init__ import _AGENT_DEFAULT_TIMEOUT, _RESOLUTION_DIR, _add_activity, _get_activity_conn, _init_db

    _init_db()
    conn = _get_activity_conn()
    try:
        if kind == "signals":
            row = conn.execute("SELECT * FROM signals WHERE id = ?", (obj_id,)).fetchone()
        else:
            row = conn.execute("SELECT * FROM incidents WHERE id = ?", (obj_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return {"error": f"{kind[:-1]} {obj_id} not found", "code": 404}
    row = dict(row)
    if row.get("resolution_status") == "running":
        # A run left "running" by a crashed/restarted backend (its worker
        # thread died with the old process) would block re-dispatch forever —
        # the UI's resolve button would hang. Treat runs started longer than
        # the agent timeout (+2 min margin) as stale and allow a fresh run.
        started = row.get("resolution_started_at")
        stale = True
        if started:
            try:
                stale = (time.time() - datetime.fromisoformat(started).timestamp()) > (_AGENT_DEFAULT_TIMEOUT + 120)
            except (ValueError, TypeError):
                stale = True  # unparseable start = orphaned row
        if not stale:
            return {"status": "already_running", "kind": kind, "id": obj_id}

    report_id = f"{kind[:-1]}-{obj_id}-{int(time.time())}"
    report_path = _RESOLUTION_DIR / f"{report_id}.json"
    prompt = _build_resolver_prompt(kind, row, _resolution_context(kind, row), report_path)

    now = datetime.now().isoformat()
    _init_db()
    conn = _get_activity_conn()
    try:
        if kind == "signals":
            conn.execute(
                """UPDATE signals SET resolution_status = 'running', resolution_started_at = ?,
                       resolution_finished_at = NULL, resolution_note = NULL WHERE id = ?""",
                (now, obj_id),
            )
        else:
            conn.execute(
                """UPDATE incidents SET resolution_status = 'running', resolution_started_at = ?,
                       resolution_finished_at = NULL, resolution_note = NULL WHERE id = ?""",
                (now, obj_id),
            )
        conn.commit()
    finally:
        conn.close()
    _add_activity(
        action="resolution_dispatched",
        description=f"Agent dispatched to resolve {kind[:-1]} {obj_id}",
        category="system",
        metadata={"kind": kind, "obj_id": obj_id, "report_id": report_id},
    )
    threading.Thread(target=_resolution_worker, args=(kind, obj_id, report_path, prompt), daemon=True).start()
    return {"status": "dispatched", "kind": kind, "id": obj_id, "report_id": report_id}
