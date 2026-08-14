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
  blinking block cursor; states print themselves ("$ ./abyss --observe --local")
  rather than appearing as badges.
- **STORY:** glance → health; symptom → one click → session trace; the Hermes
  Brain renders as a phosphor instrument.
- **FIRST VIEWPORT:** masthead boot line + mono metric strip (ACT/CRN/CAT/SIG
  with an open-signal dot), six terminal-style tabs below.
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
  because only `bg-(--ui-green)` and `bg-(--ui-yellow)` exist as compiled
  classes; the rest are variables, resolved inline.
- **Strokes:** `border-(--ui-stroke-tertiary)` (hairlines between rows),
  `border-(--ui-stroke-secondary)` (calendar today ring), `divide-(--ui-stroke-tertiary)`.
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
- **Measure:** rows keep `truncate`/`line-clamp-2`; no body text exceeds a
  comfortable width in the 420px pane.

## Components

- **Masthead:** `abyss` wordmark (bold, tracked, uppercase) + blinking block
  cursor (`@keyframes abyss-blink`, `var(--ui-green)`), boot-line
  `$ ./abyss --observe --local`, thin caption line, scanline sweep
  (`@keyframes abyss-scan`, `var(--ui-text-quaternary)` gradient).
- **StatusStrip:** mono metric strip — `live:` prefix, ACT/CRN/CAT/SIG with
  tabular values, trailing health dot + label (`all clear` / `N open` /
  `N critical`). Replaces the incumbent's four stat cards.
- **Tabs:** six SDK tabs (activity / calendar / search / trace / brain / watch);
  content rendered conditionally (TabsContent not exported).
- **Activity rows:** category glyph `▸` in type color, action title, relative
  time, category + status badges, session id micro-label.
- **Calendar:** 7-column grid via inline `gridTemplateColumns` (grid-cols-7 is
  dead in the compiled CSS); day cells `min-height:72px` inline; today ring via
  `ring-1 ring-(--ui-stroke-secondary)`; task chips in type colors.
- **Search:** SDK SearchField; source toggles; result rows with source label in
  type color, relevance %, relative time, 2-line clamp.
- **Trace:** SDK Select for sessions; timeline spine `left-2` hairline; event
  glyphs absolutely positioned with inline `left:-19px` (arbitrary classes dead);
  event type, relative time, tool/model, preview, source.
- **Brain graph:** phosphor DitherKit canvas — Atkinson-dithered ground,
  computed theme colors (`palette()` reads `getComputedStyle`; canvas cannot
  resolve `var()` strings), soft hover/selection glow, drag/zoom. Legend
  swatches inline-styled.
- **Signals & Incidents:** severity + status badges; triage wired to
  `/signals/{id}/acknowledge|resolve` and
  `/incidents/{id}/acknowledge|resolve|reopen|close`; "cluster" button; busy
  state disables the row's buttons.
- **Status chip:** `abyss` + open-signal count with health dot, navigates to
  `/abyss`; fetches `/signals?limit=50` (not the old LLM-count).

## States

- **Loading:** pulsing skeleton blocks or `GlyphSpinner` (braille) + terminal
  voice ("building brain…").
- **Empty:** SDK `EmptyState` (title + description; the SDK takes no icon prop).
- **Error:** SDK `ErrorState` with a Retry button — errors never masquerade as
  empty data (incumbent P0).
- **Busy:** triage buttons disabled while a mutation runs.
- **Hover:** rows `hover:bg-(--ui-bg-tertiary)`; graph nodes glow.
- **Disabled/acknowledged/resolved:** explicit badges so state is never implied
  by color alone.

## Motion

- **One authored moment:** the scanline drift (9s linear) + the blinking cursor
  (1.1s step-end). Everything else uses host SDK transitions. No scattered
  per-element animation.
- Motion is pure CSS keyframes injected once (`#abyss-console-css`); colors in
  the keyframes reference theme vars only.

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
