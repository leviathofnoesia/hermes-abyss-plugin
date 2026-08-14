# Design Review — Abyss Desktop Plugin

Review date: 2026-08-14 · Reviewer: Daedalus (packaging pass)
Scope: `desktop/plugin.js` (2722 lines, single-file Blob-URL React) against
`DESIGN.md` (design contract) and `PRODUCT.md` (product contract).
No browser target exists for a Blob-URL desktop plugin, so this is a
code-level / degraded-environment review: static read of the artifact against
the committed design world, SDK contract rules, and state-handling discipline.

## Verdict

**Score: 36/40** (pre-review baseline 35/40; +1 from applied P1 fixes).
The artifact is remarkably faithful to the DESIGN.md contract — the
phosphor-terminal world is implemented at high fidelity with disciplined
state handling. No P0 issues. Three P1 a11y/feedback issues were found and
fixed in the repo copy. Everything else is listed as recommendations, not
auto-applied.

## Rubric (8 categories × 5 pts)

| # | Category | Pts | Notes |
| --- | --- | --- | --- |
| 1 | DESIGN.md contract fidelity | 4.5 | Phosphor world, boot lines, mono/tabular type, scanline + blink cursor, theme-only colors — all present. Two minor drifts: (a) `CONSOLE_CSS` and `themeColor()` carry hex/rgba *fallbacks* (`#55a583`, `rgba(128,128,128,0.12)`) — DESIGN.md says "no hex/rgb literals in the artifact"; defensible as resilience, but literal. (b) DESIGN.md commits to "six terminal-style tabs"; implementation renders eight (adds `health`, `wave`). Evolved, not broken. |
| 2 | State handling (loading/empty/error/busy) | 5.0 | Every view has skeleton loading, `EmptyState`, `ErrorState` + Retry (errors never masquerade as empty — the documented incumbent P0 is fixed). Doctor flow has a full phase machine with wall-clock backstops (25/45 min) so `running`/`applying` can never spin forever. Busy states disable triage buttons; resolution polling while agents run. |
| 3 | Accessibility | 4.0 | Theme vars for both modes; severity conveyed by badge label + dot (not color alone); SearchField/Spinner carry ariaLabels. **Found P1:** four icon-only buttons (calendar chevrons ×2, brain refresh, cluster refresh) had no accessible name; brain canvas had no role/label; cluster failure was invisible. Fixed (see below). **Remaining gap (recommendation):** the canvas graph is not keyboard-operable or screen-reader-navigable beyond the img label — a real graph would need a data table fallback; out of scope for a packaging pass. |
| 4 | Copy & semantics | 4.5 | Terminal voice consistent; no fabricated metrics; empty states are honest. Minor: wave view header "plugin wave — aug 2026 interface" leaks an internal iteration date into user-facing copy. |
| 5 | Theme/visual system | 4.5 | All colors resolve to `var(--ui-*)`; canvas reads `getComputedStyle` via `palette()` (correct per contract); legend swatches inline-styled. Hex fallbacks noted in #1. |
| 6 | Performance & robustness | 5.0 | Force layout O(n²) with velocity caps + hard escape clamp (sparse-graph blowup guarded); canvas sized from container (never the 300×150 default); ResizeObserver re-layout; fetch failures never crash; polling intervals bounded. `ctx.rest()` never `.json()`'d; query strings in path (SDK has no `params`). |
| 7 | SDK/runtime constraint compliance | 5.0 | Verified against the hard-won constraint list: no relative imports, imports only `@hermes/plugin-sdk`/`react`/`react/jsx-runtime`, no `TabsContent` (conditional render), no `EmptyState` icon prop, Badge/Button variants within SDK set, no hex colors in compiled-class positions, no `dangerouslySetInnerHTML`. |
| 8 | Feedback on user actions | 4.0 | Triage single-flight guard prevents double-submit; resolution rows show running/failed/succeeded states. **Found P1:** cluster button gave zero feedback (silent console-only failure, no disabled state during the POST). Fixed. **Remaining (recommendation):** failed acknowledge/resolve/close actions are console-only; triage buttons aren't visually disabled while a non-resolve mutation is in flight (the `busyId` computed per row only matches `resolve-agent`/`reopen` patterns, though the single-flight guard makes double-submit impossible). |

## P0 / P1 issues found and fixed (in the repo copy only)

All fixes are objective, minimal, and do not touch plugin logic or styling.

1. **P1 (a11y) — icon-only buttons without accessible names.** Calendar
   previous/next-week chevrons, the brain-graph refresh button, and the
   incident-cluster button are icon-only `Button`s with no `title`/label.
   Fixed: added `title` attributes ("Previous week", "Next week", "Refresh
   graph", "Run incident clustering"). `title` was chosen over `aria-label`
   because plain attribute passthrough is proven in this file (existing
   `title` usage on close/resolve buttons); SDK prop spread for `aria-label`
   is unverified.

2. **P1 (feedback) — incident clustering was a silent operation.** The
   cluster POST failed into `console.error` only; the button never disabled
   during the run, so a user got zero feedback. Fixed: added `clustering`
   busy state (button disabled + title "Clustering incidents…"), and a
   visible inline error line ("cluster failed — incidents were not updated")
   rendered in the watch-view header on failure. No backend change.

3. **P1 (a11y) — brain canvas had no accessible name.** The canvas graph
   rendered with no `role`/`aria-label`. Fixed: `role="img"` +
   `aria-label="Hermes brain graph: N nodes, M edges"`. Full keyboard graph
   navigation remains a recommendation (below).

After fixes: `node --check desktop/plugin.js` → OK.

## Triage-wiring reconciliation (PRODUCT.md:47 vs DESIGN.md:90-92)

**Claim A (PRODUCT.md line 47):** "Triage endpoints (acknowledge/resolve/
reopen/close) exist in the API but the current UI does NOT wire them — an
identified UX gap to close."

**Claim B (DESIGN.md lines 90-93):** "Signals & Incidents: … triage wired to
`/signals/{id}/acknowledge|resolve` and `/incidents/{id}/acknowledge|resolve|
reopen|close`; 'cluster' button; busy state disables the row's buttons."

**Verdict from current `plugin.js` source: DESIGN.md (B) is TRUE; PRODUCT.md
(A) is STALE.** Evidence:

- `plugin.js:1727` — `runAction('signals', s.id, 'acknowledge')` → POST
  `/signals/{id}/acknowledge`.
- `plugin.js:1593-1605` — `resolveAgent()` → POST `/signals/{id}/resolve-agent`
  and `/incidents/{id}/resolve-agent` (agent-powered resolve with
  `resolution_status` polling).
- `plugin.js:1807-1824` — incidents: `acknowledge` (open only), `reopen`
  (resolved/closed), `close` (open/acknowledged/resolved), plus `resolve` for
  open/acknowledged.
- `plugin.js:1559-1567` — `clusterIncidents()` → POST `/incidents/cluster`,
  wired to the watch-header button.

PRODUCT.md was written against the 1509-line incumbent UI; the current 2722-line
artifact (the DESIGN.md build) wires the full triage matrix. PRODUCT.md:47
should be treated as an outdated handoff note, not a current gap.

## Recommendations (listed, NOT auto-applied)

Tonal / enhancement, in priority order:

1. **Triage error surfacing** — failed acknowledge/resolve/close/doctor
   mutations are console-only. Add a transient inline error in the row or a
   toast. (P2, real-world value high.)
2. **Visible busy state for non-resolve triage** — while a `close`/
   `acknowledge` mutation is in flight the exact button stays enabled-looking
   (single-flight guard prevents actual double-submit). Disable the row's
   buttons on any in-flight mutation for that row.
3. **Keyboard-accessible graph** — the canvas is pointer-only. Add a
   screen-reader data-table fallback or an adjacent node list for
   keyboard/screen-reader users (a11y contract says "keyboard-operable
   primary controls").
4. **Graph zoom affordance** — wheel-zoom exists but is undiscoverable; add
   a hint line or +/- controls.
5. **Wave feed coverage** — the merged wave feed shows events/streams/api/
   subagents/approvals but not commands/platform/skills (counts come from
   summary, feed omits three surfaces). Either add them or say the feed is
   curated.
6. **Copy polish** — "plugin wave — aug 2026 interface" leaks an iteration
   date; drop to "plugin wave — interface telemetry".
7. **Dead code** — `const c = palette(); void c` in `_generateBackground`
   (unused); `EVENT_ICONS`/`EVENT_TONES` are fine but the `dot` fallback icon
   name should be verified against Codicon's set.
8. **Hex fallbacks** — consider moving `CONSOLE_CSS`/`themeColor()` fallback
   literals into a single named constant if strict DESIGN.md literal-compliance
   is desired (or amend DESIGN.md to permit fallbacks).
9. **i18n keys** — `ctx.i18n.register()` registers `en` keys that the UI
   never calls (`t()` not used); either use them or drop the registration to
   avoid drift.
10. **PR body material** — the DESIGN.md "FINISH" comment references an
    internal finish review; keep as provenance, but the repo README already
    supersedes it as the public entry point.

## Evidence

- Full read of `desktop/plugin.js` (2722 lines post-fix) against
  `desktop/DESIGN.md` (130 lines) and `desktop/PRODUCT.md` (74 lines).
- `node --check desktop/plugin.js` → OK (pre- and post-fix).
- Triage wiring cited at `plugin.js` lines above; routes cross-checked against
  the backend dispatcher (`__init__.py:2544-2738`) and wave router
  (`abyss_wave.py:1032-1051`).
