# Abyss — Hermes Agent Observability Plugin

Local-first observability for Hermes AI agents. Abyss watches your agents from
the inside — recording tool calls, LLM interactions, and session lifecycle
events into SQLite — then renders them in a desktop dashboard with an activity
feed, calendar, global search, session traces, an interactive "Hermes Brain"
graph, and a signals & incidents console that surfaces silent agent failures
before they bite.

Everything stays on your machine. Abyss is an instrument, not a data exporter:
no external services, no telemetry leaving the host.

## Features

Six views in the desktop app:

- **Activity feed** — every tool call, LLM interaction, and session event, with
  category/status badges and session drill-down.
- **Calendar** — a month grid of agent activity so you can see when work
  happened.
- **Global search** — full-text search across activity, memories, and sessions,
  with relevance ranking.
- **Trace timeline** — per-session event timelines: tool calls, model calls,
  streams, subagents, approvals, and their outcomes.
- **Hermes Brain graph** — a canvas force-layout graph of the agent's
  memory/tool connections (DitherKit layout, Atkinson-dithered background),
  with drag, hover, and zoom.
- **Signals & incidents** — automatic detection of silent failures (errors,
  timeouts, rate limits, loops, vague replies), clustered into incidents by
  root cause, with acknowledge / resolve / reopen / close triage.

Plus:

- **Wave telemetry** (v2.0.0) — plugin-interface expansion observability:
  event bus activity, streaming hooks, API-call telemetry, subagents,
  approvals, platform events, commands, and skill lifecycles.
- **Health score, trends, and failure taxonomy** — at-a-glance answers to
  "are my agents OK right now?".
- **Doctor agent** — dispatch a built-in agent to diagnose the whole system
  and propose fixes.
- **Slash commands** — query everything from the terminal (`/abyss ...`).
- **Webhook alerting** — optional `ABYSS_WEBHOOK_URL` for out-of-band
  notifications.
- **Privacy by default** — secret-looking values (tokens, passwords, API keys)
  are redacted from logs, signals, and wave payloads.

## Install

Requirements: a Hermes Agent install (desktop app + plugin runtime), Python 3.10+
with `fastapi` and `httpx`.

1. Copy the backend into your Hermes plugin directory:

   ```sh
   # plugin root (backend)
   cp __init__.py abyss_wave.py plugin.yaml manifest.json manifest.yaml \
      $HERMES_HOME/plugins/abyss/
   # dashboard API mount
   mkdir -p $HERMES_HOME/plugins/abyss/dashboard
   cp dashboard/* $HERMES_HOME/plugins/abyss/dashboard/
   ```

2. Copy the desktop UI:

   ```sh
   mkdir -p $HERMES_HOME/desktop-plugins/abyss
   cp desktop/plugin.js $HERMES_HOME/desktop-plugins/abyss/plugin.js
   ```

   (`$HERMES_HOME` is `~/.hermes` on macOS/Linux and
   `%LOCALAPPDATA%\hermes` on Windows.)

3. Install Python dependencies:

   ```sh
   pip install -r requirements.txt
   ```

4. Restart the Hermes desktop app. Abyss appears as a right-sidebar pane, a
   full-page route at `/abyss`, a sidebar nav entry, a command-palette entry,
   and a status-bar chip with the live signal count.

Optional configuration (environment variables):

| Variable | Purpose | Default |
| --- | --- | --- |
| `ABYSS_DB_PATH` | SQLite database location | `~/.hermes/abyss.db` |
| `ABYSS_RETENTION_DAYS` | Days of history to keep | `30` |
| `ABYSS_WEBHOOK_URL` | Webhook endpoint for alerts | unset (off) |
| `ABYSS_AGENT_CMD` | Agent command used by doctor/resolve-agent | `hermes` |

## REST API

The backend mounts at `/api/plugins/abyss/*` inside the Hermes desktop app.

| Method | Path | Description |
| --- | --- | --- |
| GET | `/activity` | List activity entries (`?limit=&category=&session=`) |
| POST | `/activity` | Record an activity entry |
| GET | `/calendar` | Month-grid activity summary (`?year=&month=`) |
| GET | `/search` | Global search across activity/memories/sessions (`?q=`) |
| GET | `/stats` | Summary statistics |
| GET | `/health` | Agent health score 0–100 |
| GET | `/trends` | Activity/error/signal trends (`?days=&bucket=`) |
| GET | `/failures` | Root-cause failure taxonomy |
| GET | `/export` | Full JSON export of all data |
| GET | `/trace` | Session trace timeline (`?session=`) |
| GET | `/graph` | Brain graph nodes/edges |
| GET | `/signals` | Detected signals (`?limit=`) |
| GET | `/incidents` | Clustered incidents (`?status=`) |
| POST | `/signals/self-diagnostic` | Record a self-diagnostic signal |
| POST | `/incidents/cluster` | Run incident clustering |
| POST | `/signals/{id}/acknowledge` | Acknowledge a signal |
| POST | `/signals/{id}/resolve` | Resolve a signal |
| POST | `/signals/{id}/resolve-agent` | Dispatch an agent to diagnose + fix a signal |
| POST | `/incidents/{id}/acknowledge` | Acknowledge an incident |
| POST | `/incidents/{id}/resolve` | Resolve an incident |
| POST | `/incidents/{id}/reopen` | Reopen an incident |
| POST | `/incidents/{id}/close` | Close an incident |
| POST | `/incidents/{id}/resolve-agent` | Dispatch an agent to diagnose + fix an incident |
| POST | `/prune` | Delete data older than N days (`?days=`) |
| POST | `/doctor/run` | Run the doctor agent diagnosis |
| GET | `/doctor/report` | Latest doctor report |
| GET | `/doctor/last` | Last doctor run summary |
| POST | `/doctor/approve` | Approve proposed fixes |
| POST | `/benchmark/run` | Run the self-benchmark |
| GET | `/wave/events` | Event-bus telemetry |
| GET | `/wave/streams` | Streaming-hook telemetry |
| GET | `/wave/api` | API-call telemetry |
| GET | `/wave/subagents` | Subagent lifecycle telemetry |
| GET | `/wave/approvals` | Approval-flow telemetry |
| GET | `/wave/commands` | Command telemetry |
| GET | `/wave/platform` | Platform-event telemetry |
| GET | `/wave/skills` | Skill-lifecycle telemetry |
| GET | `/wave/summary` | Wave expansion summary |
| POST | `/wave/emit` | Emit a wave event |

## Slash commands

`/abyss` exposes the same data in the terminal:

```
/abyss recent [N]                 Last N activity entries (default 10)
/abyss stats                      Summary statistics
/abyss health                     Agent health score (0-100)
/abyss trends [days] [hour|day]   Activity/error/signal trends
/abyss failures [limit]           Root-cause failure taxonomy
/abyss search <query>             Search activity, memories, sessions
/abyss trace <session>            Trace timeline for a session
/abyss signals [--session=<sid>]  Detected signals
/abyss incidents [--status=<st>]  Incidents
/abyss ack <signal_id>            Acknowledge a signal
/abyss resolve <signal_id>        Resolve a signal
/abyss resolve-agent <id>         Dispatch an agent to diagnose + fix
/abyss doctor                     Full doctor diagnosis
/abyss incident <id> <action>     Acknowledge/resolve/reopen/close an incident
/abyss diagnostic <cap> <gap>     Record a self-diagnostic signal
/abyss webhook [url|off]          Show/set webhook alerting
/abyss export                     Show data volume
/abyss prune [days]               Delete data older than N days (default 30)
/abyss clean                      Clear all data (irreversible)
/abyss wave [surface]             Wave telemetry: events|streams|api|subagents|
                                  approvals|commands|platform|skills|summary
```

## Architecture

```
plugin.js (desktop, single-file React, Blob-URL loaded)
    │  ctx.rest('/api/plugins/abyss/...')
    ▼
__init__.py (FastAPI-style dispatcher + SQLite store + hook handlers)
    │  from abyss_wave import ...
    ▼
abyss_wave.py (wave expansion: event bus, streams, API telemetry,
               subagents, approvals, platform, commands, skills)
```

- The backend is a flat module pair (`__init__.py` + `abyss_wave.py`); the
  dashboard sub-API lives in `dashboard/plugin_api.py` as a FastAPI router.
- Storage is SQLite (activity, signals, incidents, traces, wave telemetry).
- Hooks (`pre_tool_call`, `post_llm_call`, `on_session_start`, … — 21 total)
  record lifecycle events without modifying agent code.
- The desktop UI is a single self-contained ESM file that imports only
  `@hermes/plugin-sdk`, `react`, and `react/jsx-runtime`; it loads via Blob
  URL, so there is no build step and no relative imports.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for the test workflow. Quick start:

```sh
pip install -r requirements.txt
python test_plugin.py          # core backend suite
python test_wave.py            # wave expansion suite
python dashboard/test_api.py   # dashboard API suite
node --check desktop/plugin.js # frontend syntax
```

Note: the tests are script-style (run `python test_*.py`, not `pytest`).

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 leviathofnoesia.

## Changelog

See [CHANGELOG.md](CHANGELOG.md).
