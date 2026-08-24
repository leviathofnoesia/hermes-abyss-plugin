# Abyss — Hermes Agent Observability Plugin

Local-first observability for Hermes AI agents. Abyss watches your agents from the inside — recording tool calls, LLM interactions, and session lifecycle events into SQLite — then renders them in a desktop dashboard with an activity feed, calendar, global search, session traces, an interactive "Hermes Brain" graph, and a signals & incidents console that surfaces silent agent failures before they bite.

Everything stays on your machine. Abyss is an instrument, not a data exporter: no external services, no telemetry leaving the host.

---

<!-- impeccable:readme-schema 1 -->

## What Abyss Does

Abyss hooks the actual agent lifecycle — pre/post tool calls, pre/post LLM calls, session boundaries, streaming, API requests, subagents, approvals, commands, platform events, and skill lifecycles — and answers one question every time you glance at it: **are my agents OK right now?**

It then gives you the tools to answer the next one: **what broke, and where?**

### Six Core Views

| View | What it shows | When you use it |
|------|---------------|----------------|
| **Activity feed** | Every tool call, LLM interaction, and session event, with category/status badges and session drill-down | Scanning what agents did today |
| **Calendar** | Month grid of agent activity — when work happened, one glance | Week-in-retro or spotting gaps |
| **Global search** | Full-text search across activity, memories, and sessions, with relevance ranking | Finding a specific run or decision |
| **Trace timeline** | Per-session event timelines: tool calls, model calls, streams, subagents, approvals, and their outcomes | Debriefing a single session |
| **Hermes Brain graph** | Canvas force-layout graph of the agent's memory/tool connections (DitherKit layout, Atkinson-dithered background), with drag, hover, and zoom | The signature view — see the agent's world |
| **Signals & incidents** | Automatic detection of silent failures (errors, timeouts, rate limits, loops, vague replies), clustered into incidents by root cause, with acknowledge/resolve/reopen/close triage | When something went wrong and you need the why |

Add to that:

- **Wave telemetry** (v2.0.0) — plugin-interface expansion observability: event bus activity, streaming hooks, API-call telemetry, subagents, approvals, platform events, commands, and skill lifecycles.
- **Health score, trends, and failure taxonomy** — at-a-glance answers to "are my agents OK right now?", with a 7-day trend and a breakdown of what's dragging the score.
- **Doctor agent** — dispatch a built-in agent to diagnose the whole system and propose fixes. It reads the actual stored state, not anecdotes.
- **Slash commands** — query everything from the terminal (`/abyss ...`).
- **Webhook alerting** — optional `ABYSS_WEBHOOK_URL` for out-of-band notifications to Slack/Discord/Teams-compatible endpoints.
- **Privacy by default** — secret-looking values (tokens, passwords, API keys) are redacted from logs, signals, and wave payloads.

### The Scoreboard, Translated

| Metric | What it means |
|--------|---------------|
| 8 views in the desktop app | activity feed, calendar, global search, trace timeline, Hermes Brain graph, signals & incidents |
| `+ sign...` (v2.0 wave) | 8 new wave surfaces: events, streams, API calls, subagents, approvals, commands, platform, skills |
| 21 hooks captured | pre/post tool call, pre/post LLM call, session boundaries, streaming, API, subagents, approvals, commands, platform, skills |
| `test_plugin.py §14` (14 asserts) | trace graph + timeline + agents-overview surfaces verified with 14 dedicated asserts |

---

## Dawn Patrol

This section is written to read like a real dev-note from a working system, not a marketing deck. It's the "what does it feel like to run this" section.

### The First Glance

Open Abyss and the first thing you see is a phosphor-terminal wordmark — `abyss` in bold tracked uppercase with a blinking block cursor — and a boot line:

```
$ ./abyss --observe --local --cloud-fix
```

Below that: a mono metric strip. `ACT`/`HLTH`/`INC`/`CRN`/`CAT`/`SIG` with tabular numerals, and a health dot that's either green (all clear) or carries the open-signal count. The glance connects to the action: `SIG` and `HLTH` are live jump-points into the watch and health views, and a trailing verdict label (`N critical ›`) jumps straight into watch.

No cards. No stat monument. It prints like an instrument.

### When Something Goes Wrong

A signal is detected — an error, a timeout, a rate limit, a loop, a vague reply — and it lands in the signals console. It carries a session id if it has one, and a `trace ›` button that drops you into the exact session trace. The incident gets clustered by root cause with other signals that share it, and triage is a row button: acknowledge, resolve, reopen, close. Nothing is ever a silent no-op; errors surface as recovery paths.

When you want the system to explain itself, run the doctor:

```
/abyss diagnostic
```

It reads the actual stored state — not anecdotes — and proposes fixes. Approve one and it applies.

### The Graph Is the Thing

The Hermes Brain graph is the signature view. It's not a decorative sidebar — it's the centerpiece. A canvas force-layout (DitherKit) on an Atkinson-dithered phosphor ground, with computed theme colors (canvas can't resolve `var()` strings, so `palette()` reads `getComputedStyle` at runtime — theme-safe in both dark and light). Nodes are draggable, hoverable, zoomable. Click selects, arrows move, Enter/Space drills into a session trace, Escape clears. A selected session node reveals a `trace ›` button in the header.

This is what makes Abyss memorable. Treat the graph as the soul, not an afterthought.

### Wave Telemetry

The v2.0 wave surfaces are: events, streams, API calls, subagents, approvals, commands, platform, skills. Each surface reports count + last occurrence, and the merged feed below is the hairline transcript of what the plugin itself did. Backend failure prints `listening for wave telemetry…` and an ErrorState with retry — never a silent no-op.

### Cloud Fix, Honestly Disclosed

Abyss is local-first. Observation stays on-machine. But agent-powered remediation — signal/incident resolve, doctor apply — can dispatch a free-Nous cloud agent. Every such action is labeled as cloud in the visible UI: the button says `resolve (cloud agent)`, the tooltip says so, and the masthead boot line carries `--cloud-fix`. No surprise, no hidden network call, no dressing up cloud work as local work.

---

## Install

Requirements: a Hermes Agent install (desktop app + plugin runtime), Python 3.10+ with `fastapi` and `httpx`.

1. Copy the backend into your Hermes plugin directory:

   ```sh
   # plugin root (backend)
   cp __init__.py abyss_wave.py plugin.yaml manifest.json manifest.json \
      $HERMES_HOME/plugins/abyss/
   # dashboard API mount
   mkdir -p $HERMES_HOME/plugins/abyss/dashboard
   cp dashboard/* $HERMES_HOME/plugins/abyss/dashboard/
   ```

   (`$HERMES_HOME` is `~/.hermes` on macOS/Linux and `%LOCALAPPDATA%\hermes` on Windows.)

2. Copy the desktop UI:

   ```sh
   mkdir -p $HERMES_HOME/desktop-plugins/abyss
   cp desktop/plugin.js $HERMES_HOME/desktop-plugins/abyss/plugin.js
   ```

3. Install Python dependencies:

   ```sh
   pip install -r requirements.txt
   ```

4. Restart the Hermes desktop app. Abyss appears as a right-sidebar pane, a full-page route at `/abyss`, a sidebar nav entry, a command-palette entry, and a status-bar chip showing live signal counts.

5. Verify the mount in `~/.hermes/logs/agent.log`:

   ```
   Mounted plugin API routes: /api/plugins/abyss/
   ```

Frontend plugins load automatically from `$HERMES_HOME/desktop-plugins/` — no config needed. If a frontend doesn't appear within a few seconds, hit `Ctrl+K` → **Reload desktop plugins**.

Backend installs require a serve-process restart (the desktop respawns `hermes serve`). Verify each mount in the agent log as above.

### What You Need Before You Install

- `fastapi` and `httpx` (see `requirements.txt`)
- The Hermes desktop app and plugin runtime running
- Python 3.10+

Abyss itself needs no API keys, no tokens, and no network access to function. The only external dependency that's optional is the webhook URL if you want out-of-band alerts.

---

## Quick Start

Once installed and the app has restarted:

1. **Open Abyss** — click the Abyss sidebar entry, or go to `/abyss`, or run `/abyss` from the command palette.

2. **Glance at the scoreboard** — the masthead boot line and the mono metric strip in the header. Green dot = all clear. Open-signal count = something to look at. Click `SIG` or `HLTH` to drill in.

3. **Scan the activity feed** — every tool call, LLM interaction, and session event. Category glyph, action title, relative time, badges, session id micro-label. Any row with a session id carries a `trace ›` button.

4. **Drop into the graph** — the Hermes Brain is the default view. Drag, hover, zoom. Click a node to select it, arrow keys to move, Enter to trace.

5. **Check signals** — the watch view surfaces detected signals and clustered incidents. Triage with the row buttons.

6. **Query from the terminal** — slash commands give you everything without leaving the keyboard:

   ```
   /abyss recent     # activity feed
   /abyss stats      # dashboard statistics
   /abyss search     # search
   /abyss trace      # trace view
   /abyss signals    # signals & incidents
   /abyss diagnostic # run the doctor
   /abyss clean      # prune old data
   ```

7. **Set a webhook (optional)** — put `ABYSS_WEBHOOK_URL` in your environment for out-of-band alerts to your Slack/Discord/Teams-compatible endpoint.

---

## Architecture

```
__init__.py            Backend core: activity store, signals/incidents,
                       doctor, slash commands, REST dispatcher (flat — the
                       test scripts import it as a sibling module).

abyss_wave.py          Wave expansion: event bus, streams, API telemetry,
                       subagents, approvals, platform, commands, skills.

dashboard/             FastAPI router mount for the desktop app.
  plugin_api.py        REST endpoint dispatcher — thin delegate to core
                       routes. Declares api path in manifest.yaml.

desktop/
  plugin.js            Single-file ESM React UI loaded as a Blob URL by the
                       Hermes desktop app. No build step.
```

### Plugin Manifest

Abyss is declared as a v2 plugin manifest (`plugin.yaml`):

```yaml
name: abyss
version: "2.0.0"
description: >-
  Abyss Dashboard — Raindrop-style observability for Hermes AI agents.
  Auto-records tool calls, LLM interactions, session events, streaming
  telemetry, API requests, subagents, approvals, commands, gateway platform
  events and skill lifecycle, with self-diagnostics, signal detection,
  incident clustering, triage, health scoring, trends, failure taxonomy,
  export, webhook alerting, and an inter-plugin event bus.
manifest_version: 2
api_version: 1
license: MIT
homepage: https://github.com/NousResearch/hermes-agent
tags: [observability, monitoring, dashboard, traces, signals, incidents, telemetry]
python_dependencies: []
requires_plugins: []
capabilities: []
emits:
  - wave_ready
  - signal_detected
  - alert_fired
  - incident_clustered
  - doctor_completed
listens: []
config_schema:
  retention_days:
    type: integer
    default: 30
    description: "Prune activity/signals/incidents older than N days on startup."
  webhook_url:
    type: string
    default: ""
    description: "Slack/Discord/Teams-compatible webhook for alert notifications."
  stream_signals:
    type: boolean
    default: true
    description: "Emit an empty_stream signal when a streamed response yields zero tokens."
  stream_signal_coalesce_minutes:
    type: integer
    default: 10
    description: "Coalesce repeated same-type wave signals (e.g. empty_stream) per session within N minutes into one signal with a repeat_count detail, instead of inserting one row per occurrence."
provides_hooks:
  - pre_tool_call
  - post_tool_call
  - pre_llm_call
  - post_llm_call
  - on_session_start
  - on_session_end
  - on_session_reset
  - on_session_finalize
  - on_stream_start
  - on_stream_delta
  - on_stream_end
  - pre_api_request
  - post_api_request
  - api_request_error
  - subagent_start
  - subagent_stop
  - pre_approval_request
  - post_approval_response
  - gateway_platform_event
  - pre_command
  - on_skill_lifecycle
commands:
  - name: abyss
    description: "Abyss observability: /abyss stats|health|trends|failures|performance|recent|search|trace|signals|incidents|ack|resolve|resolve-stale|diagnostic|webhook|prune|wave"
  - name: abyss.recent
    description: "Show recent activity feed"
    alias: "/activity"
  - name: abyss.stats
    description: "Show dashboard statistics"
    alias: "/a-stats"
  - name: abyss.search
    description: "Search activity/memories/sessions"
    alias: "/a-search"
  - name: abyss.trace
    description: "Show conversation trace timeline"
    alias: "/a-trace"
  - name: abyss.incidents
    description: "Show detected incidents and signals"
    alias: "/a-incidents"
  - name: abyss.wave
    description: "Aug-2026 wave surfaces: events|streams|api|subagents|approvals|commands|platform|skills|summary"
    alias: "/a-wave"
api:
  path: dashboard/plugin_api.py
  entry: ../desktop-plugins/abyss/plugin.js
```

The manifest declares 21 hooks, slash commands, and the `api` path pointing at `dashboard/plugin_api.py`. The v2 fields (`emits`, `listens`, `config_schema`, `capabilities`) are advisory + additive; Abyss declares no capabilities because every hook below is an observer (fail-closed consent default).

### How the Data Flows

1. **Hooks fire** — each agent lifecycle event passes through the Abyss hook registrations in `__init__.py`.

2. **Records are stored** — activity rows, signal detections, incident clusters, and wave events land in a local SQLite database. Secrets are redacted at record time.

3. **The UI reads them** — `plugin.js` calls `ctx.rest()` to hit the REST endpoints declared in `plugin_api.py`, which delegate to core routes in `__init__.py`. The UI is a single-file Blob-URL plugin — no relative imports, no build step, only `@hermes/plugin-sdk`, `react`, and `react/jsx-runtime` importable.

4. **The graph renders** — the Hermes Brain is a canvas force-layout on an Atkinson-dithered phosphor ground, with computed theme colors. Drag, hover, zoom, click/keyboard selection. The graph is the default surface.

5. **Signals become incidents** — detected failures are clustered by root cause, surfaced in the watch view with triage buttons. Agent-powered resolve can dispatch a free-Nous cloud agent — disclosed in the button label, tooltip, and masthead boot line.

---

## Views

### Activity Feed

Every tool call, LLM interaction, and session event, with:

- Category glyph `▸` in type color (session/tool/memory/category)
- Action title
- Relative time (tabular numerals)
- Category + status badges
- Session id micro-label
- `trace ›` button on any row with a session id

Rows keep `truncate`/`line-clamp-2`; nothing exceeds a comfortable width in the 420px pane. Loading prints a pulsing skeleton; empty state uses the SDK `EmptyState`; backend failure prints the terminal voice + ErrorState with retry.

### Calendar

A month grid of agent activity. 7-column layout via inline `gridTemplateColumns` (grid-cols-7 is dead in the compiled CSS). Day cells `min-height:72px` inline. Today ring via `ring-1 ring-(--ui-stroke-secondary)`. Task chips in type colors with status glyphs (✓ / ▶ / ○) so state is never color-only.

A chip whose task carries a `session_id` gets a `trace ›` drill — the affordance lights up when the backend includes the id in the calendar rows.

### Global Search

SDK `SearchField` with source toggles. Result rows carry:

- Source label in type color
- Relevance %
- Relative time
- 2-line clamp (title + preview)
- `trace ›` drill on session hits (the result id IS the session id)

### Trace Timeline

Two surfaces in the Trace tab, behind a `list / graph / timeline` segmented toggle:

**Graph (`/trace/graph` → `TraceGraphView`, canvas)** — pairs `tool_call` start/end events by `tool_call_id` into a single node carrying `status` (ok|error|running), `error_type`, `error_message`, and `duration_ms`. Groups each `llm_call` reasoning turn with the tool calls it spawned (`parent` → `spawn` edges) → a left-to-right DAG. Status → color: session=blue, reasoning=purple, ok=green, error=red, running/unknown=amber. Selected node highlights with the accent color and prints a detail row. Click hit-tests node rects from a layout ref. `ResizeObserver` re-sizes the canvas (DPR-aware).

**Timeline (`/trace/agents` + `/trace/timeline?session_id=…` → `TraceTimelineView`)** — every session renders as one horizontal bar on a shared time axis (green when healthy, red when it has failures, with an error tick). Clicking a lane selects that session. The detail view (`/trace/timeline`) splits one session into three horizontal lanes — Reasoning / Tools / Failures — each event positioned by its start offset and sized by its duration; failures glow red. Hover reveals `label · status · duration`.

Errors are counted from `event_data LIKE "status:error"` (both spacing variants), never from the literal `error_type` key — which is present even when null and previously inflated counts.

### Hermes Brain Graph

The signature view. Canvas force-layout (DitherKit) on an Atkinson-dithered phosphor ground, with:

- Computed theme colors via `getComputedStyle` (canvas cannot resolve `var()` strings)
- Soft hover/selection glow in the accent color
- Drag, hover, zoom
- Click to select (empty ground clears), arrow keys to move, Enter/Space to trace a session node, Escape to clear
- Focusable canvas (`tabIndex 0`) with aria-label announcing the selection
- `trace ›` header button on a selected session node
- rAF-throttled resize recompute so pane drags coalesce to one layout pass per frame
- Footer hint printing the keyboard map (`click: select · arrows: move · enter: trace`)

The graph is the default surface, the first tab, and the centerpiece. Treat it as the soul.

### Signals & Incidents

Automatic detection of silent failures:

| Signal type | When it fires |
|-------------|---------------|
| error | tool/LLM/API call reported an error |
| timeout | a call took too long |
| rate_limit | an API call hit a rate limit |
| loop | 3+ identical tool calls in the same session |
| empty_stream | a streamed response yielded zero tokens |
| vague_reply | a reply with no concrete content |
| self_diagnostic | doctor or self-check recorded a finding |

Signals are clustered into incidents by root cause. Triage is wired to:

```
/signals/{id}/acknowledge
/signals/{id}/resolve
/incidents/{id}/acknowledge
/incidents/{id}/resolve
/incidents/{id}/reopen
/incidents/{id}/close
```

Triage buttons are disabled while a mutation runs (busy state). Any signal or incident with a `session_id` carries a `trace ›` drill. Agent-powered resolve is labeled `resolve (cloud agent)` — it dispatches a free-Nous cloud agent, disclosed in the label, tooltip, and masthead boot line.

### Health (Report)

`$ abyss health` status line (score/100, level dot + word, counters) then hairline sections:

- Doctor flow
- Benchmark
- Score breakdown bars
- 7-day trend bars
- Failure taxonomy

No cards. No stat monument. It prints like a diagnostic report.

The score is computed from activity, signals, incidents, and the failure taxonomy. Backend failure prints `status link down` with a retry button — never a false "all clear".

### Wave Telemetry

`$ abyss wave` header, then a surface/count/last terminal table (8 surfaces: events, streams, API calls, subagents, approvals, commands, platform, skills — each with a tone dot + colored micro label + tabular count). Below: the merged hairline feed.

Loading prints `listening for wave telemetry…`. Backend failure uses ErrorState + retry.

### Status Bar Chip

`abyss` + health score (or open-signal count) with health dot. Navigates to `/abyss`. Fetches `/status` (not the old LLM-count).

---

## Design

Abyss is a phosphor-terminal instrument sitting in a midnight machine room. The design world is committed in `DESIGN.md` (the desktop plugin folder) — the reference for the committed look, feel, type, color, components, states, and motion.

The short version:

- **World:** phosphor-terminal / midnight machine room. Monospace type and tabular numerals throughout, near-black ground, a drifting scanline, a blinking block cursor. States print themselves (`$ ./abyss --observe --local --cloud-fix`) rather than appearing as badges.
- **Color:** host theme variables only — no hex/rgb literals. Status accents via inline `var(--ui-*)` so they exist in both dark and light themes.
- **Type:** host mono stack throughout; tabular numerals for every metric and timestamp; uppercase + tracking-widest for micro-labels; lowercase mono for terminal-voice labels.
- **Components:** masthead with blinking block cursor and boot line; mono metric strip (re-prints when health numbers change — a 0.5s opacity flash, ease-out); eight SDK tabs with Brain graph as the default surface; activity rows with `trace ›` drill; calendar with status glyphs; search with relevance ranking; trace graph and timeline; watch triage with busy state; health report printed like a diagnostic; wave telemetry table + merged feed.
- **Motion:** one authored moment (metric strip re-print flash) on top of ambient scanline drift and blinking cursor. Everything else uses host SDK transitions. `@media (prefers-reduced-motion: reduce)` disables animations.
- **States:** loading (skeletons + terminal voice), empty (SDK EmptyState), error (SDK ErrorState + retry), busy (buttons disabled), hover (rows and graph nodes glow).

The design is committed at full fidelity inside the host theme system. The incumbent generic-SDK look was replaced by the phosphor-terminal world.

---

## Productivity

Abyss isn't a toy. It's a working observability layer for a technical operator running Hermes Agent daily with multiple agent profiles.

### What makes it useful in practice

- **Silent failures surface before they bite** — errors, timeouts, rate limits, loops, and vague replies are detected and clustered. You see them in the watch view, not buried in a log you forgot to check.
- **Every symptom drills to the trace** — a signal, an incident, an activity row, a search hit, a brain node, a calendar chip — one or two clicks to the underlying session trace. No re-discovery.
- **Health score is honest** — it's computed from actual stored state, not a feel-good number. When the backend is down, it prints `status link down` with a retry button instead of a false "all clear".
- **The doctor reads the system** — dispatch it and it reads the actual stored state and proposes fixes. Approve one and it applies.
- **It stays local** — observation never leaves the machine. The only external call that can happen is agent-powered remediation (signal/incident resolve, doctor apply), and every such action is labeled as cloud in the visible UI.
- **Wave telemetry shows the plugin itself** — the 8 wave surfaces (events, streams, API calls, subagents, approvals, commands, platform, skills) are the plugin's own activity log. You see what Abyss itself did.

### Who It's For

A technical operator running Hermes Agent daily with multiple agent profiles. Someone who wants problems solved efficiently, values directness, and monitors agent behavior for silent failures. Someone who uses Abyss as a live instrument while agents work — checking health, debugging failed runs, and reviewing what the agent did — and as a post-mortem tool after runs.

---

## Slash Commands

Query everything from the terminal:

| Command | What it does |
|---------|--------------|
| `/abyss recent` | Activity feed (alias: `/activity`) |
| `/abyss stats` | Dashboard statistics (alias: `/a-stats`) |
| `/abyss search` | Search activity/memories/sessions (alias: `/a-search`) |
| `/abyss trace` | Trace view (alias: `/a-trace`) |
| `/abyss incidents` | Signals & incidents (alias: `/a-incidents`) |
| `/abyss wave` | Wave telemetry surfaces (alias: `/a-wave`) |
| `/abyss diagnostic` | Run the doctor |
| `/abyss clean` | Prune old data |
| `/abyss stats` | Dashboard statistics |
| `/abyss health` | Health score + report |
| `/abyss trends` | 7-day trends |
| `/abyss failures` | Failure taxonomy |

All slash commands are declared in the plugin manifest and wired into the Hermes command palette.

---

## API

Abyss exposes a REST API at `/api/plugins/abyss/` (mounted by the desktop app serve process). The API is a thin delegate in `dashboard/plugin_api.py` → core routes in `__init__.py`.

### Endpoints

| Endpoint | Method | What it does |
|----------|--------|--------------|
| `/activity` | GET | List activity (query params: limit, category, session_id) |
| `/activity` | POST | Add an activity row |
| `/calendar` | GET | Month grid of activity |
| `/search` | GET | Full-text search across activity/memories/sessions |
| `/stats` | GET | Dashboard statistics |
| `/status` | GET | Health status (score, open signals, backend alive) |
| `/health` | GET | Health score + report |
| `/trends` | GET | 7-day trends |
| `/failures` | GET | Failure taxonomy |
| `/trace` | GET | Trace timeline |
| `/graph` | GET | Hermes Brain graph data (nodes + edges) |
| `/signals` | GET | Detected signals |
| `/signals/{id}/acknowledge` | POST | Acknowledge a signal |
| `/signals/{id}/resolve` | POST | Resolve a signal |
| `/incidents` | GET | Clustered incidents |
| `/incidents/{id}/acknowledge` | POST | Acknowledge an incident |
| `/incidents/{id}/resolve` | POST | Resolve an incident |
| `/incidents/{id}/reopen` | POST | Reopen an incident |
| `/incidents/{id}/close` | POST | Close an incident |
| `/incidents/cluster` | POST | Re-cluster incidents |
| `/prune` | POST | Prune old data |
| `/doctor/run` | POST | Run the doctor |
| `/doctor/approve` | POST | Approve a doctor proposal |
| `/doctor/report` | GET | Doctor report |
| `/doctor/last` | GET | Last doctor run |
| `/benchmark/run` | POST | Run the benchmark |
| `/wave/*` | GET | Wave telemetry surfaces |
| `/resolve-agent` | POST | Dispatch agent-powered resolve |

Triage mutations (acknowledge/resolve/reopen/close) are wired with busy-state row buttons in the UI. Errors surface as recovery paths, never as silent no-ops.

### Secret Redaction

Secret-looking values (tokens, passwords, API keys) are redacted from logs, signals, wave payloads, and stored activity. The redaction happens at record time in `_coerce_body` / `_mask_secrets`. The doctor-clean declaration requires every registered hook to be listed in `provides_hooks`, so the manifest and the hook registrations are kept in sync.

---

## Wave v2.0

The v2.0 wave surfaces are the plugin's own activity log — a view of what Abyss itself is doing:

| Surface | What it records |
|---------|-----------------|
| events | Event-bus activity (`wave_ready`, `signal_detected`, `alert_fired`, `incident_clustered`, `doctor_completed`) |
| streams | Streaming hooks (`on_stream_start`, `on_stream_delta`, `on_stream_end`) |
| api | API-request telemetry (`pre_api_request`, `post_api_request`, `api_request_error`) |
| subagents | Subagent lifecycle (`subagent_start`, `subagent_stop`) |
| approvals | Approval lifecycle (`pre_approval_request`, `post_approval_response`) |
| commands | Command execution (`pre_command`) |
| platform | Gateway platform events (`gateway_platform_event`) |
| skills | Skill lifecycle (`on_skill_lifecycle`) |

Each surface reports count + last occurrence, and the merged feed below the table is the hairline transcript of what the plugin itself did. The wave schema is declared in `plugin.yaml` under `emits` and the v2 `manifest_version: 2`.

---

## Doctor

The doctor is a built-in agent that diagnoses the whole system and proposes fixes. It reads the actual stored state — activity, signals, incidents, wave events, health score, failure taxonomy — not anecdotes.

### Running the Doctor

```
/abyss diagnostic
```

The doctor runs, reads the system state, and produces a report. You can approve one of its proposals and it applies.

### What the Doctor Looks At

- Activity patterns (repeated failures, loops, rate limits)
- Signal clusters and their root causes
- Incident trends over time
- Health score breakdown
- Failure taxonomy
- Wave telemetry anomalies

### Doctor Proposal Flow

1. Doctor runs and reads the system state
2. Produces a report with findings and proposed fixes
3. You review the proposals
4. Approve one and it applies

The doctor is not a magic fix-all. It's a diagnostic tool that reads the actual state and proposes targeted fixes. The report is the product.

---

## Hooks

Abyss registers 21 hooks into the Hermes agent lifecycle:

| Hook | When it fires |
|------|---------------|
| `pre_tool_call` | Before a tool call executes |
| `post_tool_call` | After a tool call completes |
| `pre_llm_call` | Before an LLM call |
| `post_llm_call` | After an LLM call completes |
| `on_session_start` | A new session begins |
| `on_session_end` | A session ends |
| `on_session_reset` | A session is reset |
| `on_session_finalize` | A session is finalized |
| `on_stream_start` | A stream starts |
| `on_stream_delta` | A stream yields a delta |
| `on_stream_end` | A stream ends |
| `pre_api_request` | Before an API request |
| `post_api_request` | After an API request completes |
| `api_request_error` | An API request fails |
| `subagent_start` | A subagent starts |
| `subagent_stop` | A subagent stops |
| `pre_approval_request` | Before an approval request |
| `post_approval_response` | After an approval response |
| `gateway_platform_event` | A gateway platform event |
| `pre_command` | Before a command executes |
| `on_skill_lifecycle` | A skill lifecycle event |

Every hook is an observer — Abyss records what happened, detects signals, and clusters incidents. It does not interfere with the agent's execution. The v2 manifest declares these hooks in `provides_hooks` so the plugin doctor can verify alignment.

---

## State Safety

Abyss is built to fail open and never silently corrupt state:

- **All handlers accept `**_`** — extra kwargs don't break anything.
- **Fail-open consent default** — every hook is an observer; no privileged surfaces.
- **`_coerce_body`** is fully None/bytes/double-encoded tolerant.
- **`_mask_secrets`** is applied on all stored free-text.
- **Cross-module refs** (`_add_activity`, `_coerce_body`, `_get_activity_conn`) all resolve. No dead imports.
- **SQLite connections** are closed in every function that opens them (or use `finally`).
- **Wave writes** use `_wave_with_retry` (single retry on `database is locked`, closes conn before backoff — hardened for WAL cross-process collisions).

The doctor-clean declaration requires every registered hook to be listed in `provides_hooks`, so the manifest and the hook registrations are kept in sync.

---

## Config

Abyss reads config from the plugin manifest's `config_schema` (v2):

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `retention_days` | integer | 30 | Prune activity/signals/incidents older than N days on startup |
| `webhook_url` | string | "" | Slack/Discord/Teams-compatible webhook for alert notifications |
| `stream_signals` | boolean | true | Emit an empty_stream signal when a streamed response yields zero tokens |
| `stream_signal_coalesce_minutes` | integer | 10 | Coalesce repeated same-type wave signals per session within N minutes |

Config is read from the Hermes config/state bridge. No config file needed to run.

---

## Privacy

Abyss is local-first. All observation — activity, signals, graph, traces, wave telemetry — stays on-machine and is never exported.

Agent-powered remediation (signal/incident `resolve (cloud agent)`, doctor apply) may dispatch a free-Nous cloud agent. Every such action is labeled as cloud in the visible UI: the button says `resolve (cloud agent)`, the tooltip says so, and the masthead boot line carries `--cloud-fix`. No surprise. No hidden network call.

Secret-looking values (tokens, passwords, API keys) are redacted from logs, signals, wave payloads, and stored activity at record time.

---

## Tech Stack

- **Backend:** Python + FastAPI + SQLite. Flat module pair (`__init__.py` + `abyss_wave.py`). No ORM. No migrations framework. Direct SQL.
- **Frontend:** Single-file ESM React plugin (`desktop/plugin.js`), loaded as a Blob URL by the Hermes desktop app. No build step. Only `@hermes/plugin-sdk`, `react`, and `react/jsx-runtime` importable.
- **Graph:** Canvas force-layout (DitherKit) with Atkinson-dithered phosphor ground. Computed theme colors via `getComputedStyle`.
- **UI framework:** Hermes desktop app + plugin SDK. The plugin runs inside the app's React tree and respects host theming.
- **Theme:** Host theme CSS variables (`var(--ui-*)`). No hex/rgb literals. Works in both dark and light modes.
- **API:** FastAPI router (`dashboard/plugin_api.py`) delegated to core routes in `__init__.py`. REST at `/api/plugins/abyss/`.

No new toolchain. The plugin is the existing codebase.

---

## Test Suite

Abyss ships a test suite covering the backend and the REST API:

- `test_plugin.py` — backend core tests (activity store, signals/incidents, doctor, benchmark, health, trace graph, timeline, agents-overview, hook registration alignment)
- `test_wave.py` — wave telemetry tests
- `dashboard/test_api.py` — REST API tests

Run the suite:

```sh
cd plugins/abyss
python -m pytest test_plugin.py test_wave.py dashboard/test_api.py -v --tb=short
```

The plugin doctor verifies hook registration alignment automatically (every registered hook must be listed in `provides_hooks`).

---

## Development

Abyss is a small, focused codebase. The backend is a flat Python module pair and the frontend is a single-file desktop plugin. Keep it that way.

### Layout

```
__init__.py            Backend core (flat — test scripts import it as a sibling)
abyss_wave.py          Wave expansion
dashboard/
  plugin_api.py        REST dispatcher (thin delegate to core routes)
  manifest.json        Dashboard manifest
  test_api.py          REST API tests
desktop/
  plugin.js            Single-file ESM React UI (Blob URL, no build step)
DESIGN.md              Committed design world (phosphor-terminal / midnight machine room)
PRODUCT.md             Product contract (users, purpose, capabilities, constraints)
plugin.yaml            v2 plugin manifest (hooks, commands, api path, config schema)
manifest.json          Plugin manifest (id, name, version, api, entry)
manifest.yaml          Human-readable manifest
requirements.txt       Python deps (fastapi, httpx)
test_plugin.py         Backend tests
test_wave.py           Wave tests
smoke_real_agent.py    Smoke test against a real Hermes agent
```

### Design Reference

The committed design world lives in `DESIGN.md` in the desktop plugin folder. It is the reference for:

- World (phosphor-terminal / midnight machine room)
- Direction contract (thesis, own-world, story, first viewport, form)
- Color (host theme variables only, status accents via `var(--ui-*)`, strokes, accent)
- Type (mono stack, tabular numerals, scale, treatment, measure)
- Components (masthead, status strip, tabs, activity rows, calendar, search, trace graph, timeline, brain graph, watch, health report, wave, status chip)
- States (loading, empty, error, busy, hover, disabled/acknowledged/resolved)
- Motion (one authored moment, ambient scanline + cursor, reduced-motion media query)
- Constraints (single-file Blob-URL, `ctx.rest()` resolves parsed JSON, no `params` in `PluginRestOptions`, only compiled Tailwind classes, canvas colors from `getComputedStyle`, `EmptyState` no icon, `TabsContent` not exported)
- Trace views (graph + timeline)

### Product Reference

The product contract lives in `PRODUCT.md`. It defines:

- Platform (web)
- Stack (existing codebase — single-file ESM plugin.js + Python/FastAPI backend)
- Users (technical operator running Hermes Agent daily with multiple profiles)
- Product purpose (local-first observability, Raindrop-inspired, silent-failure surface, drill-don't-rediscover)
- Positioning (the only observability layer that watches Hermes agents from the inside)
- Operating context (sidebar pane, full-page route, sidebar nav, command palette, status bar chip; dark/light theme; Blob-URL UI; Python/SQLite backend)
- Capabilities (eight views, drill-down, REST API, status chip, graph, slash commands)
- Technical constraints (single-file, SDK import surface, theme variables only, canvas panes need ResizeObserver, triage wired with busy state)
- Brand commitments (name "Abyss", local-first, graph is the soul)
- Product principles (health at a glance, drill-don't-rediscover, local-first and private, operate-mode discipline, graph is the soul)

### Writing a New Wave Surface

1. Add the surface to the wave schema in `abyss_wave.py` (register the hook, define the record shape).
2. Add the surface to the wave table in `plugin.js` (surface label, tone color, count query).
3. Add a REST endpoint in `plugin_api.py` → core route in `__init__.py` if the surface needs a dedicated query.
4. Add a test in `test_wave.py`.
5. Update this README's wave table.

### Writing a New View

1. Add the view component to `plugin.js` (inlined — single file).
2. Add the tab to the tabs array (Brain graph is the default/first tab; order is brain / watch / health / activity / trace / wave / search / calendar).
3. Add the REST endpoint in `plugin_api.py` → core route in `__init__.py` if the view needs a dedicated query.
4. Add a `trace ›` drill if the view surfaces sessions.
5. Add a test in `test_plugin.py`.

---

## License

MIT — see [LICENSE](LICENSE).

## Credits

Abyss is built by the Hermes Agent community. The Raindrop.ai inspiration is deliberate — the product borrows the idea of an observability instrument that presents agent behavior as a living transcript, not a deck of metric cards.

The phosphor-terminal design world, the Hermes Brain graph, and the DitherKit canvas are Leviath/Kraken work.

The v2 plugin manifest, the wave telemetry surfaces, the doctor agent, the health score and failure taxonomy, and the webhook alerting are community work built on top of the original core.

---

<!-- A living document. Updated by the night-shift crews. -->
