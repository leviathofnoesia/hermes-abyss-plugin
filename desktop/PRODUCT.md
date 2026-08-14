# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack
Existing codebase: single-file ESM `plugin.js` (no build step) loaded by the Hermes desktop app runtime via Blob URL, plus a Python/FastAPI backend (`plugins/abyss`) serving REST at `/api/plugins/abyss/*`. Frontend uses `@hermes/plugin-sdk`, `react`, `react/jsx-runtime` only. No new toolchain.

## Users

Primary user: a technical operator (Billy) running Hermes Agent daily with multiple agent profiles. The user is an advanced user who wants problems solved efficiently, values directness, and monitors agent behavior for silent failures. They use Abyss as a live observability instrument while agents work — checking health, debugging failed runs, and reviewing what the agent did.

## Product Purpose

Abyss is a local-first observability dashboard for Hermes AI agents, inspired by Raindrop.ai. It auto-records tool calls, LLM interactions, and session lifecycle events into SQLite; detects "signals" (silent agent failures: errors, timeouts, rate limits, loops, vague replies); clusters signals into incidents; and renders the agent's memory/tool graph. Success means the user can see at a glance whether their agents are healthy, and can drill from a vague symptom down to the exact tool call, session, and root cause.

## Positioning

The only observability layer that watches Hermes agents from the inside: it hooks the actual agent lifecycle (pre/post tool and LLM calls, session boundaries), classifies failure modes, clusters incidents by root cause, and visualizes the agent's own "brain" graph — all locally, with no external data leaving the machine.

## Operating Context

- Lives inside the Hermes desktop app: a right-sidebar pane (default ~420px), a full-page route at `/abyss`, a sidebar nav entry, a command-palette entry, and a status-bar chip showing live signal counts.
- Used while agents work (live monitoring) and after runs (post-mortem debugging).
- The app has a dark/light theme with CSS variables (`var(--ui-*)`); the plugin must respect host theming and never hardcode colors.
- UI loads uncompiled as a Blob URL: single file, no relative imports, `jsx()` calls instead of JSX syntax.
- Backend is Python + SQLite, mounted at `/api/plugins/abyss/`, called via `ctx.rest()`.

## Capabilities and Constraints

Confirmed capabilities (from code):
- Six views: Activity Feed, Calendar, Global Search, Tracing timeline, Hermes Brain graph, Signals & Incidents.
- REST API: `/activity` (GET list/POST add), `/calendar`, `/search`, `/stats`, `/trace`, `/graph`, `/signals`, `/incidents`, `/signals/self-diagnostic`, `/incidents/cluster`, `/prune`, plus triage mutations: `/signals/{id}/acknowledge|resolve`, `/incidents/{id}/acknowledge|resolve|reopen|close`.
- Status bar chip (signal/llm count), sidebar nav, palette command, pane + full-page route.
- Graph rendered on canvas (DitherKit force layout + Atkinson dithering background) with drag, hover, zoom.
- Slash commands: `/abyss recent|stats|search|trace|signals|incidents|diagnostic|clean`.

Technical constraints (hard):
- Single self-contained `plugin.js`; only `@hermes/plugin-sdk`, `react`, `react/jsx-runtime` importable.
- SDK gotchas: `TabsContent` NOT exported (render conditionally), Badge variants `default|muted|warn|destructive|outline`, Button variants `default|destructive|outline|secondary|ghost|link|text|textStrong`, sizes `default|xs` (Badge) / `default|xs|sm|lg|inline` (Button).
- Theme variables only for color; no `#hex`/`rgb()` literals.
- Canvas panes need ResizeObserver; stale closures avoided via refs/state.
- Must work at pane width (~420px) and full-page width.
- Triage endpoints (acknowledge/resolve/reopen/close) exist in the API but the current UI does NOT wire them — an identified UX gap to close.

## Brand Commitments

- Name: "Abyss" — Raindrop-style observability. Existing i18n keys and plugin id `abyss` are stable. Codicon `eye` for nav/status.
- Local-first privacy is a core trait: no external services.
- The DitherKit canvas graph is a signature feature; keep and elevate it, don't remove it.

## Evidence on Hand

- Live plugin: `C:\Users\billy\AppData\Local\hermes\desktop-plugins\abyss\plugin.js` (1509 lines, incumbent UI).
- Backend: `C:\Users\billy\AppData\Local\hermes\plugins\abyss\dashboard\plugin_api.py`, `__init__.py` (69 KB core), manifests.
- Test harness: `test_plugin.py`, `dashboard/test_api.py` — expected PASS suite.
- No screenshots or DESIGN.md exist; the incumbent visual identity is the generic Hermes SDK look (theme-variable Tailwind classes, no custom design system).

## Product Principles

1. Health at a glance: the first thing a user sees must answer "are my agents OK right now?"
2. Drill-don't-rediscover: every symptom (signal, incident, activity row) leads one or two clicks to the underlying session/trace.
3. Local-first and private: nothing leaves the machine; the dashboard is an instrument, not a data exporter.
4. Operate-mode discipline: scanability, consistent states (loading/empty/error), and real usage scenes outrank decoration; brand lives in precise details.
5. The graph is the soul: the Hermes Brain visualization is what makes Abyss memorable — treat it as the centerpiece, not an afterthought.

## Accessibility & Inclusion

- Must respect host theme variables (dark and light modes).
- Keyboard-operable primary controls; focus rings via theme accent.
- No reliance on color alone: severity and status are also conveyed by labels/iconography.
