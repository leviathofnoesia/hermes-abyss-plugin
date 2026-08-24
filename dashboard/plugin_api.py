"""Abyss dashboard plugin — backend API routes.

Mounted at /api/plugins/abyss/ by the Hermes dashboard plugin system.

This layer is intentionally thin: every handler delegates to the plugin's
core module (plugins/abyss/__init__.py -> handle_request), which owns the
SQLite storage, hook handlers, signal classifiers, and incident clustering.

Endpoints:
  GET  /activity                List activity entries (limit, category, since, session_id)
  POST /activity                Add an activity entry
  GET  /calendar                Scheduled cron tasks + activity in range
  GET  /search                  Global search (q, limit)
  GET  /stats                   Dashboard summary
  GET  /trace                   Session trace or session list (session_id, limit)
  GET  /trace/graph             Graph-node (DAG) view of a session trajectory
  GET  /trace/timeline          Per-lane timeline of one session
  GET  /trace/agents            Overview: every agent (session) as its own lane
  GET  /graph                   Brain graph node/edge data (limit)
  GET  /health                  Agent health score (0-100)
  GET  /trends                  Bucketed activity/error/signal/incident trends
  GET  /failures                Root-cause failure taxonomy (type/tool/message)
  GET  /export                  Full JSON snapshot of all Abyss tables
  GET  /status                  Lightweight status for the statusbar chip (incl. last_activity_at/last_signal_at/last_error_at liveness timestamps)
  GET  /signals                 Detected signals (session_id, limit, type, severity,
                                  state=all|open|unack; rows enriched with
                                  tool_name/tool_action via activity join)
  GET  /incidents               Clustered incidents (status, limit, severity, open)
  POST /signals/self-diagnostic Record an agent self-diagnostic
  POST /incidents/cluster       Run incident clustering
  POST /prune                   Delete data older than N days (days)
  POST /signals/{id}/acknowledge   Acknowledge a signal
  POST /signals/{id}/resolve       Resolve a signal
  POST /signals/{id}/resolve-agent Dispatch a free-Nous agent to diagnose + fix a signal
  POST /incidents/{id}/acknowledge Acknowledge an incident
  POST /incidents/{id}/resolve     Resolve an incident
  POST /incidents/{id}/reopen      Reopen an incident
  POST /incidents/{id}/close       Close an incident
  POST /incidents/{id}/resolve-agent Dispatch a free-Nous agent to diagnose + fix an incident
  POST /doctor/run                 Dispatch the doctor agent (full diagnosis)
  GET  /doctor/report              Poll the doctor report (report_id)
  GET  /doctor/log                 Stream the live tail of the doctor agent's log (report_id)
  GET  /doctor/last                Return the most recent completed doctor report
  POST /doctor/approve             Approve + apply the doctor's proposed fixes
  POST /benchmark/run              Run the Abyss Bench Layer 1 probe suite
  POST /prune-resolutions          Hygiene: delete old resolution artifacts
  GET  /wave/events|streams|api|subagents|approvals|commands|platform|skills|summary
  POST /wave/emit                  Publish an event on the abyss: event bus
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request

logger = logging.getLogger("hermes.plugins.abyss.api")

# ---------------------------------------------------------------------------
# Import the plugin core. The dashboard importer loads THIS file standalone
# (spec_from_file_location), so we must locate the plugin package manually.
# ---------------------------------------------------------------------------
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent

if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

try:
    from __init__ import handle_request  # type: ignore
    _CORE_OK = True
except Exception as exc:  # pragma: no cover - import guard
    _CORE_OK = False
    logger.error("Abyss: failed to import plugin core: %s", exc)


router = APIRouter()


async def _json_body(request: Request) -> Dict[str, Any]:
    """Read a JSON request body, tolerating empty/malformed input.

    ``request.body()`` is a coroutine in Starlette/FastAPI — it MUST be
    awaited, otherwise ``isinstance(raw, bytes)`` is always False and every
    POST body silently becomes ``{}``.

    Also tolerates a DOUBLE-ENCODED body: the desktop IPC layer always
    ``JSON.stringify``s the request body (electron fetchJson), so a client
    that pre-stringifies produces a JSON-encoded *string* here. Decode it
    again so handlers always receive a dict.
    """
    try:
        raw = await request.body()
        if isinstance(raw, (bytes, bytearray)) and raw:
            data = json.loads(raw.decode("utf-8") or "{}")
            if isinstance(data, str):
                data = json.loads(data)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _guard():
    """Return a 500 payload when the core module failed to import."""
    return {"error": "abyss core not loaded", "code": 500}


def _delegate(method: str, path: str, params: dict = None, body: str = None):
    """Call the core dispatcher, returning its dict (or an error payload)."""
    if not _CORE_OK:
        return _guard()
    return handle_request(method, path, params or {}, body)


# --- activity ---------------------------------------------------------------

@router.get("/activity")
async def get_activity(
    limit: int = 50,
    category: Optional[str] = None,
    since: Optional[str] = None,
    session_id: Optional[str] = None,
):
    return _delegate("GET", "/activity", {"limit": limit, "category": category, "since": since, "session_id": session_id})


@router.post("/activity")
async def post_activity(request: Request):
    data = await _json_body(request)
    return _delegate("POST", "/activity", body=json.dumps(data))


# --- calendar ---------------------------------------------------------------

@router.get("/calendar")
async def get_calendar(start: Optional[str] = None, end: Optional[str] = None):
    return _delegate("GET", "/calendar", {"start": start, "end": end})


# --- search -----------------------------------------------------------------

@router.get("/search")
async def search(q: str = "", limit: int = 20):
    return _delegate("GET", "/search", {"q": q, "limit": limit})


# --- stats ------------------------------------------------------------------

@router.get("/stats")
async def stats():
    return _delegate("GET", "/stats")


@router.get("/health")
async def health():
    return _delegate("GET", "/health")


@router.get("/trends")
async def trends(days: int = 7, bucket: str = "day"):
    return _delegate("GET", "/trends", {"days": days, "bucket": bucket})


@router.get("/failures")
async def failures(limit: int = 15):
    return _delegate("GET", "/failures", {"limit": limit})


@router.get("/performance")
async def performance(days: int = 7, limit: int = 20):
    return _delegate("GET", "/performance", {"days": days, "limit": limit})


@router.get("/export")
async def export_all():
    return _delegate("GET", "/export")


@router.get("/status")
async def status():
    return _delegate("GET", "/status")


# --- trace ------------------------------------------------------------------

@router.get("/trace")
async def trace(session_id: Optional[str] = None, limit: int = 200):
    return _delegate("GET", "/trace", {"session_id": session_id or "", "limit": limit})


@router.get("/trace/graph")
async def trace_graph(session_id: str = "", limit: int = 300):
    """Graph-node (DAG) view of a session trajectory — Raindrop-style."""
    return _delegate("GET", "/trace/graph", {"session_id": session_id, "limit": limit})


@router.get("/trace/timeline")
async def trace_timeline(session_id: str = "", limit: int = 300):
    """Per-lane timeline of one session (agent trajectory as a timeline)."""
    return _delegate("GET", "/trace/timeline", {"session_id": session_id, "limit": limit})


@router.get("/trace/agents")
async def trace_agents(limit: int = 60):
    """Overview: every agent (session) as its own timeline lane."""
    return _delegate("GET", "/trace/agents", {"limit": limit})


# --- graph ------------------------------------------------------------------

@router.get("/graph")
async def graph(limit: int = 200):
    return _delegate("GET", "/graph", {"limit": limit})


# --- signals & incidents ----------------------------------------------------

@router.get("/signals")
async def signals(
    session_id: Optional[str] = None,
    limit: int = 50,
    type: Optional[str] = None,
    severity: Optional[str] = None,
    state: Optional[str] = None,
):
    return _delegate("GET", "/signals", {
        "session_id": session_id or "", "limit": limit,
        "type": type or "", "severity": severity or "",
        "state": state or "",
    })


@router.post("/signals/self-diagnostic")
async def self_diagnostic(request: Request):
    data = await _json_body(request)
    return _delegate("POST", "/signals/self-diagnostic", body=json.dumps(data))


@router.post("/signals/resolve-bulk")
async def resolve_signals_bulk(request: Request):
    """Bulk-resolve stale signals (triage semantics) — see /signals/resolve-bulk."""
    data = await _json_body(request)
    return _delegate("POST", "/signals/resolve-bulk", body=json.dumps(data))


@router.post("/signals/{signal_id}/acknowledge")
async def acknowledge_signal(signal_id: int):
    return _delegate("POST", f"/signals/{signal_id}/acknowledge")


@router.post("/signals/{signal_id}/resolve")
async def resolve_signal(signal_id: int):
    return _delegate("POST", f"/signals/{signal_id}/resolve")


@router.post("/signals/{signal_id}/resolve-agent")
async def resolve_signal_agent(signal_id: int):
    """Dispatch a free-Nous Hermes agent to diagnose + fix this signal."""
    return _delegate("POST", f"/signals/{signal_id}/resolve-agent")


@router.post("/incidents/{incident_id}/resolve-agent")
async def resolve_incident_agent(incident_id: int):
    """Dispatch a free-Nous Hermes agent to diagnose + fix this incident."""
    return _delegate("POST", f"/incidents/{incident_id}/resolve-agent")


@router.get("/incidents")
async def incidents(
    status: Optional[str] = None,
    limit: int = 50,
    severity: Optional[str] = None,
    open: bool = False,
):
    return _delegate("GET", "/incidents", {
        "status": status or "", "limit": limit,
        "severity": severity or "", "open": open,
    })


@router.post("/incidents/cluster")
async def cluster_incidents():
    return _delegate("POST", "/incidents/cluster")


@router.post("/incidents/{incident_id}/acknowledge")
async def acknowledge_incident(incident_id: int):
    return _delegate("POST", f"/incidents/{incident_id}/acknowledge")


@router.post("/incidents/{incident_id}/resolve")
async def resolve_incident(incident_id: int):
    return _delegate("POST", f"/incidents/{incident_id}/resolve")


@router.post("/incidents/{incident_id}/reopen")
async def reopen_incident(incident_id: int):
    return _delegate("POST", f"/incidents/{incident_id}/reopen")


@router.post("/incidents/{incident_id}/close")
async def close_incident(incident_id: int):
    return _delegate("POST", f"/incidents/{incident_id}/close")


# --- doctor (agent-powered diagnosis + approval-gated fixes) ----------------

@router.post("/doctor/run")
async def doctor_run():
    """Dispatch the doctor agent: full overarching diagnosis + proposed fixes."""
    return _delegate("POST", "/doctor/run")


@router.get("/doctor/report")
async def doctor_report(report_id: str = ""):
    """Poll the doctor report: {status: running|ready, report?}."""
    return _delegate("GET", "/doctor/report", {"report_id": report_id})


@router.get("/doctor/last")
async def doctor_last():
    """Return the most recent completed doctor report (resume support)."""
    return _delegate("GET", "/doctor/last")


@router.get("/doctor/log")
async def doctor_log(report_id: str = ""):
    """Stream the live tail of the spawned doctor agent's stdout log."""
    return _delegate("GET", "/doctor/log", {"report_id": report_id})


@router.post("/benchmark/run")
async def benchmark_run():
    """Run the Abyss Bench Layer 1 probe suite (deterministic regression gate)."""
    return _delegate("POST", "/benchmark/run")


@router.post("/prune-resolutions")
async def prune_resolutions(request: Request):
    """Hygiene: delete old resolution artifacts (days, keep_recent)."""
    data = await _json_body(request)
    return _delegate("POST", "/prune-resolutions", body=json.dumps(data))


@router.post("/doctor/approve")
async def doctor_approve(request: Request):
    """Approve the doctor's proposed fixes — an apply agent executes them."""
    data = await _json_body(request)
    return _delegate("POST", "/doctor/approve", body=json.dumps(data))


# --- maintenance ------------------------------------------------------------

@router.post("/prune")
async def prune(request: Request):
    data = await _json_body(request)
    return _delegate("POST", "/prune", body=json.dumps(data))


# --- wave (August 2026 plugin-interface expansion) ---------------------------
# Event bus audit, streaming telemetry, API request telemetry, subagents,
# approvals, command usage, gateway platform events, skill lifecycle.

@router.get("/wave/events")
async def wave_events(limit: int = 50):
    return _delegate("GET", "/wave/events", {"limit": limit})


@router.get("/wave/streams")
async def wave_streams(limit: int = 50):
    return _delegate("GET", "/wave/streams", {"limit": limit})


@router.get("/wave/api")
async def wave_api(limit: int = 50):
    return _delegate("GET", "/wave/api", {"limit": limit})


@router.get("/wave/subagents")
async def wave_subagents(limit: int = 50):
    return _delegate("GET", "/wave/subagents", {"limit": limit})


@router.get("/wave/approvals")
async def wave_approvals(limit: int = 50):
    return _delegate("GET", "/wave/approvals", {"limit": limit})


@router.get("/wave/commands")
async def wave_commands(limit: int = 50):
    return _delegate("GET", "/wave/commands", {"limit": limit})


@router.get("/wave/platform")
async def wave_platform(limit: int = 50):
    return _delegate("GET", "/wave/platform", {"limit": limit})


@router.get("/wave/skills")
async def wave_skills(limit: int = 50):
    return _delegate("GET", "/wave/skills", {"limit": limit})


@router.get("/wave/summary")
async def wave_summary():
    return _delegate("GET", "/wave/summary")


@router.post("/wave/emit")
async def wave_emit(request: Request):
    """Debug/test surface: publish an event on the abyss: event bus."""
    data = await _json_body(request)
    return _delegate("POST", "/wave/emit", body=json.dumps(data))
