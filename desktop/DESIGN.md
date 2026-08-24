# Design — Abyss Dashboard

<!-- impeccable:design-schema 1 -->

## World

**Phosphor-terminal / midnight machine room.** The Abyss is a CRT instrument
sitting in a dark operations room: agent behavior prints itself as a living
transcript instead of being dressed in cards and chrome. The identity is
carried by type, texture, and motion — not by novel colors — so it survives
inside the host theme system (dark and light).

## Direction contract

- **THESIS:** An instrument, not a dashboard. The first line answers "are my
  agents OK right now?"; the incumbent's hero-metric card strip and card-of-cards
  scaffold are refused.
- **OWN-WORLD:** green-phosphor terminal / midnight machine room. Monospace type
  and tabular numerals throughout, near-black ground, a drifting scanline, a
  blinking block cursor; states print themselves ("$ ./abyss --observe --local --cloud-fix")
  rather than appearing as badges.
- **STORY:** glance → health; symptom → one click → session trace; the Hermes
  Brain renders as a phosphor instrument.
- **FIRST VIEWPORT:** masthead boot line + mono metric strip (ACT/HLTH/INC/CRN/CAT/SIG
  with an open-signal dot), then the eight views behind terminal-style tabs;
  the Brain graph is the DEFAULT surface (brain / watch / health / activity /
  trace / wave / search / calendar), because the graph is the soul and the
  centerpiece.
- **FORM:** phosphor-terminal world, committed at full fidelity inside the host
  theme system. Concept-seed roll `b920409f` (fused challenger).

## Color

Host theme variables only — no hex/rgb literals in the artifact.

- **Surfaces:** `bg-(--ui-bg-editor)` (graph ground), `bg-(--ui-bg-elevated)`
  (calendar cells), `bg-(--ui-bg-tertiary/quaternary)` (hover/skeleton/strip),
  `bg-(--ui-bg-quinary)` (sub-strips). Root shell uses SDK tokens
  (`bg-background`/`text-foreground`).
- **Text:** `text-(--ui-text-primary/secondary/tertiary/quaternary)` for the
  four-step hierarchy; `text-(--ui-text-quaternary)` is the terminal-dim line.
- **Status accents** (via inline `var(--ui-*)` so they exist in both themes):
  green = healthy, yellow = warning, red = critical; blue = neutral running
  state; cyan/purple/orange = type colors (session/tool/memory/category).
  Legend swatches and signal dots use `style={{ backgroundColor: 'var(--ui-*)' }}`
  because only `bg-(--ui-green)`, `bg-(--ui-yellow)` and `bg-(--ui-accent)` exist
  as compiled classes; the rest are variables, resolved inline (verified against
  the host's compiled CSS: `bg-(--ui-red)` and all `bg-(--ui-*)/15` variants are
  NOT compiled and must never be used as classes).
- **Strokes:** `border-(--ui-stroke-tertiary)` (hairlines between rows),
  `border-(--ui-stroke-secondary)` (calendar today ring). NOTE: `divide-(--ui-stroke-tertiary)`
  is NOT compiled — row lists use per-row `border-b border-(--ui-stroke-tertiary)`.
- **Accent (selection/hover emphasis):** SDK Button/Badge variants and
  `hover:bg-(--ui-bg-tertiary)` rows; the graph uses `--ui-accent` computed
  color for the selected node stroke.

## Type

- **Stack:** host mono (`var(--font-mono, ui-monospace, SFMono-Regular, Menlo,
  Consolas, monospace)`) for everything; tabular numerals (`tabular-nums`) for
  every metric and timestamp.
- **Scale (compiled classes only):** `text-xs` (0.75rem) body, `text-sm`
  headings/action names, `text-[0.65rem]`/`text-[0.6rem]` micro-labels (both
  confirmed live in the compiled CSS), `text-xl` for large numerals.
- **Treatment:** uppercase + `tracking-widest` for micro-labels (ACT/CRN/SIG,
  category tags, tab labels); lowercase mono for terminal-voice labels
  (`activity`, `search`, `trace`, `watch`).
- **Body copy is mono too:** activity action/description, search titles,
  signal labels, incident titles and doctor findings all carry `abyss-mono` —
  the transcript itself reads in-world, not just the chrome around it.
- **Measure:** rows keep `truncate`/`line-clamp-2`; no body text exceeds a
  comfortable width in the 420px pane.

## Components

- **Masthead:** `abyss` wordmark (bold, tracked, uppercase) + blinking block
  cursor (`@keyframes abyss-blink`, `var(--ui-green)`), boot-line
  `$ ./abyss --observe --local --cloud-fix`, thin caption line, scanline sweep
  (`@keyframes abyss-scan`, `var(--ui-text-quaternary)` gradient). The
  `--cloud-fix` flag is an honest disclosure: observation stays local, but
  agent-powered remediation (resolve, doctor apply) can dispatch a free-Nous
  cloud agent — those buttons say so in their label.
- **StatusStrip:** mono metric strip — `live:` prefix, ACT/HLTH/INC/CRN/CAT/SIG with
  tabular values, trailing health dot + label (`all clear` / `N open` /
  `N critical`). Replaces the incumbent's four stat cards. Backend failure
  prints `status link down` with a retry button — never a false "all clear". On
  poll, if the health numbers actually changed, the strip re-prints once (0.5s
  opacity flash, ease-out — the only authored motion besides scanline/cursor).
  The glance connects to the action: `SIG` and `HLTH` are live jump-points into
  the watch / health views, and the trailing verdict dot + label (`N critical ›`)
  jumps into watch — all via the dashboard's `setActiveTab`. The verdict is
  right-pinned and never breaks mid-phrase (`ml-auto shrink-0 whitespace-nowrap`);
  in the 420px pane it settles onto its own line below the metrics.
- **Tabs:** eight SDK tabs, Brain graph is the DEFAULT surface and the first
  tab; order is brain / watch / health / activity / trace / wave / search /
  calendar — operational instruments first, browsing views last; content
  rendered conditionally (TabsContent not exported).
- **Activity rows:** category glyph `▸` in type color, action title, relative
  time, category + status badges, session id micro-label; any row with a
  session id carries a `trace ›` button that jumps to the trace view with that
  session pre-selected (drill-don't-rediscover).
- **Calendar:** 7-column grid via inline `gridTemplateColumns` (grid-cols-7 is
  dead in the compiled CSS); day cells `min-height:72px` inline; today ring via
  `ring-1 ring-(--ui-stroke-secondary)`; task chips in type colors with status
  GLYPHS (✓ / ▶ / ○) so state is never color-only. A chip whose task carries a
  `session_id` gets a `trace ›` drill (the backend does not currently include
  `session_id` in `/calendar` rows — the affordance lights up when it does).
- **Search:** SDK SearchField; source toggles; result rows with source label in
  type color, relevance %, relative time, 2-line clamp; session hits carry a
  `trace ›` drill (the result id IS the session id).
- **Trace:** SDK Select for sessions; timeline spine `left-2` hairline; event
  glyphs absolutely positioned with inline `left:-19px` (arbitrary classes dead);
  event type, relative time, tool/model, preview, source.
- **Brain graph:** phosphor DitherKit canvas — Atkinson-dithered ground,
  computed theme colors (`palette()` reads `getComputedStyle`; canvas cannot
  resolve `var()` strings), soft hover/selection glow, drag/zoom. Selection is
  OPERABLE: click a node to select it (empty ground clears), arrow keys move
  the selection between nodes, Enter/Space on a session node drills into its
  trace, Escape clears; the canvas is focusable (`tabIndex 0`) and its
  aria-label announces the selection. A selected session node reveals a
  `trace ›` button in the header. Resize recompute is rAF-throttled so pane
  drags coalesce to one layout pass per frame. Legend swatches inline-styled;
  footer hint prints the keyboard map (`click: select · arrows: move ·
  enter: trace`).
- **Watch (signals & incidents):** severity + status badges; triage wired to
  `/signals/{id}/acknowledge|resolve` and
  `/incidents/{id}/acknowledge|resolve|reopen|close`; "cluster" button (combine
  codicon — deliberately distinct from `refresh`); busy state disables the
  row's buttons. Agent-powered resolve is labeled `resolve (cloud agent)` —
  it dispatches a free-Nous cloud agent, disclosed in the label, the tooltip
  and the masthead boot line (`--cloud-fix`). Any signal with a `session_id`
  (or incident with `session_ids`) carries a `trace ›` drill into that
  session's trace.
- **Health (report):** `$ abyss health` status line (score/100, level dot +
  word, counters) then hairline sections — doctor flow, benchmark, score
  breakdown bars, 7-day trend bars, failure taxonomy. No cards, no stat
  monument: it prints like a diagnostic report.
- **Wave (telemetry):** `$ abyss wave` header, a surface/count/last terminal
  table (8 surfaces, tone dot + colored micro label + tabular count), then the
  merged hairline feed. Loading prints "listening for wave telemetry…";
  backend failure uses ErrorState + retry.
- **Status chip:** `abyss` + health score (or open-signal count) with health dot,
  navigates to `/abyss`; fetches `/status` (not the old LLM-count).

## States

- **Loading:** pulsing skeleton blocks or `GlyphSpinner` (braille) + terminal
  voice ("building brain…", "listening for wave telemetry…").
- **Empty:** SDK `EmptyState` (title + description; the SDK takes no icon prop).
- **Error:** SDK `ErrorState` with a Retry button — errors never masquerade as
  empty data (incumbent P0). StatusStrip prints `status link down` + retry
  instead of a false "all clear".
- **Busy:** triage buttons disabled while a mutation runs.
- **Hover:** rows `hover:bg-(--ui-bg-tertiary)`; graph nodes glow.
- **Disabled/acknowledged/resolved:** explicit badges so state is never implied
  by color alone.

## Motion

- **One authored moment:** the metric strip re-prints when health numbers change
  (0.5s opacity flash, ease-out) — on top of the ambient scanline drift (9s
  linear) and the blinking cursor (1.1s step-end). Everything else uses host
  SDK transitions. No scattered per-element animation.
- Motion is pure CSS keyframes injected once (`#abyss-console-css`); colors in
  the keyframes reference theme vars only.
- `@media (prefers-reduced-motion: reduce)` disables scanline, cursor and flash
  animations.

## Constraints (hard-won, verified against source)

- Single-file Blob-URL plugin: no relative imports; all components inlined.
- `ctx.rest()` resolves parsed JSON — never call `.json()` on the result
  (electron `fetchJson` → `JSON.parse`).
- `PluginRestOptions` has no `params`; query strings go in the path
  (`/activity?limit=50&category=cron`).
- Only Tailwind classes present in the host's compiled CSS render; all class
  tokens in this build are verified live (0 dead) or injected custom
  (`abyss-*`). Inline styles are used for values with no compiled class
  (grid template columns, arbitrary offsets).
- Canvas colors come from `getComputedStyle`, not `var()` strings.
- `EmptyState` accepts no icon; `TabsContent` is not exported; Badge sizes
  `default|xs`, Button variants per SDK.

## Trace views — Graph & Timeline (Raindrop trajectories)

The Trace tab gains two view modes beside the incumbent list. A segmented
toggle in the trace header (`list` / `graph` / `timeline`) swaps the whole
content pane; list stays the default so a fresh user sees the known surface.

### Trajectory Graph (`/trace/graph` → `TraceGraphView`, canvas)

- Backend pairs `tool_call` **start/end** events by `tool_call_id` into a
  single node carrying `status` (ok|error|running), `error_type`,
  `error_message`, and `duration_ms`; groups each `llm_call` reasoning turn
  with the tool calls it spawned (`parent` → `spawn` edges) → a left-to-right
  DAG instead of a flat list.
- Canvas layout is deterministic, not force-simulated: BFS depth turns into
  columns; nodes stack within a column and are centered vertically. No
  timestamp jitter, no dangling `Math.max(...)` on an empty set.
- Status → color: session=blue, reasoning=purple, ok=green, error=red,
  running/unknown=amber. Selected node highlights with the accent color and
  prints a detail row (label, duration, tool, error/result preview).
- Click hit-tests node rects from a layout ref stored on each draw.
- `ResizeObserver` re-sizes the canvas with the pane (DPR-aware).

### Agent Timeline (`/trace/agents` + `/trace/timeline` → `TraceTimelineView`)

- **Agents overview** (`/trace/agents`): every session renders as one
  horizontal bar on a shared time axis — green when healthy, red when it has
  failures, with an error tick. Clicking a lane selects that session. This is
  the literal "see each agent as a timeline" view.
- **Trajectory detail** (`/trace/timeline?session_id=…`): one session split
  into three horizontal lanes — Reasoning / Tools / Failures — each event
  positioned by its start offset and sized by its duration; failures glow
  red. Hover reveals `label · status · duration`.
- Errors are counted from `"status":"error"` JSON (both spacing variants),
  never from the literal `"error_type"` key — which is present even when
  `null` and previously inflated counts (fixed in `get_agents_overview`).

Backend routes are thin delegations in `dashboard/plugin_api.py` → core
`get_trace_graph` / `get_trace_timeline` / `get_agents_overview` in
`__init__.py`. Covered by `test_plugin.py` §14 (14 asserts).

