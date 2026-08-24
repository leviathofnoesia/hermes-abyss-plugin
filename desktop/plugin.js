/**
 * Abyss Dashboard Plugin — Raindrop-style Observability for Hermes
 * Eight views: Brain (default), Watch (signals & incidents), Health, Activity,
 * Trace, Wave, Search, Calendar.
 *
 * THESIS: An instrument, not a dashboard. The Abyss is a phosphor CRT sitting in
 * a midnight machine room; agent behavior prints itself as a living transcript
 * instead of being dressed in cards and chrome. It refuses the generic hero-
 * metric template and the card-of-cards scaffold of the incumbent.
 * OWN-WORLD: green-phosphor terminal / midnight machine room. Monospace type and
 * tabular numerals throughout, near-black ground, a drifting scanline, a blinking
 * block cursor; states print themselves ("$ abyss --observe", boot lines) rather
 * than appearing as badges. Colors come only from host theme variables.
 * STORY: the operator glances at the pane and the first line answers "are my
 * agents OK right now?"; every symptom (signal, incident, activity row) is one
 * click from its session trace; the Hermes Brain renders as a phosphor instrument.
 * FIRST VIEWPORT: masthead line "$ abyss --observe" over a mono metric strip
 * (ACT/HLTH/INC/CRN/CAT/SIG), then the eight views behind terminal-style tabs;
 * the Brain graph opens by default and is the soul, drawn on canvas with
 * Atkinson dithering.
 * FORM: phosphor-terminal world (concept-seed roll b920409f, fused challenger),
 * committed at full fidelity inside the host theme system.
 * FINISH: unreviewed and undocumented is unfinished; this build ends with the
 * finish review, the verdict, and DESIGN.md.
 *
 * NOTE: This is a SINGLE-FILE plugin because the runtime loader evaluates
 * plugin.js as a Blob URL, so relative imports cannot resolve. All components
 * are inlined below.
 *
 * Hard-won contracts (verified against source, not assumed):
 * - ctx.rest() resolves the PARSED JSON body directly (electron/main.ts
 *   fetchJson → JSON.parse). Never call .json() on the result.
 * - PluginRestOptions has NO `params` field (src/hermes.ts:269). Query strings
 *   go in the path: `/activity?limit=50&category=cron`.
 * - EmptyState takes only {title, description, className} — no icon prop.
 * - Tailwind classes must exist in the host's compiled CSS (the plugin is a
 *   Blob URL, so Tailwind never scans its class strings). Only verified classes
 *   are used; arbitrary `--ui-*` utilities are used only where the compiled
 *   stylesheet actually defines them (bg-(--ui-bg-tertiary/quaternary/quinary/
 *   elevated/chrome/editor), text-(--ui-text-*), border-(--ui-stroke-*), hover:,
 *   placeholder:, bg-(--ui-green)[/10|/70] ...).
 * - TabsContent is NOT exported; render views conditionally on activeTab.
 * - Canvas fillStyle/strokeStyle resolve computed colors, not var() strings.
 *
 * Save to: ~/.hermes/desktop-plugins/abyss/plugin.js
 */
import {
  cn, host, Button, Badge, Codicon, EmptyState, ErrorState,
  SearchField, Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
  Tabs, TabsList, TabsTrigger, GlyphSpinner
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'
import { useEffect, useState, useMemo, useCallback, useRef } from 'react'

const ID = 'abyss'

// ---------------------------------------------------------------------------
// Theme helpers — read computed CSS variable values for canvas (canvas cannot
// resolve var() strings). All colors stay inside the host theme system. The
// fallback is a var() REFERENCE, never a color literal: the artifact ships
// zero hex/rgb values, and in a real browser getComputedStyle always resolves
// the host theme, so the fallback path only matters in exotic environments.
function themeColor(name) {
  try {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
    return v || `var(${name})`
  } catch {
    return `var(${name})`
  }
}
function palette() {
  return {
    session: themeColor('--ui-blue'),
    tool: themeColor('--ui-green'),
    memory: themeColor('--ui-purple'),
    category: themeColor('--ui-orange'),
    task: themeColor('--ui-red'),
    general: themeColor('--ui-text-secondary'),
    accent: themeColor('--ui-accent'),
    stroke: themeColor('--ui-stroke-secondary'),
    strokeDim: themeColor('--ui-stroke-tertiary'),
    ground: themeColor('--ui-bg-editor'),
    surface: themeColor('--ui-bg-elevated')
  }
}

// ---------------------------------------------------------------------------
// Date helpers
// ---------------------------------------------------------------------------
const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December']
function relativeTime(ts) {
  if (!ts) return ''
  const t = new Date(ts).getTime()
  if (Number.isNaN(t)) return ''
  const diff = Date.now() - t
  const abs = Math.abs(diff)
  const future = diff < 0
  if (abs < 45e3) return 'just now'
  const mins = Math.round(abs / 6e4)
  if (mins < 60) return future ? `in ${mins}m` : `${mins}m ago`
  const hrs = Math.round(mins / 60)
  if (hrs < 24) return future ? `in ${hrs}h` : `${hrs}h ago`
  const days = Math.round(hrs / 24)
  if (days < 7) return future ? `in ${days}d` : `${days}d ago`
  // Cross-year honesty: a Dec 2025 entry read in 2026 must not print
  // "Dec 15" as if it were this year's — append the year when it differs.
  const d2 = new Date(ts)
  const opts = { month: 'short', day: 'numeric' }
  if (d2.getFullYear() !== new Date().getFullYear()) opts.year = 'numeric'
  return d2.toLocaleDateString('en-US', opts)
}
// Absolute timestamp for hover titles — complements relativeTime()'s
// "2h ago" voice with the exact moment. The year is appended only when it
// differs (same cross-year honesty as relativeTime); tooltips are
// hover-only, so the fuller date+time is welcome there.
function timeTitle(ts) {
  if (!ts) return ''
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return ''
  const opts = { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', second: '2-digit' }
  if (d.getFullYear() !== new Date().getFullYear()) opts.year = 'numeric'
  return d.toLocaleString('en-US', opts)
}
// Thousands-separator formatting for lifetime counts (tick-47): the strip's
// ACT tile prints total activities ever recorded (25,875 at shift time) and
// SIG prints open signals (3,382) — raw, they read as opaque digit blobs
// ('25875'). en-US grouping matches the dashboard's existing toLocaleString
// locale use. Non-finite/nullish input passes through unchanged so '—' and
// fallbacks survive; sub-1,000 counts are byte-identical so the common
// compact tabular-nums look is untouched.
function fmtCount(n) {
  return (typeof n === 'number' && Number.isFinite(n)) ? n.toLocaleString('en-US') : n
}
// Silence disclosure for the StatusStrip verdict (tick-42): the counts
// ('1073 critical') are lifetime/24h totals — a system whose hooks stopped
// recording (gateway down, plugin misload, session death) still shows big
// numbers, so the verdict must answer the RIGHT question: are agents
// recording RIGHT NOW? Returns null while fresh (<15m — no suffix noise),
// a yellow '· idle 15m' in the quiet window, a red '· idle 5h' once the
// backend's own 30s poll has nothing new for an hour.
function idleLabel(ts) {
  if (!ts) return null
  const t = new Date(ts).getTime()
  if (Number.isNaN(t)) return null
  const diff = Date.now() - t
  if (diff < 0) return null
  const mins = Math.round(diff / 6e4)
  if (mins < 15) return null
  if (mins < 60) return { text: `${mins}m`, tone: 'text-(--ui-yellow)' }
  const hrs = Math.round(mins / 60)
  if (hrs < 24) return { text: `${hrs}h`, tone: 'text-(--ui-red)' }
  const days = Math.round(hrs / 24)
  return { text: `${days}d`, tone: 'text-(--ui-red)' }
}
const getWeekStart = (date = new Date()) => {
  const d = new Date(date)
  d.setDate(d.getDate() - d.getDay())
  d.setHours(0, 0, 0, 0)
  return d
}
const getWeekDays = (date = new Date()) => {
  const start = getWeekStart(date)
  return Array.from({ length: 7 }, (_, i) => {
    const d = new Date(start)
    d.setDate(start.getDate() + i)
    return d
  })
}
const addDays = (date, days) => {
  const d = new Date(date)
  d.setDate(d.getDate() + days)
  return d
}
const isSameDay = (d1, d2) => {
  return d1.getDate() === d2.getDate() &&
    d1.getMonth() === d2.getMonth() &&
    d1.getFullYear() === d2.getFullYear()
}
const formatDateISO = (date) => {
  const y = date.getFullYear()
  const m = String(date.getMonth() + 1).padStart(2, '0')
  const d = String(date.getDate()).padStart(2, '0')
  return `${y}-${m}-${d}`
}
// Incidents store their sessions as `session_ids` (TEXT — possibly a
// comma-separated list or a JSON array); resolve the first usable id.
const firstSessionId = (v) => {
  if (!v) return null
  if (Array.isArray(v)) return v[0] || null
  if (typeof v === 'string') {
    const t = v.trim()
    if (!t || t === '[]' || t === 'null' || t === 'none') return null
    if (t.startsWith('[')) {
      try {
        const arr = JSON.parse(t)
        return Array.isArray(arr) && arr[0] ? String(arr[0]) : null
      } catch (e) { return null }
    }
    return t.split(/[,;]/)[0].trim() || null
  }
  return String(v)
}

// ---------------------------------------------------------------------------
// Phosphor console style — injected once. Every color below is a theme var;
// no hex/rgb literals ship in the artifact.
// ---------------------------------------------------------------------------
const CONSOLE_CSS = `
@keyframes abyss-scan {
  0% { transform: translateY(-100%); }
  100% { transform: translateY(100%); }
}
@keyframes abyss-blink {
  0%, 49% { opacity: 1; }
  50%, 100% { opacity: 0; }
}
@keyframes abyss-flash {
  0% { opacity: 0.3; }
  100% { opacity: 1; }
}
.abyss-scanlines {
  position: absolute;
  inset: 0;
  pointer-events: none;
  overflow: hidden;
  border-radius: inherit;
}
.abyss-scanlines::after {
  content: '';
  position: absolute;
  left: 0;
  right: 0;
  height: 3.5rem;
  top: -4rem;
  background: linear-gradient(180deg, transparent, var(--ui-text-quaternary), transparent);
  animation: abyss-scan 9s linear infinite;
}
.abyss-cursor {
  display: inline-block;
  width: 0.5em;
  height: 1.05em;
  vertical-align: text-bottom;
  margin-left: 2px;
  background: var(--ui-green);
  animation: abyss-blink 1.1s step-end infinite;
}
.abyss-mono { font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, Consolas, monospace); }
.abyss-flash { animation: abyss-flash 0.5s ease-out; }
/* Custom utility classes for Tailwind utilities not compiled in the current
   host CSS build (index-ChgG27Ex.css; the comment's earlier reference to
   index-CPG4RIdW.css went stale across self-updates — see tick-22). These
   cover micro-label sizes and row hover that the plugin relies on but the
   compiled stylesheet doesn't provide. */
.abyss-micro { font-size: 0.65rem; }
.abyss-tiny { font-size: 0.6rem; }
.abyss-row-hover:hover { background-color: var(--ui-bg-tertiary); }
/* Visible focus ring for keyboard-operable surfaces that the compiled
   bundle has no --ui-accent utility for (TraceTimelineView lanes are
   role=button tabIndex=0; the canvases draw their own ring natively).
   Theme-accented per DESIGN.md a11y: "focus rings via theme accent". */
.abyss-focus-ring:focus-visible { outline: 2px solid var(--ui-accent); outline-offset: 1px; }
@media (prefers-reduced-motion: reduce) {
  .abyss-scanlines::after, .abyss-cursor, .abyss-flash { animation: none !important; }
  /* Honor the operator's OS motion preference for host Tailwind utilities
     used inside the plugin too — the CSS above only controls authored
     abyss-* animations; Tailwind's animate-pulse is a host keyframe that
     this media query can't reach, so an operator who asked the OS for less
     motion still got 5 separate skeleton rows pulsing at them. Drop it to a
     flat dim surface instead of a strobing one. */
  .abyss-mute-pulse { animation: none !important; opacity: 0.5; }
}`
let cssInjected = false
function ensureConsoleCss() {
  if (cssInjected) return
  cssInjected = true
  try {
    const style = document.createElement('style')
    style.id = 'abyss-console-css'
    style.textContent = CONSOLE_CSS
    document.head.appendChild(style)
  } catch (e) {
    console.error('abyss: failed to inject console css', e)
  }
}

// ---------------------------------------------------------------------------
// DitherKit: canvas-based graph rendering with Atkinson dithering
// ---------------------------------------------------------------------------
function ditherAtkinson(luma, width, height) {
  const out = new Uint8ClampedArray(luma.length)
  const copy = new Float32Array(luma)
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      const i = y * width + x
      const old = copy[i]
      const newVal = old > 127 ? 255 : 0
      out[i] = newVal
      const err = (old - newVal) / 3
      if (err !== 0) {
        if (x + 1 < width) copy[i + 1] += err
        if (x + 2 < width) copy[i + 2] += err
        if (y + 1 < height) {
          if (x > 0) copy[i + width - 1] += err
          copy[i + width] += err
          if (x + 1 < width) copy[i + width + 1] += err
        }
        if (y + 2 < height) copy[i + width * 2] += err
      }
    }
  }
  return out
}

function forceLayout(nodes, edges, iterations = 300, width = 800, height = 600) {
  const n = nodes.length
  if (n === 0) return []
  const indexById = new Map(nodes.map((node, i) => [node.id, i]))
  const edgeList = []
  for (const edge of edges) {
    const si = indexById.get(edge.source)
    const ti = indexById.get(edge.target)
    if (si !== undefined && ti !== undefined && si !== ti) {
      edgeList.push({ source: si, target: ti, weight: edge.weight || 1 })
    }
  }
  const cx = width / 2, cy = height / 2
  // Seed: an organic ring around the center — avoids the old bias of seeding
  // everywhere at random and letting repulsion explode everything to the walls.
  const radius = Math.min(width, height) * 0.38
  const positions = nodes.map((_, i) => {
    const angle = (i / Math.max(1, n)) * Math.PI * 2
    const r = radius * (0.55 + 0.45 * (((i * 7) % 10) / 10))
    return { x: cx + Math.cos(angle) * r, y: cy + Math.sin(angle) * r, vx: 0, vy: 0 }
  })

  // Obsidian / d3-force style physics (velocity Verlet integrator):
  //   - many-body Coulomb repulsion (charge) keeps nodes apart
  //   - Hooke link springs pull connected nodes together → clusters form
  //   - CENTER force: d3-style forceCenter — translate the graph's centroid
  //     toward the canvas center each step. This PRESERVES spread (makes a
  //     circular/organic graph); an inward pull instead collapses every node
  //     into a tight ball in the middle.
  //   - collision keeps nodes from overlapping
  //   - SOFT boundary eases nodes back inward near the edges — no sticky
  //     hard clamps, so no rectangular wall-sticking
  //   - stability guards (velocity cap + hard escape clamp) so sparse
  //     real-world graphs (many unconnected nodes) can never blow positions
  //     up to ±1e8 and vanish off-canvas
  const alpha = 1
  const alphaDecay = 0.0228
  const velocityDecay = 0.4
  const linkDist = Math.min(width, height) / Math.max(4, Math.sqrt(n))
  const linkStrength = 1.2
  const charge = -160
  const chargeMax = 300
  const collideR = 14
  const velCap = 60
  const posClampMargin = 200
  const centerRate = 0.15

  for (let iter = 0; iter < iterations; iter++) {
    const a = alpha * (1 - Math.pow(1 - alphaDecay, iter + 1))

    for (let i = 0; i < n; i++) {
      for (let j = i + 1; j < n; j++) {
        let dx = positions[i].x - positions[j].x
        let dy = positions[i].y - positions[j].y
        const dist2 = dx * dx + dy * dy
        const dist = Math.sqrt(dist2) || 0.01
        let force = (charge * a) / dist2
        if (force > chargeMax) force = chargeMax
        if (force < -chargeMax) force = -chargeMax
        dx /= dist; dy /= dist
        positions[i].vx += dx * force
        positions[i].vy += dy * force
        positions[j].vx -= dx * force
        positions[j].vy -= dy * force
      }
    }

    for (const edge of edgeList) {
      const s = positions[edge.source]
      const t = positions[edge.target]
      let dx = t.x - s.x
      let dy = t.y - s.y
      const dist = Math.sqrt(dx * dx + dy * dy) || 0.01
      const w = Math.sqrt(edge.weight || 1)
      const force = (linkStrength * a * (dist - linkDist / w)) / dist
      dx *= force; dy *= force
      s.vx += dx; s.vy += dy
      t.vx -= dx; t.vy -= dy
    }

    // d3-style forceCenter: shift the centroid toward the canvas center.
    // Eased so the graph drifts home gently instead of snapping.
    {
      let sx = 0
      let sy = 0

      for (let i = 0; i < n; i++) {
        sx += positions[i].x
        sy += positions[i].y
      }
      const ox = (cx - sx / n) * centerRate
      const oy = (cy - sy / n) * centerRate

      for (let i = 0; i < n; i++) {
        positions[i].x += ox
        positions[i].y += oy
      }
    }

    for (let i = 0; i < n; i++) {
      const p = positions[i]
      for (let j = i + 1; j < n; j++) {
        const q = positions[j]
        let dx = p.x - q.x
        let dy = p.y - q.y
        let dist = Math.sqrt(dx * dx + dy * dy) || 0.01
        if (dist < collideR) {
          const overlap = (collideR - dist) / dist
          dx *= overlap * 0.5; dy *= overlap * 0.5
          p.x += dx; p.y += dy
          q.x -= dx; q.y -= dy
        }
      }
      // Velocity cap — the instability guard (see above).
      p.vx = Math.max(-velCap, Math.min(velCap, p.vx))
      p.vy = Math.max(-velCap, Math.min(velCap, p.vy))
      p.vx *= velocityDecay
      p.vy *= velocityDecay
      p.x += p.vx
      p.y += p.vy

      const margin = 30
      if (p.x < margin) p.x = margin + (margin - p.x) * 0.4
      else if (p.x > width - margin) p.x = (width - margin) - (p.x - (width - margin)) * 0.4
      if (p.y < margin) p.y = margin + (margin - p.y) * 0.4
      else if (p.y > height - margin) p.y = (height - margin) - (p.y - (height - margin)) * 0.4
      // Hard escape clamp — a node that somehow still blows past the soft
      // boundary gets pulled back inside the viewport (never ±1e8 off-screen).
      p.x = Math.max(-posClampMargin, Math.min(width + posClampMargin, p.x))
      p.y = Math.max(-posClampMargin, Math.min(height + posClampMargin, p.y))
    }
  }
  return positions
}

// ---------------------------------------------------------------------------
// Phosphor graph renderer — the incumbent DitherKit graph, re-rendered as a
// phosphor instrument: computed theme colors, dithered ground, soft glow.
// ---------------------------------------------------------------------------
class PhosphorGraphRenderer {
  constructor(canvas, initialData) {
    this.canvas = canvas
    this.ctx = canvas.getContext('2d')
    this.nodes = initialData.nodes || []
    this.edges = initialData.edges || []
    this.scale = 1
    this.offsetX = 0
    this.offsetY = 0
    this.hoveredNode = null
    this.selectedNode = null
    this.draggedNode = null
    this.focused = false
    this.positions = []
    this._bgPattern = null
    // HiDPI (Windows display scaling): the backing store is sized in device
    // pixels while every internal coordinate (layout, offsets, labels) stays
    // in logical CSS px; _render() folds dpr into the transform. Parity with
    // TraceGraphView's DPR-aware canvas.
    this._dpr = (typeof window !== 'undefined' && window.devicePixelRatio) || 1
    // Selection/activation hooks — wired by the React host so the canvas
    // graph can drive the trace drill (and its own aria-label).
    this.onSelect = null
    this.onActivate = null

    this._computeLayout()
    this._generateBackground()
    this._bindEvents()
    this._render()
  }

  _computeLayout() {
    // Logical CSS px only — the backing store may be larger (dpr); physics
    // and seeding must run in the same space as mouse/math coordinates.
    this.positions = forceLayout(this.nodes, this.edges, 260, this.canvas.width / this._dpr, this.canvas.height / this._dpr)
  }

  _generateBackground() {
    const size = 128
    const off = document.createElement('canvas')
    off.width = size
    off.height = size
    const octx = off.getContext('2d')
    if (!octx) return
    const luma = new Uint8ClampedArray(size * size)
    for (let i = 0; i < luma.length; i++) {
      luma[i] = Math.floor(Math.random() * 256 * 0.08 + 256 * 0.92)
    }
    const dithered = ditherAtkinson(luma, size, size)
    const imageData = octx.createImageData(size, size)
    const data = imageData.data
    for (let i = 0; i < dithered.length; i++) {
      const idx = i * 4
      const v = dithered[i]
      data[idx] = v; data[idx + 1] = v; data[idx + 2] = v
      data[idx + 3] = Math.min(v, 10)
    }
    octx.putImageData(imageData, 0, 0)
    this._bgPattern = octx.createPattern(off, 'repeat')
  }

  _bindEvents() {
    const c = this.canvas
    c.addEventListener('mousedown', (e) => {
      c.focus()
      const rect = c.getBoundingClientRect()
      const x = (e.clientX - rect.left) / this.scale - this.offsetX
      const y = (e.clientY - rect.top) / this.scale - this.offsetY
      const hit = this._hitTest(x, y)
      if (hit) {
        this.draggedNode = hit
        this.selectedNode = hit
      } else {
        // Clicking empty ground clears the selection.
        this.selectedNode = null
        this.draggedNode = null
      }
      if (this.onSelect) this.onSelect(this.selectedNode)
    })
    c.addEventListener('mousemove', (e) => {
      const rect = c.getBoundingClientRect()
      const x = (e.clientX - rect.left) / this.scale - this.offsetX
      const y = (e.clientY - rect.top) / this.scale - this.offsetY
      if (this.draggedNode) {
        const idx = this.nodes.findIndex(n => n.id === this.draggedNode.id)
        if (idx !== -1 && this.positions[idx]) {
          this.positions[idx].x = x
          this.positions[idx].y = y
          this._render()
        }
      } else {
        const hit = this._hitTest(x, y)
        if (this.hoveredNode?.id !== hit?.id) {
          this.hoveredNode = hit
          this._render()
        }
      }
    })
    c.addEventListener('mouseup', () => { this.draggedNode = null; this._render() })
    c.addEventListener('wheel', (e) => {
      e.preventDefault()
      this.scale = Math.min(3, Math.max(0.3, this.scale * (e.deltaY > 0 ? 0.9 : 1.1)))
      this._render()
    })
    c.addEventListener('mouseleave', () => {
      this.hoveredNode = null
      this._render()
    })
    // Focus ring: the canvas is focusable (tabIndex 0) with a keyboard
    // contract, so a focused-but-invisible canvas is a dead end for
    // keyboard operators. Track focus and repaint a phosphor dashed ring
    // with the theme accent (DESIGN.md a11y: focus rings via theme accent).
    c.addEventListener('focus', () => { this.focused = true; this._render() })
    c.addEventListener('blur', () => { this.focused = false; this._render() })
    // Drag end OUTSIDE the canvas: releasing the button off-canvas must still
    // drop the dragged node — without this, the node stays glued to the cursor
    // and every later passive mousemove over the canvas teleports it. The
    // listener self-cleans: once this renderer's canvas is detached (remount),
    // its first fire removes it, so replaced renderers never accumulate.
    this._winMouseUp = () => {
      if (!c.isConnected) { window.removeEventListener('mouseup', this._winMouseUp); return }
      if (this.draggedNode) { this.draggedNode = null; this._render() }
    }
    window.addEventListener('mouseup', this._winMouseUp)
    // Keyboard path: the canvas is focusable (tabIndex 0); arrow keys move
    // the selection between nodes, Enter/Space activates (session → trace
    // drill), Escape clears the selection.
    c.addEventListener('keydown', (e) => {
      const dirs = { ArrowUp: [0, -1], ArrowDown: [0, 1], ArrowLeft: [-1, 0], ArrowRight: [1, 0] }
      const d = dirs[e.key]
      if (d) {
        e.preventDefault()
        const next = this._selectByDirection(d[0], d[1])
        if (next) this.selectNode(next)
        return
      }
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault()
        if (this.selectedNode && this.onActivate) this.onActivate(this.selectedNode)
        return
      }
      if (e.key === 'Escape') {
        this.selectNode(null)
      }
    })
  }

  selectNode(node) {
    this.selectedNode = node || null
    if (this.onSelect) this.onSelect(this.selectedNode)
    this._render()
  }

  // Nearest node in the direction half-plane, preferring straight-ahead —
  // the arrow-key selection model for a force-directed graph.
  _selectByDirection(dx, dy) {
    if (!this.nodes.length) return null
    const cur = this.selectedNode
    let curPos = null
    if (cur) {
      const idx = this.nodes.findIndex(n => n.id === cur.id)
      curPos = idx !== -1 ? this.positions[idx] : null
    }
    if (!curPos) {
      curPos = {
        x: (this.canvas.width / this._dpr / 2 - this.offsetX) / this.scale,
        y: (this.canvas.height / this._dpr / 2 - this.offsetY) / this.scale
      }
    }
    let best = null
    let bestScore = Infinity
    for (let i = 0; i < this.nodes.length; i++) {
      const node = this.nodes[i]
      if (cur && node.id === cur.id) continue
      const pos = this.positions[i]
      if (!pos) continue
      const vx = pos.x - curPos.x
      const vy = pos.y - curPos.y
      const len = Math.sqrt(vx * vx + vy * vy) || 1
      const dot = (vx * dx + vy * dy) / len
      if (dot <= 0) continue
      const perp = Math.abs(vx * dy - vy * dx) / len
      const score = perp - dot * 0.5
      if (score < bestScore) { bestScore = score; best = node }
    }
    return best
  }

  _hitTest(x, y) {
    for (let i = 0; i < this.nodes.length; i++) {
      const pos = this.positions[i]
      if (!pos) continue
      const r = this._radius(this.nodes[i])
      const dx = pos.x - x, dy = pos.y - y
      if (Math.sqrt(dx * dx + dy * dy) < r + 8) return this.nodes[i]
    }
    return null
  }

  _radius(node) {
    const d = node.data || {}
    const size = d.activity_count || d.usage_count || d.count || 5
    return Math.min(26, Math.max(6, 6 + size * 0.45))
  }

  _color(node) {
    const p = palette()
    return p[node.type] || p.general
  }

  setData(data) {
    this.nodes = data.nodes || []
    this.edges = data.edges || []
    this.selectedNode = null
    this.hoveredNode = null
    this._computeLayout()
    this._render()
  }

  _render() {
    const ctx = this.ctx
    // Logical CSS-px viewport: the backing store is dpr× larger; fold dpr into
    // the transform so every coordinate below (layout, radii, offsets, label
    // box, dashes, line widths) stays in CSS px. Same pattern as
    // TraceGraphView's DPR-aware canvas.
    const w = this.canvas.width / this._dpr
    const h = this.canvas.height / this._dpr
    ctx.setTransform(this._dpr, 0, 0, this._dpr, 0, 0)
    ctx.clearRect(0, 0, w, h)
    const p = palette()
    if (this._bgPattern) {
      ctx.fillStyle = this._bgPattern
      ctx.fillRect(0, 0, w, h)
    }
    // Focus ring — drawn in logical CSS px (transform is already dpr-scaled).
    if (this.focused) {
      ctx.save()
      ctx.strokeStyle = p.accent
      ctx.lineWidth = 1.5
      ctx.globalAlpha = 0.9
      ctx.setLineDash([4, 3])
      ctx.strokeRect(1, 1, w - 2, h - 2)
      ctx.setLineDash([])
      ctx.restore()
    }
    if (!this.nodes.length) return
    ctx.save()
    ctx.setTransform(this._dpr * this.scale, 0, 0, this._dpr * this.scale, this._dpr * this.offsetX, this._dpr * this.offsetY)
    // Edges — phosphor-dim lines
    for (const edge of this.edges) {
      const si = this.nodes.findIndex(n => n.id === edge.source)
      const ti = this.nodes.findIndex(n => n.id === edge.target)
      if (si === -1 || ti === -1 || !this.positions[si] || !this.positions[ti]) continue
      ctx.lineWidth = Math.min(2.5, 0.5 + (edge.weight || 1) * 0.25) / this.scale
      ctx.setLineDash(edge.type === 'reference' ? [3 / this.scale, 3 / this.scale] : [])
      ctx.strokeStyle = edge.type === 'reference' ? p.accent : p.strokeDim
      ctx.globalAlpha = edge.type === 'reference' ? 0.55 : 1
      ctx.beginPath()
      ctx.moveTo(this.positions[si].x, this.positions[si].y)
      ctx.lineTo(this.positions[ti].x, this.positions[ti].y)
      ctx.stroke()
      ctx.globalAlpha = 1
    }
    ctx.setLineDash([])
    // Nodes — phosphor dots with soft glow on hover/selection
    for (let i = 0; i < this.nodes.length; i++) {
      const node = this.nodes[i]
      const pos = this.positions[i]
      if (!pos) continue
      const r = this._radius(node)
      const hovered = this.hoveredNode?.id === node.id
      const selected = this.selectedNode?.id === node.id
      const color = this._color(node)
      if (hovered || selected) {
        ctx.save()
        ctx.globalAlpha = 0.35
        ctx.fillStyle = color
        ctx.beginPath()
        ctx.arc(pos.x, pos.y, r + 6, 0, Math.PI * 2)
        ctx.fill()
        ctx.restore()
      }
      ctx.fillStyle = color
      ctx.beginPath()
      ctx.arc(pos.x, pos.y, r, 0, Math.PI * 2)
      ctx.fill()
      ctx.globalAlpha = selected ? 1 : 0.6
      ctx.strokeStyle = selected ? p.accent : color
      ctx.lineWidth = selected ? 2 / this.scale : 1 / this.scale
      ctx.stroke()
      ctx.globalAlpha = 1
    }
    // Labels — Obsidian-style: show the node's name on hover/selection so an
    // opaque id (tool:web_search, memory:…) becomes readable.
    const labelNode = this.selectedNode ?? this.hoveredNode
    if (labelNode) {
      try {
        const idx = this.nodes.findIndex(n => n.id === labelNode.id)
        const pos = this.positions[idx]
        if (pos) {
          const r = this._radius(labelNode)
          const label = labelNode.label || labelNode.id
          const color = this._color(labelNode)
          ctx.save()
          ctx.font = `500 ${Math.max(10, 11 / this.scale)}px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace`
          ctx.textAlign = 'left'
          ctx.textBaseline = 'middle'
          const tw = ctx.measureText(label).width
          const pad = 5
          const boxW = tw + pad * 2
          const boxH = 18
          // Label above the node, clamped to the canvas edges.
          const bx = Math.max(4, Math.min(w / this.scale - boxW - 4, pos.x + r + 8))
          const by = Math.max(4, Math.min(h / this.scale - boxH - 4, pos.y - boxH / 2))
          ctx.globalAlpha = 0.92
          ctx.fillStyle = p.surface
          ctx.beginPath()
          ctx.roundRect(bx, by, boxW, boxH, 4)
          ctx.fill()
          ctx.globalAlpha = 1
          ctx.fillStyle = color
          ctx.fillText(label, bx + pad, by + boxH / 2)
          ctx.restore()
        }
      } catch (e) {
        console.warn('[abyss-brain] label render skipped:', e)
      }
    }
    ctx.restore()
  }
}

// ==================== COMPONENTS ====================

// --- Masthead + status strip (health at a glance) ---
function StatusStrip({ ctx, onNavigate }) {
  const [stats, setStats] = useState(null)
  const [status, setStatus] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  // One authored motion moment: the metric strip re-prints (brief opacity
  // flash, ease-out) only when the health-relevant numbers actually change.
  const [flashTick, setFlashTick] = useState(0)
  const prevHealthRef = useRef(null)

  const fetchAll = useCallback(async () => {
    if (!ctx) return
    setLoading(true)
    setError(null)
    try {
      // Real totals come from /status (signals_open + severity breakdown).
      // Do NOT derive them by sampling /signals?limit=50 — that undercounted
      // 800+ open signals as "50 SIG / 43 critical".
      const [s, st] = await Promise.all([
        ctx.rest('/stats', { method: 'GET', timeoutMs: 5000 }),
        ctx.rest('/status', { method: 'GET', timeoutMs: 5000 })
      ])
      const prev = prevHealthRef.current
      const sig = st?.signals_open ?? 0
      const score = st?.score ?? null
      if (prev && (prev.sig !== sig || prev.score !== score)) setFlashTick(t => t + 1)
      prevHealthRef.current = { sig, score }
      setStats(s || null)
      setStatus(st && typeof st === 'object' ? st : null)
    } catch (e) {
      console.error('abyss: status fetch failed', e)
      // Never masquerade a dead backend as "all clear" — say so, with retry.
      setError('status link down')
      setStats(null)
      setStatus(null)
    } finally {
      setLoading(false)
    }
  }, [ctx])

  useEffect(() => {
    fetchAll()
    const t = setInterval(fetchAll, 30000)
    return () => clearInterval(t)
  }, [fetchAll])

  const openSignals = status?.signals_open ?? 0
  const criticals = (status?.signals_critical ?? 0) + (status?.signals_error ?? 0)
  // Silence disclosure: how long since ANY activity was recorded. The
  // counts above can look healthy while hooks silently stopped (tick-42).
  const idle = idleLabel(status?.last_activity_at)
  const idleEl = idle ? jsx('span', {
    className: cn('shrink-0 abyss-tiny uppercase tracking-widest', idle.tone),
    title: `last recorded activity ${timeTitle(status.last_activity_at)} — hooks may have stopped firing`,
    children: `· idle ${idle.text}`
  }) : null

  // In-flight remediation disclosure (tick-44): /status aggregates how many
  // cloud-agent fixes are running RIGHT NOW (signals + incidents with
  // resolution_status='running' — the value the Watch tab's 8s poll also
  // watches, but aggregated backend-side so a resolution on row 60 of a
  // 50-row fetch still counts). The glance's verdict is "are my agents OK?";
  // an active doctor/resolver is part of that answer — the operator must not
  // have to open Watch to learn a fix is in flight, and on dark hooks
  // ("idle 5h") a still-running resolution is the one sign of life left.
  const resolvingCount = status?.resolutions_running ?? 0
  const resolvingEl = resolvingCount > 0 ? jsx('span', {
    // No compiled text-(--ui-blue) class exists in the host bundle (only
    // red/yellow/green/accent do), so the tone is inline var(), matching the
    // Calendar "running" glyph convention (DESIGN.md: inline styles for
    // values with no compiled class).
    className: 'shrink-0 abyss-tiny uppercase tracking-widest',
    style: { color: 'var(--ui-blue)' },
    title: `${resolvingCount} cloud-agent fix${resolvingCount === 1 ? '' : 'es'} in flight — resolving rows update every 8s`,
    children: `· ${resolvingCount} resolving`
  }) : null

  const healthScore = status?.score
  const healthTone = (status?.level === 'critical') ? 'text-(--ui-red)'
    : (status?.level === 'degraded' || status?.level === 'fair') ? 'text-(--ui-yellow)'
    : 'text-(--ui-green)'

  const items = [
    // fmtCount (tick-47): lifetime totals (ACT/SIG) exceed four digits —
    // group them. HLTH stays raw: a 0–100 score never needs a separator.
    { label: 'ACT', value: fmtCount(stats?.total_activities ?? 0), tone: 'text-(--ui-text-primary)', hint: 'total activity entries recorded' },
    { label: 'HLTH', value: healthScore ?? '—', tone: healthScore != null ? healthTone : 'text-(--ui-text-tertiary)', nav: 'health', hint: 'current health score (0–100) · click to open health' },
    { label: 'INC', value: fmtCount(status?.incidents_open ?? 0), tone: (status?.incidents_open ?? 0) > 0 ? 'text-(--ui-yellow)' : 'text-(--ui-text-primary)', hint: 'open clustered incidents' },
    { label: 'CRN', value: fmtCount(stats?.cron_jobs ?? 0), tone: 'text-(--ui-text-primary)', hint: 'active cron jobs tracked' },
    { label: 'CAT', value: fmtCount(stats?.categories ? Object.keys(stats.categories).length : 0), tone: 'text-(--ui-text-primary)', hint: 'distinct activity categories recorded' },
    { label: 'SIG', value: fmtCount(openSignals), tone: criticals > 0 ? 'text-(--ui-red)' : openSignals > 0 ? 'text-(--ui-yellow)' : 'text-(--ui-green)', nav: 'signals', hint: 'open signals (silent agent failures) · click to open watch' }
  ]

  if (loading && !stats && !status) {
    // Skeleton on the FIRST load only — every 30s background poll flips
    // `loading` back to true momentarily, and without this guard the strip
    // punches out to a blank skeleton row every half minute instead of
    // silently refreshing in place (the authored flash tick already announces
    // a real change). Returning the prior data here keeps the glance steady.
    return jsx('div', {
      className: 'shrink-0 px-3 py-2 border-b border-(--ui-stroke-tertiary)',
      children: jsx('div', { className: 'h-7 w-full bg-(--ui-bg-tertiary) rounded animate-pulse abyss-mute-pulse' })
    })
  }

  if (error && !stats && !status) {
    return jsxs('div', {
      className: 'shrink-0 px-3 py-1.5 border-b border-(--ui-stroke-tertiary) flex items-center gap-2',
      children: [
        jsx('span', { className: 'inline-block h-1.5 w-1.5 rounded-full', style: { backgroundColor: 'var(--ui-red)' }, children: '' }),
        jsx('span', { className: 'text-xs text-(--ui-red) abyss-mono', children: 'status link down' }),
        jsx(Button, {
          variant: 'ghost', size: 'xs', className: 'text-xs abyss-mono',
          onClick: fetchAll,
          children: 'retry'
        }),
        // Screen-reader parity with the data branch below (tick-27): the
        // 'status link down' line (with its retry affordance) is all visual —
        // a screen-reader operator gets zero announcement that the answer to
        // "are my agents OK right now?" just became UNKNOWABLE. Echo it in
        // the same polite live region so the failure state is spoken when it
        // flips, never silently lost in the strip.
        jsx('span', {
          role: 'status',
          'aria-live': 'polite',
          className: 'sr-only',
          children: 'abyss health: status link down'
        })
      ]
    })
  }

  return jsxs('div', {
    className: 'shrink-0 px-3 py-1.5 border-b border-(--ui-stroke-tertiary) flex items-center gap-4',
    children: [
      jsx('span', { className: 'text-xs text-(--ui-text-tertiary) abyss-mono select-none shrink-0', title: 'auto-refreshes every 30s', children: 'live:' }),
      jsxs('div', {
        key: flashTick,
        className: 'abyss-flash flex items-center gap-4 flex-wrap min-w-0 flex-1',
        children: [
          items.map(item => {
            const metric = [
              // The strip is a row of bare acronyms (ACT/HLTH/INC/CRN/CAT/SIG)
              // — the very first thing an operator sees (DESIGN.md FIRST
              // VIEWPORT). Each value now carries a hint title so the meaning
              // is one hover away instead of living only in the docs; the
              // same title works inside the SIG/HLTH nav buttons.
              jsx('span', { className: cn('text-xs font-semibold abyss-mono tabular-nums', item.tone), title: item.hint, children: item.value }),
              jsx('span', { className: 'abyss-micro uppercase tracking-widest text-(--ui-text-quaternary)', children: item.label })
            ]
            // The glance must connect to the action: SIG and HLTH are live
            // jump-points into the watch / health views (drill-don't-rediscover).
            if (item.nav && onNavigate) {
              return jsx(Button, {
                key: item.label,
                variant: 'ghost', size: 'xs',
                onClick: () => onNavigate(item.nav),
                title: `open ${item.nav} view`,
                'aria-label': `Open ${item.nav} view`,
                className: 'flex items-baseline gap-1 h-6 px-1',
                children: metric
              })
            }
            return jsxs('span', { key: item.label, className: 'flex items-baseline gap-1', children: metric })
          }),
          // Verdict gets a reserved, non-wrapping slot on the strip's right
          // edge; the metric row wraps beneath it in a narrow pane.
          onNavigate ? jsx(Button, {
            variant: 'ghost', size: 'xs',
            onClick: () => onNavigate('signals'),
            title: 'open watch view',
            'aria-label': 'Open watch view',
            className: 'ml-auto shrink-0 whitespace-nowrap flex items-center gap-1.5 text-xs text-(--ui-text-tertiary) abyss-mono h-6 px-1.5',
            children: [
              jsx('span', {
                className: 'inline-block h-1.5 w-1.5 rounded-full',
                style: { backgroundColor: criticals > 0 ? 'var(--ui-red)' : openSignals > 0 ? 'var(--ui-yellow)' : 'var(--ui-green)' }
              }),
              // fmtCount (tick-47): criticals/open can exceed four digits.
              criticals > 0 ? `${fmtCount(criticals)} critical` : openSignals > 0 ? `${fmtCount(openSignals)} open` : 'all clear',
              idleEl,
              resolvingEl,
              '›'
            ]
          }) : jsxs('span', {
            className: 'ml-auto shrink-0 whitespace-nowrap flex items-center gap-1.5 text-xs text-(--ui-text-tertiary) abyss-mono',
            children: [
              jsx('span', {
                className: 'inline-block h-1.5 w-1.5 rounded-full',
                style: { backgroundColor: criticals > 0 ? 'var(--ui-red)' : openSignals > 0 ? 'var(--ui-yellow)' : 'var(--ui-green)' }
              }),
              criticals > 0 ? `${fmtCount(criticals)} critical` : openSignals > 0 ? `${fmtCount(openSignals)} open` : 'all clear',
              idleEl,
              resolvingEl
            ]
          }),
          // Screen-reader parity for the glance: the strip's flash
          // (flashTick) is a purely VISUAL announcement that health numbers
          // changed — a screen-reader operator gets nothing. This sr-only
          // live region (role=status → polite announcement, not an alert)
          // echoes the verdict phrase so the "are my agents OK?" answer is
          // spoken exactly when it changes. sr-only verified compiled in
          // index-ChgG27Ex.css.
          jsx('span', {
            role: 'status',
            'aria-live': 'polite',
            className: 'sr-only',
            children: `abyss health: ${criticals > 0 ? `${fmtCount(criticals)} critical` : openSignals > 0 ? `${fmtCount(openSignals)} open` : 'all clear'}${idle ? ` · idle ${idle.text}` : ''}${resolvingCount > 0 ? ` · ${resolvingCount} resolving` : ''}`
          })
        ]
      })
    ]
  })
}

function Masthead() {
  return jsxs('div', {
    className: 'shrink-0 px-3 pt-2 pb-1.5 border-b border-(--ui-stroke-tertiary) relative overflow-hidden',
    children: [
      jsx('div', { className: 'abyss-scanlines' }),
      jsxs('div', {
        className: 'flex items-center justify-between',
        children: [
          jsxs('div', {
            className: 'flex items-baseline gap-2',
            children: [
              jsx('span', {
                className: 'text-sm font-bold tracking-widest text-(--ui-text-primary) abyss-mono uppercase',
                children: 'abyss'
              }),
              jsx('span', { className: 'abyss-cursor' })
            ]
          }),
          jsxs('div', {
            className: 'flex items-center gap-2',
            children: [
              jsx('span', { className: 'text-xs text-(--ui-text-tertiary) abyss-mono min-w-0 truncate', title: '$ ./abyss --observe --local --cloud-fix', children: '$ ./abyss --observe --local --cloud-fix' }),
              jsx(Button, {
                variant: 'ghost',
                size: 'sm',
                className: 'h-7 w-7 px-0',
                onClick: () => { try { host.navigate('/') } catch { } },
                title: 'Close Abyss dashboard',
                'aria-label': 'Close Abyss dashboard',
                children: jsx(Codicon, { name: 'close', className: 'text-(--ui-text-tertiary)' })
              })
            ]
          })
        ]
      }),
      jsx('div', { className: 'mt-0.5 abyss-micro text-(--ui-text-quaternary) abyss-mono truncate', title: 'self-diagnostics · signal detection · incident clustering · hermès brain', children: 'self-diagnostics · signal detection · incident clustering · hermès brain' })
    ]
  })
}

// --- Activity Feed ---
function ActivityFeed({ ctx, onOpenTrace }) {
  const [activities, setActivities] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [filter, setFilter] = useState('all')
  // Out-of-order guard: the 30s poll plus manual filter switches can put two
  // fetches in flight; a slow response for the OLD filter must never overwrite
  // rows for the NEW one.
  const fetchSeqRef = useRef(0)
  // Filter-honesty guard: tracks which category the visible rows actually
  // belong to. The 30s poll intentionally keeps cached rows while it runs (no
  // blink, tick-2), but a MANUAL filter switch must never leave the previous
  // category's rows underneath the newly-highlighted filter button — the
  // skeleton shows (and a failed switch fetch surfaces ErrorState) until the
  // new filter's fetch actually lands. Poll failures keep the ref matched
  // (same filter), so the preserve-on-blip policy is unchanged.
  const loadedFilterRef = useRef('all')

  const fetchActivities = useCallback(async () => {
    if (!ctx) return
    const seq = ++fetchSeqRef.current
    setLoading(true)
    setError(null)
    try {
      const q = filter !== 'all' ? `?limit=50&category=${encodeURIComponent(filter)}` : '?limit=50'
      const data = await ctx.rest(`/activity${q}`, { method: 'GET', timeoutMs: 5000 })
      if (seq !== fetchSeqRef.current) return // stale — a newer fetch is in flight
      setActivities(Array.isArray(data) ? data : [])
      loadedFilterRef.current = filter
    } catch (e) {
      console.error('Failed to fetch activity:', e)
      if (seq !== fetchSeqRef.current) return
      // A filter-SWITCH fetch that fails must not leave the previous
      // category's rows misleadingly under the new filter button — drop them
      // so the ErrorState below surfaces (errors never masquerade as data
      // of a different slice). Same-filter background-poll failures keep the
      // cached list — a transient network blip every 30s must not blank the
      // feed the operator is reading; the ErrorState only renders when the
      // prior data is also gone (below).
      if (loadedFilterRef.current !== filter) setActivities([])
      setError(String(e?.message || e))
    } finally {
      if (seq === fetchSeqRef.current) setLoading(false)
    }
  }, [ctx, filter])

  useEffect(() => {
    fetchActivities()
    const t = setInterval(fetchActivities, 30000)
    return () => clearInterval(t)
  }, [fetchActivities])

  // Only categories the backend actually records. The old list
  // (cron/task/command) matched zero rows — clicking those filters returned
  // "No activity yet" for categories that never exist.
  const categories = ['all', 'tool', 'llm', 'session', 'system']

  const categoryTone = {
    tool: { color: 'var(--ui-cyan)' },
    llm: { color: 'var(--ui-blue)' },
    session: { color: 'var(--ui-purple)' },
    system: { color: 'var(--ui-orange)' },
    cron: { color: 'var(--ui-blue)' },
    task: { color: 'var(--ui-green)' },
    command: { color: 'var(--ui-cyan)' },
    general: { color: 'var(--ui-text-secondary)' }
  }
  const categoryStyle = (cat) => categoryTone[cat] || categoryTone.general

  // Skeleton on initial load only (no cached activities yet) OR while a
  // manual filter switch's fetch is in flight (the cached rows belong to a
  // different category — the new filter button must never sit above stale
  // rows of the old slice; tick-31 filter-honesty). The 30s poll flipping
  // loading=true does NOT punch the existing rows out to blank bars — the ref
  // still matches, so the row list silently refreshes in place.
  if (loading && (activities.length === 0 || loadedFilterRef.current !== filter)) {
    return jsx('div', {
      className: 'p-3',
      children: jsxs('div', { className: 'space-y-2', children: Array.from({ length: 5 }).map((_, i) =>
        jsx('div', { key: i, className: 'h-12 w-full bg-(--ui-bg-tertiary) rounded animate-pulse abyss-mute-pulse' })
      ) })
    })
  }

  if (error && activities.length === 0) {
    return jsx(ErrorState, {
      title: 'Activity unavailable',
      description: error,
      children: jsx(Button, { variant: 'secondary', size: 'sm', onClick: fetchActivities, children: 'Retry' })
    })
  }

  if (activities.length === 0) {
    // Filter-aware empty copy: "no activity yet" is a lie when a specific
    // category filter is active but simply has no rows — the operator asked
    // for a slice, not the whole store (DESIGN.md States: empty states tell
    // the truth about what the operator is looking at).
    return jsx(EmptyState, {
      title: filter === 'all' ? 'No activity yet' : `No ${filter} activity`,
      description: filter === 'all'
        ? 'Activity entries will appear here as you work.'
        : `No ${filter} entries recorded in the recent window.`
    })
  }

  return jsxs('div', {
    className: 'flex h-full flex-col',
    children: [
      jsxs('div', {
        className: 'flex gap-1 px-3 py-2 border-b border-(--ui-stroke-tertiary) overflow-x-auto',
        children: [
          ...categories.map(cat =>
            jsx(Button, {
              key: cat,
              variant: filter === cat ? 'default' : 'ghost',
              size: 'sm',
              onClick: () => setFilter(cat),
              'aria-pressed': filter === cat,
              'aria-label': `Filter by ${cat === 'all' ? 'all categories' : cat}`,
              className: 'text-xs h-7 whitespace-nowrap abyss-mono',
              children: cat === 'all' ? 'all' : cat
            })
          ),
          // Cap disclosure (tick-35 counter-honesty parity with the Watch
          // tabs): /activity is fetched with limit=50, so a feed pinned at
          // exactly 50 rows usually means MORE exist beyond the visible
          // sample. The strip's ACT metric carries the exact recorded total,
          // so the marker just says where the full count lives — a bare
          // "50 rows" next to ACT "1,204" would be a false total.
          activities.length >= 50 && jsx('span', {
            className: 'ml-auto shrink-0 abyss-micro abyss-mono text-(--ui-text-quaternary) whitespace-nowrap',
            title: 'showing the most recent 50 entries — the ACT metric in the strip shows the exact total',
            children: '50+'
          })
        ]
      }),
      jsx('div', {
        className: 'flex-1 overflow-y-auto',
        children: jsxs('div', {
          className: 'flex flex-col',
          children: activities.map((entry, idx) =>
            jsxs('div', {
              key: entry.id,
              className: cn(
                'px-3 py-2 flex items-start gap-2.5 abyss-row-hover',
                idx < activities.length - 1 && 'border-b border-(--ui-stroke-tertiary)'
              ),
              children: [
                jsx('span', {
                  className: 'abyss-micro tabular-nums abyss-mono mt-0.5 select-none',
                  style: categoryStyle(entry.category),
                  children: '▸'
                }),
                jsxs('div', {
                  className: 'flex-1 min-w-0',
                  children: [
                    jsxs('div', {
                      className: 'flex items-center gap-2',
                      children: [
                        jsx('span', { className: 'text-sm font-medium text-(--ui-text-primary) truncate min-w-0 abyss-mono', title: entry.action, children: entry.action }),
                        jsx('span', { className: 'abyss-micro tabular-nums text-(--ui-text-quaternary) abyss-mono whitespace-nowrap shrink-0', title: timeTitle(entry.timestamp), children: relativeTime(entry.timestamp) })
                      ]
                    }),
                    entry.description && jsx('div', {
                      className: 'text-xs text-(--ui-text-secondary) mt-0.5 truncate abyss-mono',
                      title: entry.description || undefined,
                      children: entry.description
                    }),
                    jsxs('div', {
                      className: 'flex items-center gap-2 mt-1',
                      children: [
                        entry.category && jsx(Badge, {
                          variant: 'muted', size: 'xs', className: 'uppercase tracking-wider',
                          children: entry.category
                        }),
                        entry.status && jsx(Badge, {
                          variant: entry.status === 'error' ? 'destructive' : entry.status === 'running' ? 'warn' : 'default',
                          size: 'xs',
                          children: entry.status
                        }),
                        entry.session_id && jsx('span', {
                          className: 'abyss-micro text-(--ui-text-quaternary) abyss-mono',
                          title: entry.session_id,
                          children: `sid ${entry.session_id.slice(0, 8)}`
                        }),
                        entry.session_id && onOpenTrace && jsx(Button, {
                          variant: 'ghost', size: 'xs',
                          onClick: () => onOpenTrace(entry.session_id),
                          title: 'Open this session trace',
                          'aria-label': `Open trace for session ${entry.session_id.slice(0, 8)}`,
                          className: 'abyss-tiny abyss-mono h-6 px-1.5',
                          children: 'trace ›'
                        })
                      ]
                    })
                  ]
                })
              ]
            })
          )
        })
      })
    ]
  })
}

// --- Calendar View ---
function CalendarView({ ctx, onOpenTrace }) {
  const [currentWeek, setCurrentWeek] = useState(new Date())
  const [tasks, setTasks] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  // Out-of-order guard: rapid week navigation puts several fetches in flight;
  // a slow response for an OLD week must never paint its tasks into the NEW
  // week's grid (tasks would appear on the wrong days).
  const fetchSeqRef = useRef(0)
  // Week-honesty guard: tracks which week's tasks the visible grid actually
  // belongs to. A 30s background poll must never punch the operator's grid
  // out to skeleton (no-blink, tick-2 policy), but a WEEK-SWITCH fetch must
  // never leave the previous week's tasks painted under the new week's
  // header — the skeleton shows (and a failed switch fetch surfaces
  // ErrorState, dropping the stale rows) until the new week's fetch lands.
  const loadedWeekRef = useRef(null)

  const weekStart = getWeekStart(currentWeek)
  const weekEnd = addDays(weekStart, 6)
  // Affordance const for the 'today' jump (tick-41): whether the visible
  // week already IS the current week. The tick-21 guard made a no-op click
  // silent — the button now disables instead so 'you are already here' is
  // visible, not a dead-looking click target.
  const isCurrentWeekShown = getWeekStart(currentWeek).getTime() === getWeekStart(new Date()).getTime()

  const fetchTasks = useCallback(async () => {
    if (!ctx) return
    const seq = ++fetchSeqRef.current
    setLoading(true)
    setError(null)
    try {
      const startISO = weekStart.toISOString()
      const endISO = addDays(weekEnd, 1).toISOString()
      const data = await ctx.rest(`/calendar?start=${encodeURIComponent(startISO)}&end=${encodeURIComponent(endISO)}`, {
        method: 'GET',
        timeoutMs: 5000
      })
      if (seq !== fetchSeqRef.current) return // stale — the operator moved to another week
      setTasks(Array.isArray(data) ? data : [])
      loadedWeekRef.current = startISO
    } catch (e) {
      console.error('Failed to fetch calendar:', e)
      if (seq !== fetchSeqRef.current) return
      // A week-SWITCH fetch that fails must not leave the previous week's
      // tasks misleadingly under the new week's header — drop them so the
      // ErrorState below surfaces (errors never masquerade as data of a
      // different week). Same-week background-poll failures keep the cached
      // grid — a transient network blip every 30s must not blank the
      // calendar the operator is reading; the ErrorState only renders when
      // the cache is also gone (below).
      if (loadedWeekRef.current !== startISO) setTasks([])
      setError(String(e?.message || e))
    } finally {
      if (seq === fetchSeqRef.current) setLoading(false)
    }
  }, [ctx, currentWeek])

  useEffect(() => {
    // Live calendar: re-poll the visible week every 30s (cadence parity with
    // StatusStrip/ActivityFeed/Watch/Wave/Trace-agent surfaces) so a task
    // scheduled by a running cron appears without remounting the view. The
    // loading guard below only renders the skeleton when the cached grid is
    // also gone (or the week changed), so the background poll silently
    // refreshes the rows in place.
    fetchTasks()
    const t = setInterval(fetchTasks, 30000)
    return () => clearInterval(t)
  }, [fetchTasks])

  const weekDays = getWeekDays(currentWeek)

  const tasksByDay = useMemo(() => {
    const grouped = {}
    weekDays.forEach(day => { grouped[formatDateISO(day)] = [] })
    tasks.forEach(task => {
      const taskDate = task.timestamp || task.next_run
      if (!taskDate) return
      const taskDay = new Date(taskDate)
      // Exclusive upper bound: weekEnd is Saturday 00:00, so a plain `<=`
      // silently dropped every task later that day — the whole LAST column
      // of the week grid lost its entries. Compare against the start of the
      // following day instead.
      if (taskDay >= weekStart && taskDay < addDays(weekEnd, 1)) {
        const dayKey = formatDateISO(taskDay)
        if (grouped[dayKey]) {
          const exists = grouped[dayKey].some(t => t.id === task.id)
          if (!exists) grouped[dayKey].push(task)
        }
      }
    })
    return grouped
  }, [tasks, weekDays])

  const taskTone = {
    completed: 'var(--ui-green)',
    running: 'var(--ui-blue)',
    pending: 'var(--ui-yellow)',
    cron: 'var(--ui-blue)',
    tool: 'var(--ui-green)',
    task: 'var(--ui-green)',
    session: 'var(--ui-purple)',
    system: 'var(--ui-orange)',
    command: 'var(--ui-cyan)',
    general: 'var(--ui-text-secondary)'
  }

  // Skeleton on initial load only (no cached grid yet) OR while a
  // week-SWITCH fetch is in flight (the cached grid belongs to a different
  // week — the new week's header must never sit above the old week's rows;
  // tick-32 week-honesty). The 30s poll flipping loading=true does NOT punch
  // the existing grid out to blank cells — the ref still matches, so the
  // calendar silently refreshes in place.
  if (loading && (tasks.length === 0 || loadedWeekRef.current !== weekStart.toISOString())) {
    return jsx('div', { className: 'p-3', children: jsxs('div', {
      style: { gridTemplateColumns: 'repeat(7, minmax(0, 1fr))' },
      className: 'grid gap-px bg-(--ui-stroke-tertiary)',
      children: Array.from({ length: 7 }).map((_, i) =>
        jsx('div', { key: i, className: 'bg-(--ui-bg-elevated) p-1', style: { minHeight: 72 }, children: jsx('div', { className: 'h-12 w-full bg-(--ui-bg-tertiary) rounded animate-pulse abyss-mute-pulse' }) })
      )
    }) })
  }

  // Same-week poll failures keep the cached grid (ErrorState only when the
  // cache is also gone — a transient 30s blip must never blank the calendar
  // the operator is reading; the next successful poll clears the error).
  if (error && tasks.length === 0) {
    return jsx(ErrorState, {
      title: 'Calendar unavailable',
      description: error,
      children: jsx(Button, { variant: 'secondary', size: 'sm', onClick: fetchTasks, children: 'Retry' })
    })
  }

  return jsxs('div', {
    className: 'flex h-full flex-col',
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between px-3 py-2 border-b border-(--ui-stroke-tertiary)',
        children: [
          jsx(Button, {
            variant: 'ghost', size: 'sm',
            onClick: () => setCurrentWeek(addDays(currentWeek, -7)),
            title: 'Previous week',
            'aria-label': 'Previous week',
            children: jsx(Codicon, { name: 'chevron-left' })
          }),
          jsxs('div', {
            className: 'flex items-center gap-2 abyss-mono',
            children: jsx('span', {
              className: 'text-sm font-medium uppercase tracking-wider text-(--ui-text-primary) abyss-mono',
              children: weekStart.getMonth() === weekEnd.getMonth()
                ? `${MONTHS[weekStart.getMonth()]} ${weekStart.getDate()} – ${weekEnd.getDate()}, ${weekStart.getFullYear()}`
                : `${MONTHS[weekStart.getMonth()].slice(0, 3)} ${weekStart.getDate()} – ${MONTHS[weekEnd.getMonth()].slice(0, 3)} ${weekEnd.getDate()}, ${weekStart.getFullYear()}${weekEnd.getFullYear() !== weekStart.getFullYear() ? '–' + weekEnd.getFullYear() : ''}`
            })
          }),
          jsx(Button, {
            variant: 'ghost', size: 'sm',
            onClick: () => setCurrentWeek(addDays(currentWeek, 7)),
            title: 'Next week',
            'aria-label': 'Next week',
            children: jsx(Codicon, { name: 'chevron-right' })
          })
        ]
      }),
      jsx('div', {
        className: 'px-2 py-1 border-b border-(--ui-stroke-tertiary)',
        children: jsx(Button, {
          variant: 'ghost', size: 'sm', className: 'text-xs abyss-mono',
          // Tick-41 affordance: when the visible grid is already the current
          // week, the jump is a no-op — disable the button (title explains)
          // instead of letting the click die silently (tick-21 guard kept
          // behind the disabled state, so the no-op-refetch protection holds).
          disabled: isCurrentWeekShown,
          title: isCurrentWeekShown ? 'already showing the current week' : 'Jump to the current week',
          onClick: () => {
            // Jump only when actually off the current week: a fresh Date()
            // identity would re-fire fetchTasks and flash the whole grid to
            // skeleton even when the operator is already on this week.
            const now = new Date()
            if (getWeekStart(currentWeek).getTime() !== getWeekStart(now).getTime()) setCurrentWeek(now)
          },
          children: 'today'
        })
      }),
      jsx('div', {
        className: 'flex-1 overflow-auto',
        children: tasks.length === 0
          ? jsx(EmptyState, {
              title: 'No tasks this week',
              description: 'Nothing scheduled in this range yet. Use the arrows to look around, or “today” to jump back.'
            })
          : jsxs('div', {
          className: 'grid gap-px bg-(--ui-stroke-tertiary) text-xs',
          style: { gridTemplateColumns: 'repeat(7, minmax(0, 1fr))' },
          children: [
            WEEKDAYS.map(day =>
              jsx('div', {
                key: day,
                // abyss-mono: the Sunday–Saturday column headers are the
                // calendar grid's micro-label row — DESIGN.md Type mandates
                // the phosphor stack throughout, and the day numbers below
                // them already carry abyss-mono; without it the header row
                // rendered in the host sans face, a type-stack seam inside
                // the grid (tick-30 HealthView parity).
                className: 'bg-(--ui-bg-quaternary) px-1 py-1 text-center font-medium text-(--ui-text-tertiary) uppercase tracking-wider abyss-tiny abyss-mono',
                children: day
              })
            ),
            weekDays.map((day, dayIdx) => {
              const dayKey = formatDateISO(day)
              const dayTasks = tasksByDay[dayKey] || []
              const isToday = isSameDay(day, new Date())
              const isCurrentMonth = day.getMonth() === currentWeek.getMonth()

              return jsxs('div', {
                key: dayKey,
                className: cn(
                  'bg-(--ui-bg-elevated) p-1 abyss-row-hover',
                  !isCurrentMonth && 'opacity-40',
                  isToday && 'ring-1 ring-(--ui-stroke-secondary)'
                ),
                style: { minHeight: 72 },
                children: [
                  jsx('div', {
                    className: 'flex justify-between items-center',
                    children: [
                      jsx('span', {
                        className: cn(
                          'text-xs tabular-nums abyss-mono',
                          isToday && 'font-bold text-(--ui-accent)',
                          !isCurrentMonth && 'text-(--ui-text-tertiary)'
                        ),
                        title: `${MONTHS[day.getMonth()].slice(0, 3)} ${day.getDate()}, ${day.getFullYear()}${isToday ? ' · today' : ''}${"\n"}${dayIdx === 0 ? 'week starts' : dayIdx === 6 ? 'week ends' : ''}`,
                        children: day.getDate()
                      }),
                      dayTasks.length > 0 && jsx(Badge, {
                        variant: 'outline', size: 'xs', className: 'abyss-tiny h-4 min-w-4 justify-center rounded-full',
                        children: dayTasks.length
                      })
                    ]
                  }),
                  dayTasks.length > 0 ? jsxs('div', {
                    className: 'mt-1 space-y-0.5',
                    children: dayTasks.slice(0, 2).map(task => {
                      const tone = taskTone[task.status] || taskTone[task.category] || taskTone.general
                      // Glyph, not just colour: completed/running/pending stay
                      // distinguishable without colour vision.
                      const glyph = task.status === 'completed' ? '✓' : task.status === 'running' ? '▶' : task.status === 'pending' ? '○' : '·'
                      return jsxs('div', {
                        key: task.id,
                        className: 'flex items-center gap-1 min-w-0',
                        title: `${task.status || task.category || 'task'}: ${task.title || task.action || ''}`,
                        children: [
                          jsx('span', { className: 'shrink-0 abyss-tiny abyss-mono', style: { color: tone }, children: glyph }),
                          jsx('span', { className: 'abyss-micro truncate min-w-0', style: { color: tone }, children: task.title || task.action || '' }),
                          task.session_id && onOpenTrace && jsx(Button, {
                            variant: 'ghost', size: 'xs',
                            onClick: () => onOpenTrace(task.session_id),
                            title: 'Open this session trace',
                            'aria-label': `Open trace for session ${task.session_id.slice(0, 8)}`,
                            className: 'abyss-tiny abyss-mono h-5 px-1 ml-auto shrink-0',
                            children: 'trace ›'
                          })
                        ]
                      })
                    })
                  }) : null,
                  dayTasks.length > 2 && jsx('div', {
                    className: 'abyss-tiny text-(--ui-text-tertiary) mt-0.5 abyss-mono',
                    title: dayTasks.slice(2).map(t => t.title || t.action || t.id || 'task').join(' · '),
                    children: `+${dayTasks.length - 2} more`
                  })
                ]
              })
            })
          ]
        })
      })
    ]
  })
}

// --- Global Search ---
function GlobalSearch({ ctx, onOpenTrace }) {
  const [query, setQuery] = useState('')
  const [results, setResults] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [selectedSources, setSelectedSources] = useState({
    memory: true, sessions: true, kanban: true, activity: true
  })
  // Out-of-order guard: a slow response for an older query must never
  // overwrite results for the current one (the count line would disagree
  // with the visible query).
  const fetchSeqRef = useRef(0)

  const fetchResults = useCallback(async (q) => {
    if (!q || q.length < 2 || !ctx) {
      fetchSeqRef.current++
      setResults([])
      setError(null)
      // Invalidate the in-flight fetch BEFORE clearing loading: bumping the
      // sequence above makes any pending response "stale", and its finally
      // block skips setLoading(false) for stale requests. Without this, the
      // spinner from the aborted query spins forever.
      setLoading(false)
      return
    }
    const seq = ++fetchSeqRef.current
    setLoading(true)
    setError(null)
    try {
      const data = await ctx.rest(`/search?q=${encodeURIComponent(q)}&limit=50`, {
        method: 'GET',
        timeoutMs: 5000
      })
      if (seq !== fetchSeqRef.current) return // stale response — a newer query is in flight
      setResults(Array.isArray(data) ? data : [])
    } catch (e) {
      console.error('Search failed:', e)
      if (seq !== fetchSeqRef.current) return
      setError(String(e?.message || e))
      setResults([])
    } finally {
      if (seq === fetchSeqRef.current) setLoading(false)
    }
  }, [ctx])

  useEffect(() => {
    const timer = setTimeout(() => { fetchResults(query) }, 300)
    return () => clearTimeout(timer)
  }, [query, fetchResults])

  const sourceLabels = { memory: 'Memory', sessions: 'Session', kanban: 'Task', activity: 'Activity' }
  const sourceStyle = {
    memory: { color: 'var(--ui-purple)' },
    sessions: { color: 'var(--ui-blue)' },
    kanban: { color: 'var(--ui-green)' },
    activity: { color: 'var(--ui-orange)' }
  }

  const filteredResults = useMemo(() => {
    if (Object.values(selectedSources).every(v => v)) return results
    return results.filter(r => selectedSources[r.source] !== false)
  }, [results, selectedSources])
  const allSourcesOff = Object.values(selectedSources).every(v => !v)

  // Per-source match counts from the RAW sample (before client-side toggling):
  // an operator deciding which sources to include sees at a glance where the
  // hits are ("Memory (3) · Task (0)") instead of toggling blind. Shown only
  // when the sample honestly reflects the CURRENT query — while a fetch is in
  // flight, the previous query's counts would be a lie about the new text
  // (same policy as the count line, tick-8). The backend caps /search at the
  // requested limit (50), so when the raw sample is at the cap the counts are
  // sample counts, disclosed in the title (tick-35 counter-honesty parity).
  const sourceCounts = (query.length >= 2 && !loading)
    ? Object.fromEntries(
        Object.keys(selectedSources).map(s => [s, results.filter(r => r.source === s).length])
      )
    : null
  const sampleCapped = results.length >= 50
  // The visible-set cap disclosure: filteredResults is capped by the raw
  // sample (≤50), so a count pinned at 50 means MORE matches may exist beyond
  // what the backend returned — print 50+ instead of a false exact total
  // (tick-35 counter-honesty policy, now on the last undislosed list surface).
  const visibleCapped = filteredResults.length >= 50

  const toggleSource = (source) => {
    setSelectedSources(prev => ({ ...prev, [source]: !prev[source] }))
  }

  return jsxs('div', {
    className: 'flex h-full flex-col',
    children: [
      jsxs('div', {
        className: 'p-3 border-b border-(--ui-stroke-tertiary) space-y-2',
        children: [
          jsx(SearchField, {
            placeholder: 'search memories, sessions, tasks, activity…',
            value: query,
            onChange: setQuery,
            // Canonical SDK clear affordance: one click resets a long query
            // instead of backspacing it. The cleared fetch short-circuits in
            // fetchResults (bumps the seq, clears results + loading), so no
            // stale-response race is possible.
            onClear: () => setQuery(''),
            ariaLabel: 'Search Abyss'
          }),
          jsx('div', {
            className: 'flex gap-1 flex-wrap',
            children: Object.entries(selectedSources).map(([source, enabled]) =>
              jsx(Button, {
                key: source,
                variant: enabled ? 'default' : 'ghost',
                size: 'sm',
                onClick: () => toggleSource(source),
                'aria-pressed': enabled,
                'aria-label': `${sourceLabels[source]}${sourceCounts != null ? `, ${sourceCounts[source]} matches` : ''}`,
                className: 'text-xs h-6 abyss-mono',
                children: [
                  sourceLabels[source],
                  sourceCounts != null && jsx('span', {
                    className: 'abyss-micro abyss-mono tabular-nums ' + (sourceCounts[source] > 0 ? 'text-(--ui-text-tertiary)' : 'text-(--ui-text-quaternary)'),
                    title: sampleCapped ? 'matches in the first 50 results' : undefined,
                    children: `(${sourceCounts[source]})`
                  })
                ]
              })
            )
          })
        ]
      }),
      // Hidden while a fetch is in flight: showing the counter mid-flight pairs
      // the NEW query text with the OLD result count for one debounce frame
      // (a lie about what the operator just typed).
      query.length >= 2 && !loading && jsx('div', {
        className: 'px-3 py-1 abyss-micro text-(--ui-text-tertiary) abyss-mono border-b border-(--ui-stroke-tertiary)',
        title: visibleCapped ? 'showing the first 50 matches — more may exist' : undefined,
        children: `${filteredResults.length}${visibleCapped ? '+' : ''} result${filteredResults.length !== 1 ? 's' : ''} for “${query}”`
      }),
      jsx('div', {
        className: 'flex-1 overflow-y-auto',
        children: loading ? jsx('div', {
          className: 'p-3 flex items-center justify-center',
          children: jsx(GlyphSpinner, { ariaLabel: 'Searching', className: 'text-(--ui-text-tertiary)' })
        }) : error ? jsx(ErrorState, {
          title: 'Search failed',
          description: error,
          children: jsx(Button, { variant: 'secondary', size: 'sm', onClick: () => fetchResults(query), children: 'Retry' })
        }) : filteredResults.length === 0 ? jsx(EmptyState, {
          title: query.length < 2 ? 'Search Abyss' : allSourcesOff ? 'All sources off' : 'No results found',
          description: query.length < 2
            ? 'Type to search across memories, sessions, tasks, and activity.'
            : allSourcesOff
              ? 'Every source is disabled — enable at least one source to see matches.'
              : `No matches for “${query}” in the selected sources.`
        }) : jsxs('div', {
          className: 'flex flex-col',
          children: filteredResults.map((result, idx) => {
            const srcStyle = sourceStyle[result.source] || { color: 'var(--ui-text-secondary)' }
            return jsxs('div', {
              key: `${result.source}-${result.id}-${idx}`,
              className: cn(
                'px-3 py-2 abyss-row-hover',
                idx < filteredResults.length - 1 && 'border-b border-(--ui-stroke-tertiary)'
              ),
              children: [
                jsxs('div', {
                  className: 'flex items-center gap-2',
                  children: [
                    jsx('span', { className: 'abyss-micro abyss-mono uppercase tracking-wider select-none', style: srcStyle, children: result.source }),
                    result.relevance && jsx(Badge, { variant: 'outline', size: 'xs', className: 'abyss-mono tabular-nums', children: `${Math.round(result.relevance * 100)}%` }),
                    result.timestamp && jsx('span', { className: 'abyss-micro text-(--ui-text-quaternary) abyss-mono ml-auto', title: timeTitle(result.timestamp), children: relativeTime(result.timestamp) })
                  ]
                }),
                jsx('div', { className: 'font-medium text-sm mt-0.5 truncate text-(--ui-text-primary) abyss-mono', title: result.title, children: result.title }),
                result.description && jsx('div', {
                  className: 'text-xs text-(--ui-text-secondary) mt-1 line-clamp-2 abyss-mono',
                  title: result.description || undefined,
                  children: result.description
                }),
                (result.category || result.status) && jsx('div', {
                  className: 'flex gap-1.5 mt-1 flex-wrap',
                  children: [
                    result.category && jsx(Badge, { variant: 'muted', size: 'xs', children: result.category }),
                    result.status && jsx(Badge, { variant: 'outline', size: 'xs', children: result.status })
                  ]
                }),
                result.source === 'sessions' && onOpenTrace && jsx('div', {
                  className: 'mt-1',
                  children: jsx(Button, {
                    variant: 'ghost', size: 'xs',
                    onClick: () => onOpenTrace(result.id),
                    title: 'Open this session trace',
                    'aria-label': `Open trace for session ${String(result.id || '').slice(0, 8)}`,
                    className: 'abyss-tiny abyss-mono h-6 px-1.5',
                    children: 'trace ›'
                  })
                })
              ]
            })
          })
        })
      })
    ]
  })
}

// --- Tracing View ---
const EVENT_ICONS = {
  tool_call: 'play-circle',
  tool_call_end: 'pass',
  llm_call: 'sparkle',
  llm_call_end: 'wand',
  session_start: 'circle-small',
  session_end: 'stop-circle',
  memory_save: 'bookmark',
  memory_recall: 'history'
}
const EVENT_TONES = {
  tool_call: 'var(--ui-blue)',
  tool_call_end: 'var(--ui-green)',
  llm_call: 'var(--ui-purple)',
  llm_call_end: 'var(--ui-purple)',
  session_start: 'var(--ui-cyan)',
  session_end: 'var(--ui-text-secondary)',
  memory_save: 'var(--ui-yellow)',
  memory_recall: 'var(--ui-cyan)'
}

// Raindrop-style duration formatters
function fmtDur(ms) {
  if (!ms && ms !== 0) return '—'
  if (ms < 1000) return `${Math.round(ms)}ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`
  const m = Math.floor(ms / 60000)
  const s = Math.round((ms % 60000) / 1000)
  return `${m}m ${s}s`
}
const shortID = (id) => String(id || '').slice(0, 8)

// ---------------------------------------------------------------------------
// Trace Graph — Raindrop-style trajectory DAG rendered on canvas.
// Each tool call is a node (start+end merged), grouped under the reasoning
// turn that spawned it; error nodes glow red, open calls amber. This is the
// "visual graph node system instead of list view" the operator asked for.
// ---------------------------------------------------------------------------
function TraceGraphView({ ctx, session }) {
  const wrapRef = useRef(null)
  const canvasRef = useRef(null)
  const layoutRef = useRef({ pos: {}, byId: {} })
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [selected, setSelected] = useState(null)
  // Focus ring state: the canvas is focusable with the full keyboard
  // contract, so a focused-but-invisible canvas is a dead end for keyboard
  // operators. Focus/blur bump this state so the draw effect replays the
  // ring (DESIGN.md a11y: focus rings via theme accent, phosphor dashed).
  const [canvasFocused, setCanvasFocused] = useState(false)
  // Out-of-order guard: rapid session switches must never let a slow response
  // for the OLD session draw its DAG under the NEW one.
  const fetchSeqRef = useRef(0)

  const fetchGraph = useCallback(async () => {
    if (!ctx || !session) return
    const seq = ++fetchSeqRef.current
    setLoading(true); setError(null)
    // Focus is lost when the loading spinner unmounts the canvas, and React
    // does NOT fire blur on unmount — reset the ring state so the remounted
    // canvas doesn't repaint a stale focus ring over an unfocused graph.
    setCanvasFocused(false)
    try {
      const d = await ctx.rest(`/trace/graph?session_id=${encodeURIComponent(session)}&limit=300`, { method: 'GET', timeoutMs: 8000 })
      if (seq !== fetchSeqRef.current) return // stale — a newer session is selected
      if (d && Array.isArray(d.nodes)) { setData(d); setSelected(null) }
      else { setData({ nodes: [], edges: [], stats: {} }) }
    } catch (e) {
      console.error('graph fetch failed', e)
      if (seq !== fetchSeqRef.current) return
      setError(String(e?.message || e)); setData(null)
    } finally { if (seq === fetchSeqRef.current) setLoading(false) }
  }, [ctx, session]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => { fetchGraph() }, [fetchGraph])

  useEffect(() => {
    const cv = canvasRef.current, wrap = wrapRef.current
    if (!cv || !wrap || !data) return
    const NODE_W = 168, NODE_H = 44, COLM = 208, ROWM = 62
    const draw = () => {
      const W = Math.max(wrap.clientWidth, 160)
      const H = Math.max(wrap.clientHeight, 340)
      const dpr = window.devicePixelRatio || 1
      cv.width = W * dpr; cv.height = H * dpr
      cv.style.width = W + 'px'; cv.style.height = H + 'px'
      const g = cv.getContext('2d'); g.setTransform(dpr, 0, 0, dpr, 0, 0)
      g.clearRect(0, 0, W, H)
      const p = palette()

      // Focus ring — logical CSS px, theme accent (see canvasFocused note).
      if (canvasFocused) {
        g.save()
        g.strokeStyle = p.accent
        g.lineWidth = 1.5
        g.globalAlpha = 0.9
        g.setLineDash([4, 3])
        g.strokeRect(1, 1, W - 2, H - 2)
        g.setLineDash([])
        g.restore()
      }

      // ---- assign depths via BFS over spawn edges ----
      const byId = {}; data.nodes.forEach(n => { byId[n.id] = n })
      const kids = {}; (data.edges || []).forEach(e => { (kids[e.source] = kids[e.source] || []).push(e.target) })
      const depth = {}
      // Cycle guard: the backend pairs start/end events into a DAG, but a
      // malformed payload with a cyclic spawn edge would otherwise recurse
      // forever inside draw() and take the whole pane down. A node currently
      // on the recursion stack contributes depth 0 instead of looping.
      const onStack = {}
      const depthOf = (id) => {
        if (depth[id] != null) return depth[id]
        if (onStack[id]) return 0
        onStack[id] = true
        let m = 0
        ;(kids[id] || []).forEach(c => { m = Math.max(m, depthOf(c) + 1) })
        onStack[id] = false
        depth[id] = m; return m
      }
      data.nodes.forEach(n => depthOf(n.id))
      const maxD = Math.max(0, ...Object.values(depth))

      // ---- group into left->right columns by depth, stack within ----
      const cols = {}
      data.nodes.forEach(n => {
        const c = maxD - depth[n.id]
        ;(cols[c] = cols[c] || []).push(n)
      })
      const pos = {}
      Object.keys(cols).forEach(cs => {
        const list = cols[cs]
        const totalH = list.length * ROWM
        let startY = (H - totalH) / 2 + ROWM / 2
        if (startY < 8) startY = 8
        list.forEach((n, i) => {
          pos[n.id] = { x: 16 + (+cs) * COLM, y: startY + i * ROWM }
        })
      })
      layoutRef.current = { pos, byId, nodeW: NODE_W, nodeH: NODE_H }

      // ---- edges ----
      const edgeC = p.strokeDim
      ;(data.edges || []).forEach(e => {
        const s = pos[e.source], t = pos[e.target]
        if (!s || !t) return
        g.strokeStyle = edgeC; g.lineWidth = 1; g.globalAlpha = 0.45
        g.beginPath()
        const sx = s.x + NODE_W, sy = s.y + NODE_H / 2, tx = t.x, ty = t.y + NODE_H / 2
        const mx = (sx + tx) / 2
        g.moveTo(sx, sy); g.bezierCurveTo(mx, sy, mx, ty, tx, ty); g.stroke()
        g.globalAlpha = 1
      })

      // ---- nodes ----
      const statusTone = (n) => {
        if (n.type === 'session') return p.session
        if (n.type === 'llm') return p.memory
        if (n.status === 'error') return themeColor('--ui-red')
        if (n.status === 'running' || n.status === 'unknown') return themeColor('--ui-orange')
        return p.tool
      }
      data.nodes.forEach(n => {
        const p0 = pos[n.id]; if (!p0) return
        const isSel = selected && selected.id === n.id
        const fill = themeColor('--ui-bg-elevated')
        const col = statusTone(n)
        const x = p0.x, y = p0.y, w = NODE_W, h = NODE_H, r = 6
        g.fillStyle = fill; g.strokeStyle = isSel ? themeColor('--ui-accent') : col
        g.lineWidth = isSel ? 2.4 : 1.3
        g.beginPath()
        g.moveTo(x + r, y); g.arcTo(x + w, y, x + w, y + h, r); g.arcTo(x + w, y + h, x, y + h, r)
        g.arcTo(x, y + h, x, y, r); g.arcTo(x, y, x + w, y, r); g.closePath(); g.fill(); g.stroke()
        // left color bar + status dot
        g.fillStyle = col; g.fillRect(x + 3, y + 6, 3, h - 12)
        g.beginPath(); g.arc(x + w - 10, y + 11, 3, 0, Math.PI * 2); g.fill()
        // label — host mono stack (parity with PhosphorGraphRenderer; DESIGN.md
        // Type: host mono for everything, canvas cannot resolve var()).
        g.fillStyle = themeColor('--ui-text-primary'); g.font = '600 11px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace'
        g.fillText(String(n.label || n.type || '').slice(0, 17), x + 13, y + 16)
        // sub
        g.fillStyle = themeColor('--ui-text-quaternary'); g.font = '9px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace'
        const sub = n.type === 'llm' ? 'reasoning' : (n.type === 'session' ? 'session' : (n.error_type || n.status))
        g.fillText(String(sub || '').slice(0, 15), x + 13, y + 30)
        if (n.duration_ms) {
          g.fillText(fmtDur(n.duration_ms), x + w - 42, y + 31)
        }
      })

      // ---- empty hint ----
      if (!data.nodes.length) {
        g.fillStyle = themeColor('--ui-text-quaternary'); g.font = '12px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace'
        g.fillText('no trace events for this session', 12, 24)
      }
    }
    draw()
    const ro = new ResizeObserver(draw)
    ro.observe(wrap)
    return () => ro.disconnect()
  }, [data, selected, session, canvasFocused])

  const onClick = (ev) => {
    const cv = canvasRef.current, lay = layoutRef.current
    if (!cv || !lay.pos) return
    const rect = cv.getBoundingClientRect()
    const mx = ev.clientX - rect.left, my = ev.clientY - rect.top
    let hit = null
    Object.keys(lay.pos).forEach(id => {
      const p = lay.pos[id]
      if (mx >= p.x && mx <= p.x + lay.nodeW && my >= p.y && my <= p.y + lay.nodeH) hit = lay.byId[id]
    })
    setSelected(hit)
  }

  const st = (data && data.stats) || {}
  return jsxs('div', {
    className: 'flex flex-1 min-h-0 flex-col',
    children: [
      jsxs('div', {
        className: 'flex items-center gap-2 px-3 py-1.5 border-b border-(--ui-stroke-tertiary) flex-wrap',
        children: [
          jsx(Codicon, { name: 'graph', className: 'text-(--ui-text-secondary)' }),
          jsx('span', { className: 'abyss-micro uppercase tracking-wider text-(--ui-text-secondary) abyss-mono', children: 'trajectory graph' }),
          jsx(Badge, { variant: 'outline', size: 'xs', className: 'abyss-tiny abyss-mono', children: `${st.tools || 0} tools` }),
          jsx(Badge, { variant: 'outline', size: 'xs', className: 'abyss-tiny abyss-mono', children: `${st.llms || 0} reasoning` }),
          st.errors > 0 && jsx(Badge, { variant: 'outline', size: 'xs', className: 'abyss-tiny text-(--ui-red) abyss-mono', children: `${st.errors} failed` }),
          st.open > 0 && jsx(Badge, { variant: 'outline', size: 'xs', className: 'abyss-tiny abyss-mono', style: { color: 'var(--ui-orange)' }, children: `${st.open} open` })
        ]
      }),
      jsx('div', {
        ref: wrapRef,
        className: 'relative flex-1 min-h-0 overflow-hidden',
        children: loading ? jsxs('div', { className: 'flex h-full items-center justify-center gap-2', children: [jsx(GlyphSpinner, { ariaLabel: 'Loading graph', className: 'text-(--ui-text-tertiary)' }), jsx('span', { className: 'text-sm text-(--ui-text-secondary) abyss-mono', children: 'building trajectory…' })] })
          : error ? jsx(ErrorState, { title: 'Graph unavailable', description: error, children: jsx(Button, { variant: 'secondary', size: 'sm', onClick: fetchGraph, children: 'Retry' }) })
          : [jsx('canvas', {
              ref: canvasRef, onClick,
              // Click-select affordance parity with the Brain graph canvas.
              className: 'block cursor-crosshair',
              onFocus: () => setCanvasFocused(true),
              onBlur: () => setCanvasFocused(false),
              // Match the Brain graph's keyboard contract: focusable, labelled,
              // Escape clears the selection (parity with PhosphorGraphRenderer).
              tabIndex: 0,
              role: 'application',
              'aria-label': `Trajectory graph${session ? ` for session ${String(session).slice(0, 8)}` : ''}${selected ? `, selected ${selected.label || selected.type}` : ''} — arrow keys move selection, escape clears`,
              onKeyDown: (ev) => {
                if (ev.key === 'Escape') { setSelected(null); return }
                // Arrow-key selection parity with the Brain graph canvas:
                // nearest node in the direction half-plane, preferring
                // straight-ahead (same scoring model as
                // PhosphorGraphRenderer._selectByDirection).
                const dirs = { ArrowUp: [0, -1], ArrowDown: [0, 1], ArrowLeft: [-1, 0], ArrowRight: [1, 0] }
                const d = dirs[ev.key]
                if (!d) return
                ev.preventDefault()
                const lay = layoutRef.current
                const cv = canvasRef.current
                if (!lay || !lay.pos || !cv) return
                const ids = Object.keys(lay.pos)
                if (!ids.length) return
                let cur = (selected && lay.pos[selected.id]) || null
                if (!cur) {
                  // No selection yet: start from the viewport center so the
                  // first arrow press picks the graph's middle node.
                  cur = { x: cv.clientWidth / 2, y: cv.clientHeight / 2 }
                }
                let best = null
                let bestScore = Infinity
                for (const id of ids) {
                  if (selected && id === selected.id) continue
                  const p = lay.pos[id]
                  const vx = p.x - cur.x
                  const vy = p.y - cur.y
                  const len = Math.sqrt(vx * vx + vy * vy) || 1
                  const dot = (vx * d[0] + vy * d[1]) / len
                  if (dot <= 0) continue
                  const perp = Math.abs(vx * d[1] - vy * d[0]) / len
                  const score = perp - dot * 0.5
                  if (score < bestScore) { bestScore = score; best = lay.byId[id] }
                }
                if (best) setSelected(best)
              }
            }),
              // Keyboard-map hint — discoverability parity with the Brain
              // graph footer ("footer hint prints the keyboard map", DESIGN.md
              // Brain graph). The trajectory canvas gained the same arrow-key
              // selection contract in tick-16, but had no affordance telling a
              // keyboard operator it exists. Dim, non-interactive overlay on the
              // drawn ground; inline position (no compiled utility for it).
              jsx('div', {
                className: 'abyss-micro text-(--ui-text-quaternary) abyss-mono pointer-events-none select-none',
                style: { position: 'absolute', bottom: 6, right: 8, zIndex: 5 },
                children: 'click: select · arrows: move · esc: clear'
              })
            ]
      }),
      selected && jsxs('div', {
        className: 'border-t border-(--ui-stroke-tertiary) px-3 py-2 abyss-mono',
        children: [
          jsxs('div', { className: 'flex items-center justify-between gap-2', children: [
            jsx('span', { className: 'text-xs font-medium text-(--ui-text-primary)', children: String(selected.label || selected.type) }),
            jsx('span', { className: 'abyss-micro text-(--ui-text-quaternary)', children: fmtDur(selected.duration_ms) })
          ] }),
          selected.tool && jsx('div', { className: 'text-xs text-(--ui-text-secondary) mt-0.5', children: selected.tool }),
          (selected.status === 'error') && jsx('div', { className: 'text-xs text-(--ui-red) mt-1', children: `⚠ ${selected.error_type || 'error'}${selected.error_message ? ': ' + String(selected.error_message).slice(0, 160) : ''}` }),
          !(selected.status === 'error') && selected.result_preview && jsx('div', { className: 'text-xs text-(--ui-text-secondary) mt-1 break-words', children: String(selected.result_preview).slice(0, 160) })
        ]
      })
    ]
  })
}

// ---------------------------------------------------------------------------
// Trace Timeline — "see each agent as a timeline."
// Overview: every agent (session) renders as one horizontal bar on a shared
// time axis, green when healthy, red when it failed somewhere. Click a lane
// to drill into that agent's per-lane trajectory (reasoning / tools /
// failures), each event positioned by its start offset.
// ---------------------------------------------------------------------------
function TraceTimelineView({ ctx, session, onPick }) {
  const [overview, setOverview] = useState(null)
  const [loadingO, setLoadingO] = useState(true)
  const [tl, setTl] = useState(null)
  const [loadingT, setLoadingT] = useState(false)
  const [error, setError] = useState(null)
  // Trajectory-detail fetch failure (kept separate from `error` which is the
  // agents-overview failure): a dead /trace/timeline link must print a retry
  // surface, never the "no trajectory data" empty line (DESIGN.md States —
  // errors never masquerade as empty data).
  const [tlError, setTlError] = useState(null)
  // Out-of-order guard: clicking lanes quickly must never let a slow response
  // for the OLD session's trajectory render under the NEW selection.
  const tlSeqRef = useRef(0)

  const fetchOverview = useCallback(async () => {
    if (!ctx) return
    setLoadingO(true); setError(null)
    try {
      const d = await ctx.rest('/trace/agents?limit=60', { method: 'GET', timeoutMs: 8000 })
      setOverview(Array.isArray(d?.agents) ? d.agents : [])
    } catch (e) { console.error('agents fetch', e); setError(String(e?.message || e)) }
    finally { setLoadingO(false) }
  }, [ctx])
  useEffect(() => {
    // Live agents list: poll every 30s (cadence parity with status/activity/
    // wave surfaces) so a session that just finished appears without
    // remounting the view. The loading guard below keeps the cached lanes
    // while the poll is in flight — no blink.
    fetchOverview()
    const t = setInterval(fetchOverview, 30000)
    return () => clearInterval(t)
  }, [fetchOverview])

  const fetchTimeline = useCallback(async (sid) => {
    if (!ctx || !sid) return
    const seq = ++tlSeqRef.current
    setLoadingT(true)
    try {
      const d = await ctx.rest(`/trace/timeline?session_id=${encodeURIComponent(sid)}&limit=300`, { method: 'GET', timeoutMs: 8000 })
      if (seq !== tlSeqRef.current) return // stale — a newer lane is selected
      setTl(Array.isArray(d?.lanes) ? d : null)
      setTlError(null)
    } catch (e) {
      console.error('timeline fetch', e)
      if (seq === tlSeqRef.current) { setTl(null); setTlError('timeline link down') }
    }
    finally { if (seq === tlSeqRef.current) setLoadingT(false) }
  }, [ctx])
  useEffect(() => {
    if (!session) return
    // Clear the previous session's lanes immediately — never paint the old
    // trajectory under the new session's header while its fetch is in flight.
    setTl(null)
    setTlError(null)
    fetchTimeline(session)
  }, [session, fetchTimeline])

  const maxDur = overview && overview.length ? Math.max(...overview.map(a => a.duration_ms || 0), 1) : 1
  const L = tl ? Math.max(tl.total_ms, 1) : 1
  const laneTone = {
    reasoning: themeColor('--ui-purple'),
    tools: themeColor('--ui-green'),
    failures: themeColor('--ui-red')
  }

  return jsxs('div', {
    className: 'flex flex-1 min-h-0 flex-col',
    children: [
      jsxs('div', {
        className: 'flex items-center gap-2 px-3 py-1.5 border-b border-(--ui-stroke-tertiary)',
        children: [
          jsx(Codicon, { name: 'whole-word', className: 'text-(--ui-text-secondary)' }),
          jsx('span', { className: 'abyss-micro uppercase tracking-wider text-(--ui-text-secondary) abyss-mono', children: 'agents · every agent as a timeline' })
        ]
      }),
      jsx('div', {
        className: 'flex-1 min-h-0 overflow-y-auto px-3 py-2',
        children: loadingO && (!overview || !overview.length) ? jsxs('div', { className: 'py-6 flex items-center justify-center gap-2', children: [jsx(GlyphSpinner, { ariaLabel: 'Loading agents', className: 'text-(--ui-text-tertiary)' }), jsx('span', { className: 'text-sm text-(--ui-text-secondary) abyss-mono', children: 'loading agents…' })] })
          : error && (!overview || !overview.length) ? jsx(ErrorState, { title: 'Agents unavailable', description: error, children: jsx(Button, { variant: 'secondary', size: 'sm', onClick: fetchOverview, children: 'Retry' }) })
          : !overview || !overview.length ? jsx(EmptyState, { title: 'No agents', description: 'No traced sessions yet.' })
          : jsxs('div', { className: 'space-y-1.5', children: [
              overview.map(a => {
                const pct = Math.max(((a.duration_ms || 0) / maxDur) * 100, 2)
                const active = session === a.session_id
                return jsxs('div', {
                  key: a.session_id,
                  className: 'group flex items-center gap-2 cursor-pointer rounded px-1 py-0.5 abyss-focus-ring ' + (active ? 'bg-(--ui-bg-tertiary)' : 'hover:bg-(--ui-bg-tertiary)'),
                  // Keyboard contract parity with the Brain/Trace canvases:
                  // lanes are click-to-drill, so they must also activate on
                  // Enter/Space for keyboard-only operators (DESIGN.md a11y).
                  role: 'button',
                  tabIndex: 0,
                  'aria-label': `Agent ${shortID(a.session_id)} — ${a.event_count || 0} events, ${a.error_count || 0} failed${active ? ', selected' : ''} — Enter opens its trajectory`,
                  onClick: () => onPick && onPick(a.session_id),
                  onKeyDown: (ev) => {
                    if (ev.key === 'Enter' || ev.key === ' ') {
                      ev.preventDefault()
                      if (onPick) onPick(a.session_id)
                    }
                  },
                  children: [
                    jsx('span', { className: 'abyss-tiny abyss-mono text-(--ui-text-secondary) w-14 shrink-0', title: a.session_id, children: shortID(a.session_id) }),
                    jsx('div', { className: 'relative h-4 flex-1 rounded-sm overflow-hidden bg-(--ui-bg-tertiary)', title: `${a.event_count || 0} events · ${a.error_count || 0} failed · ${a.llm_count || 0} reasoning`, children: [
                      jsx('div', { className: 'h-full rounded-sm', style: { width: pct + '%', backgroundColor: a.has_errors ? 'var(--ui-red)' : 'var(--ui-green)', opacity: 0.4 } }),
                      a.error_count > 0 && jsx('div', { className: 'absolute top-0 h-full rounded-sm', style: { left: `calc(${pct}% - 2px)`, width: 4, backgroundColor: 'var(--ui-red)' } })
                    ] }),
                    jsx('span', { className: 'abyss-tiny abyss-mono ' + (a.has_errors ? 'text-(--ui-red)' : 'text-(--ui-text-quaternary)'), children: a.has_errors ? `${a.error_count}✗` : '✓' })
                  ]
                })
              })
            ] })
      }),
      jsxs('div', {
        className: 'border-t border-(--ui-stroke-tertiary) px-3 py-2',
        children: [
          jsxs('div', { className: 'flex items-center justify-between mb-1.5', children: [
            jsx('span', { className: 'abyss-micro uppercase tracking-wider text-(--ui-text-secondary) abyss-mono', children: session ? `trajectory · ${shortID(session)}` : 'trajectory · pick an agent' }),
            tl && jsx('span', { className: 'abyss-micro text-(--ui-text-quaternary) abyss-mono', children: fmtDur(L) })
          ] }),
          loadingT ? jsxs('div', { className: 'flex items-center gap-2', children: [jsx(GlyphSpinner, { ariaLabel: 'Loading trajectory', className: 'text-(--ui-text-tertiary)' }), jsx('span', { className: 'text-xs text-(--ui-text-tertiary) abyss-mono', children: 'loading trajectory…' })] })
            : tlError && !tl ? jsxs('div', { className: 'flex items-center gap-2', children: [
                jsx('span', { className: 'text-xs text-(--ui-red) abyss-mono', children: tlError }),
                jsx(Button, { variant: 'ghost', size: 'xs', className: 'abyss-tiny abyss-mono h-6 px-1.5', onClick: () => fetchTimeline(session), children: 'retry' })
              ] })
            : !tl ? jsx('div', { className: 'text-xs text-(--ui-text-quaternary) abyss-mono', children: session ? `no trajectory data for ${shortID(session)}` : 'pick a session to see its trajectory' })
            : tl.lanes.every(l => !l.nodes.length) ? jsx('div', { className: 'text-xs text-(--ui-text-quaternary) abyss-mono', children: 'no events in this trajectory' })
            : jsx('div', { className: 'space-y-1.5', style: { maxHeight: '12rem', overflowY: 'auto' }, children: tl.lanes.map(lane => {
                if (!lane.nodes.length) return null
                const tone = laneTone[lane.id] || themeColor('--ui-text-secondary')
                return jsxs('div', { key: lane.id, className: 'space-y-0.5', children: [
                  jsxs('div', { className: 'flex items-center gap-2', children: [
                    jsxs('span', { className: 'flex items-center gap-1 w-20 shrink-0 min-w-0', children: [
                      jsx('span', { className: 'inline-block h-1.5 w-1.5 rounded-full shrink-0', style: { backgroundColor: tone }, children: '' }),
                      jsx('span', { className: 'abyss-micro abyss-mono text-(--ui-text-quaternary) truncate', title: lane.label, children: lane.label })
                    ]}),
                    jsx('div', { className: 'relative flex-1 h-3 overflow-hidden rounded-sm bg-(--ui-bg-tertiary)', children: lane.nodes.map(n => {
                      const left = Math.max((n.start_ms / L) * 100, 0)
                      const width = Math.min(100, Math.max(((n.duration_ms || 200) / L) * 100, 1.2))
                      const isFail = lane.id === 'failures' || n.status === 'error'
                      return jsx('div', {
                        key: n.id,
                        className: 'absolute top-0 h-full rounded-sm opacity-90',
                        style: { left: left + '%', width: width + '%', backgroundColor: isFail ? 'var(--ui-red)' : 'var(--ui-accent)' },
                        title: `${n.label} · ${n.status} · ${fmtDur(n.duration_ms)}`
                      })
                    }) })
                  ] })
                ] })
              }) })
        ]
      })
    ]
  })
}

function TracingView({ ctx, presetSessionId, onPresetConsumed }) {
  const [sessions, setSessions] = useState([])
  const [selectedSession, setSelectedSession] = useState(null)
  const [traces, setTraces] = useState([])
  const [loading, setLoading] = useState(true)
  const [loadingSessions, setLoadingSessions] = useState(true)
  const [error, setError] = useState(null)
  const [mode, setMode] = useState('list') // 'list' | 'graph' | 'timeline'

  const fetchSessions = useCallback(async () => {
    if (!ctx) return
    setLoadingSessions(true)
    setError(null)
    try {
      const data = await ctx.rest('/trace?limit=50', { method: 'GET', timeoutMs: 5000 })
      const arr = Array.isArray(data) ? data : []
      setSessions(arr)
      if (arr.length > 0) {
        setSelectedSession(prev => prev || arr[0].session_id)
      }
    } catch (e) {
      console.error('Failed to fetch sessions:', e)
      setError(String(e?.message || e))
      setSessions([])
    } finally {
      setLoadingSessions(false)
    }
  }, [ctx])

  // Out-of-order guard: switching sessions quickly (Select dropdown or a
  // drill-in from another view) must never let a slow response for the OLD
  // session overwrite the event list shown under the NEW session's header.
  const traceSeqRef = useRef(0)

  const fetchTraces = useCallback(async () => {
    if (!ctx || !selectedSession) return
    const seq = ++traceSeqRef.current
    setLoading(true)
    setError(null)
    try {
      const data = await ctx.rest(`/trace?session_id=${encodeURIComponent(selectedSession)}&limit=200`, {
        method: 'GET',
        timeoutMs: 5000
      })
      if (seq !== traceSeqRef.current) return // stale — a newer session is selected
      setTraces(Array.isArray(data) ? data : [])
    } catch (e) {
      console.error('Failed to fetch traces:', e)
      if (seq !== traceSeqRef.current) return
      setError(String(e?.message || e))
      setTraces([])
    } finally {
      if (seq === traceSeqRef.current) setLoading(false)
    }
  }, [ctx, selectedSession])

  useEffect(() => { fetchSessions() }, [fetchSessions])
  useEffect(() => { if (selectedSession) { fetchTraces() } }, [selectedSession, fetchTraces])

  // Drill-down entry: another view (activity row) can preset the session, so
  // a symptom is one click from its trace. The preset is consumed ONCE — a
  // ref records which preset was already applied. The former version also
  // depended on selectedSession, so after any drill-in it re-fired whenever
  // the operator picked another session in the dropdown and snapped the
  // Selection straight back to the drilled session (manual choice was
  // impossible until the view remounted).
  const presetHandledRef = useRef(null)
  useEffect(() => {
    if (presetSessionId && presetHandledRef.current !== presetSessionId) {
      presetHandledRef.current = presetSessionId
      setSelectedSession(presetSessionId)
      // Consume the preset in the PARENT too. The dashboard's tracePreset
      // survives in AbyssDashboard state, so a stale drill would otherwise
      // resurrect on the NEXT tab switch: leaving the trace tab and returning
      // REMOUNTS TracingView with a fresh presetHandledRef (null) and the old
      // preset re-applies, snapping the selection away from whatever the
      // operator had picked in between — the tick-13 hijack bug re-entering
      // through the remount door. onPresetConsumed clears the dashboard's
      // preset so the drill is a true one-shot; a later return to the trace
      // tab starts from the natural most-recent session.
      onPresetConsumed?.()
    }
  }, [presetSessionId, onPresetConsumed])

  // A drill into an old/deep session (activity row, signal, brain node,
  // search hit) can point at a session that fell off the /trace?limit=50
  // recency list. Keep it in the dropdown as a synthetic item so the Select
  // has a matching value (no Radix "value does not exist in items" warning)
  // and the operator sees which session they drilled into; without it the
  // trigger fell back to the "select session…" placeholder while the trace
  // data still loaded. Hooks stay above the early returns (React 310-safe).
  const sessionOptions = useMemo(() => {
    const list = Array.isArray(sessions) ? sessions : []
    if (!selectedSession) return list
    if (list.some(s => s.session_id === selectedSession)) return list
    return [...list, { session_id: selectedSession, synthetic: true }]
  }, [sessions, selectedSession])

  if (loadingSessions) {
    return jsx('div', { className: 'p-3', children: jsx('div', { className: 'h-8 w-full bg-(--ui-bg-tertiary) rounded animate-pulse abyss-mute-pulse' }) })
  }

  // Error-first: a failed /trace fetch must surface as a Retry surface — never
  // masquerade as "no sessions yet" (the P0 from the DESIGN.md States contract:
  // "errors never masquerade as empty data"). Without this guard a dead backend
  // looks indistinguishable from a fresh system with no recorded sessions.
  // A drill preset keeps the view alive too: the list endpoint being down must
  // not bury the preset session's own trace (that endpoint is fetched separately
  // and has its own error surface per mode).
  if (error && sessions.length === 0 && !selectedSession) {
    return jsx(ErrorState, {
      title: 'Sessions unavailable',
      description: error,
      children: jsx(Button, { variant: 'secondary', size: 'sm', onClick: fetchSessions, children: 'Retry' })
    })
  }

  // Empty only when there is genuinely nothing selected: a drill preset keeps
  // the view alive even if the recency list is empty, because fetchTraces
  // already loaded that session's events (errors never masquerade as empty).
  if ((!sessions || sessions.length === 0) && !selectedSession) {
    return jsx(EmptyState, {
      title: 'No sessions',
      description: 'No traced sessions found yet. Activity is recorded as you work.'
    })
  }

  return jsxs('div', {
    className: 'flex h-full flex-col',
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between px-3 py-2 border-b border-(--ui-stroke-tertiary)',
        children: [
          jsxs('div', {
            className: 'flex items-center gap-2',
            children: [
              jsx(Codicon, { name: 'history', className: 'text-(--ui-text-secondary)' }),
              jsx('span', { className: 'text-sm font-medium uppercase tracking-wider text-(--ui-text-primary) abyss-mono', children: 'trace' })
            ]
          }),
          jsxs('div', {
            className: 'flex items-center gap-0.5 rounded-md bg-(--ui-bg-tertiary) p-0.5',
            children: [
              ['list', 'list-flat'], ['graph', 'graph'], ['timeline', 'history']
            ].map(([m, ic]) => jsx(Button, {
              key: m,
              variant: mode === m ? 'secondary' : 'ghost',
              size: 'sm',
              className: 'h-6 px-2 abyss-tiny abyss-mono',
              onClick: () => setMode(m),
              'aria-pressed': mode === m,
              'aria-label': `${m} view`,
              // The toggle is icon-only (list-flat/graph/history) — DESIGN.md
              // names the three modes `list / graph / timeline`, so a mouse
              // hover must reveal which mode an icon toggles. Same hint as the
              // aria-label for keyboard/AT operators.
              title: `${m} view`,
              children: jsx(Codicon, { name: ic, className: 'text-xs' })
            }))
          })
        ]
      }),
      jsx('div', {
        className: 'px-3 py-2 border-b border-(--ui-stroke-tertiary)',
        children: jsx(Select, {
          value: selectedSession || '',
          onValueChange: setSelectedSession,
          children: [
            jsx(SelectTrigger, {
              className: 'w-full text-xs abyss-mono',
              children: jsx(SelectValue, { placeholder: 'select session…' })
            }),
            jsx(SelectContent, {
              children: sessionOptions.map(s =>
                s.synthetic
                  ? jsx(SelectItem, {
                      key: s.session_id,
                      value: s.session_id,
                      children: `${s.session_id?.slice(0, 8) || 'unknown'}… (drill)`
                    })
                  : jsxs(SelectItem, {
                      key: s.session_id,
                      value: s.session_id,
                      children: [
                        `${s.session_id?.slice(0, 8) || 'unknown'}… (${s.activity_count || 0} events`,
                        s.error_count > 0 ? `, ${s.error_count}✗` : '',
                        s.llm_count > 0 ? `, ${s.llm_count}◆` : '',
                        ')'
                      ]
                    })
              )
            })
          ]
        })
      }),
      mode !== 'list' ? (mode === 'graph'
        ? jsx(TraceGraphView, { ctx, session: selectedSession })
        : jsx(TraceTimelineView, { ctx, session: selectedSession, onPick: setSelectedSession })
      ) : jsx('div', {
        className: 'flex-1 overflow-y-auto',
        children: loading ? jsxs('div', {
          className: 'p-3 flex items-center justify-center gap-2',
          children: [jsx(GlyphSpinner, { ariaLabel: 'Loading trace', className: 'text-(--ui-text-tertiary)' }), jsx('span', { className: 'text-sm text-(--ui-text-secondary) abyss-mono', children: 'loading trace…' })]
        }) : error ? jsx(ErrorState, {
          title: 'Trace unavailable',
          description: error,
          children: jsx(Button, { variant: 'secondary', size: 'sm', onClick: fetchTraces, children: 'Retry' })
        }) : traces.length === 0 ? jsx(EmptyState, {
          title: 'No traces found',
          description: selectedSession
            ? 'No trace events recorded for this session yet.'
            : 'Select a session to view its trace timeline.'
        }) : jsx('div', {
          className: 'relative p-2',
          children: [
            jsx('div', { className: 'absolute left-2 top-2 bottom-2 w-px bg-(--ui-stroke-tertiary)' }),
            jsx('div', { className: 'space-y-1', children: traces.map(t => {
              let data = null
              try { data = JSON.parse(t.event_data) } catch (e) { /* not json */ }
              const icon = EVENT_ICONS[t.event_type] || 'circle-small'
              const tone = EVENT_TONES[t.event_type] || 'var(--ui-text-secondary)'
              return jsxs('div', {
                key: t.id,
                // ml-3 is NOT compiled in the live host bundle (index-ChgG27Ex.css) —
                // without it the row box shifts 12px left and the absolute-positioned
                // event glyph (left:-19px) lands off the timeline spine. Applied inline
                // per DESIGN.md (inline styles for values with no class).
                style: { marginLeft: 12 },
                // Hover affordance parity: every other list surface in the
                // instrument (activity rows, search results, signals, incidents,
                // wave feed) rolls over with abyss-row-hover (DESIGN.md States:
                // "rows hover:bg-(--ui-bg-tertiary)"); the trace event list was
                // the lone exception. Class-only, plugin-injected via CONSOLE_CSS.
                className: 'relative pl-3 pb-2.5 last:mb-0 abyss-row-hover',
                children: [
                  jsx('div', {
                    className: 'absolute flex items-center justify-center',
                    style: { left: -19, top: 0, color: tone },
                    children: jsx(Codicon, { name: icon, className: 'text-sm' })
                  }),
                  jsx('div', {
                    className: 'flex items-center gap-2 mb-0.5',
                    children: [
                      jsx(Badge, { variant: 'outline', size: 'xs', className: 'abyss-tiny uppercase tracking-wider abyss-mono', children: (t.event_type || '').replace(/_/g, ' ') }),
                      t.timestamp && jsx('span', { className: 'abyss-micro text-(--ui-text-quaternary) abyss-mono tabular-nums', title: timeTitle(t.timestamp), children: relativeTime(t.timestamp) })
                    ]
                  }),
                  data && data.tool && jsx('div', { className: 'text-sm font-medium text-(--ui-text-primary) truncate abyss-mono', title: data.tool, children: data.tool }),
                  data && data.model && jsx('div', { className: 'text-xs text-(--ui-text-secondary) truncate abyss-mono', title: data.model, children: data.model }),
                  data && data.result_preview && jsx('div', {
                    className: 'text-xs text-(--ui-text-secondary) mt-0.5 truncate abyss-mono',
                    style: { maxWidth: '90%' },
                    title: data.result_preview,
                    children: data.result_preview
                  }),
                  data && data.source && jsx('div', {
                    className: 'abyss-micro text-(--ui-text-quaternary) mt-0.5 abyss-mono',
                    children: 'source: ' + data.source
                  })
                ]
              })
            }) })
          ]
        })
      })
    ]
  })
}

// --- Hermes Brain (phosphor graph) ---
function BrainGraph({ ctx, onOpenTrace }) {
  const canvasRef = useRef(null)
  const containerRef = useRef(null)
  const [loading, setLoading] = useState(true)
  const [nodeCount, setNodeCount] = useState(0)
  const [edgeCount, setEdgeCount] = useState(0)
  const [error, setError] = useState(null)
  const [selectedNodeId, setSelectedNodeId] = useState(null)
  const graphRef = useRef(null)
  const dataRef = useRef(null)
  const roRef = useRef(null)
  // Latest prop for the renderer's activation hook (wired once, not per
  // render — the setters it closes over are stable, so a ref is enough).
  const onOpenTraceRef = useRef(onOpenTrace)
  onOpenTraceRef.current = onOpenTrace

  const fetchGraphData = useCallback(async () => {
    if (!ctx) return
    setLoading(true)
    setError(null)
    try {
      const data = await ctx.rest('/graph?limit=300', { method: 'GET', timeoutMs: 10000 })
      dataRef.current = data || null
      setNodeCount(data?.nodes?.length || 0)
      setEdgeCount(data?.edges?.length || 0)
      setSelectedNodeId(null)
    } catch (e) {
      console.error('Failed to fetch graph:', e)
      dataRef.current = null
      setError(String(e?.message || e))
    } finally {
      setLoading(false)
    }
  }, [ctx])

  useEffect(() => { fetchGraphData() }, [fetchGraphData])

  // Build the renderer ONLY after the canvas has actually mounted. The canvas
  // element is gated behind loading/nodeCount (spinner → canvas swap), so it
  // does NOT exist while the fetch is in flight — constructing the renderer
  // inside fetchGraphData would find a null canvasRef and silently no-op,
  // leaving the graph permanently blank (the "53 nodes / 138 edges but empty
  // canvas" bug). This effect re-runs when loading settles and the canvas
  // exists; it also re-syncs data on refresh.
  useEffect(() => {
    const canvas = canvasRef.current
    const container = containerRef.current
    const data = dataRef.current
    if (!canvas || !container || !data || loading || error) return
    // A freshly-mounted <canvas> defaults to 300×150 backing store even though
    // CSS stretches it to fill the container. ALWAYS size it from the container
    // here — checking `canvas.width === 0` misses the 300×150 default and the
    // renderer would draw into a tiny buffer (blank/distorted graph). Size in
    // device pixels (dpr) so the phosphor dots and labels stay crisp on
    // HiDPI/Windows-scaling displays; the renderer divides by _dpr internally.
    const dpr = (typeof window !== 'undefined' && window.devicePixelRatio) || 1
    canvas.width = Math.max(1, Math.round(container.clientWidth * dpr))
    canvas.height = Math.max(1, Math.round(container.clientHeight * dpr))
    try {
      const g = graphRef.current
      // Rebuild against the LIVE canvas when absent OR stale: a refresh flips
      // loading → spinner → canvas, which remounts the <canvas> element (React
      // swaps sibling element types at the same position), so graphRef.current
      // still points at the detached old canvas. Drawing to it would leave the
      // graph blank after a refresh. Comparing the renderer's stored canvas to
      // the live node detects the remount cheaply.
      if (!g || g.canvas !== canvas) {
        graphRef.current = new PhosphorGraphRenderer(canvas, data)
      } else {
        g.setData(data)
      }
      // React-side selection state (drives the trace › affordance and the
      // canvas aria-label).
      g.onSelect = (node) => setSelectedNodeId(node ? node.id : null)
      // Enter/Space on a session node drills straight into its trace.
      g.onActivate = (node) => {
        if (!node || node.type !== 'session') return
        const sid = (node.data && node.data.session_id) || String(node.id || '').replace(/^session:/, '')
        if (sid && onOpenTraceRef.current) onOpenTraceRef.current(sid)
      }
    } catch (e) {
      console.error('[abyss-brain] renderer construction THREW', e)
    }
  }, [loading, error, nodeCount])

  useEffect(() => {
    if (!containerRef.current) return
    // rAF-throttle: rapid resize (pane drag) coalesces to one layout recompute
    // + render per frame instead of thrashing synchronously per RO callback.
    let rafId = null
    const resize = () => {
      rafId = null
      const canvas = canvasRef.current
      const container = containerRef.current
      const g = graphRef.current
      if (!canvas || !container || !g) return
      const w = container.clientWidth
      const h = container.clientHeight
      // Compare against the dpr-scaled backing store (the mount effect sizes
      // device-pixel buffers; clientWidth/Height are CSS px).
      const dpr = (typeof window !== 'undefined' && window.devicePixelRatio) || 1
      const bw = Math.max(1, Math.round(w * dpr))
      const bh = Math.max(1, Math.round(h * dpr))
      if (w > 0 && h > 0 && (canvas.width !== bw || canvas.height !== bh)) {
        canvas.width = bw
        canvas.height = bh
        // Layout was computed for the old size — recompute so nodes stay inside
        // the (possibly much larger) canvas instead of piling into one corner.
        g._computeLayout()
      }
      g._render()
    }
    roRef.current = new ResizeObserver(() => {
      if (rafId != null) return
      rafId = requestAnimationFrame(resize)
    })
    roRef.current.observe(containerRef.current)
    return () => {
      if (rafId != null) cancelAnimationFrame(rafId)
      roRef.current?.disconnect()
    }
  }, [])

  const legend = [
    { style: { backgroundColor: 'var(--ui-blue)' }, label: 'Sessions' },
    { style: { backgroundColor: 'var(--ui-green)' }, label: 'Tools' },
    { style: { backgroundColor: 'var(--ui-purple)' }, label: 'Memories' },
    { style: { backgroundColor: 'var(--ui-orange)' }, label: 'Categories' },
    { style: { backgroundColor: 'var(--ui-red)' }, label: 'Tasks' }
  ]

  // The selected node object (drives the trace › affordance and aria-label).
  const selectedNode = useMemo(() => {
    if (!selectedNodeId || !dataRef.current) return null
    return (dataRef.current.nodes || []).find(n => n.id === selectedNodeId) || null
  }, [selectedNodeId])
  const sessionTraceId = selectedNode && selectedNode.type === 'session'
    ? (selectedNode.data && selectedNode.data.session_id) || String(selectedNode.id || '').replace(/^session:/, '')
    : null

  return jsxs('div', {
    className: 'flex h-full flex-col',
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between px-3 py-2 border-b border-(--ui-stroke-tertiary)',
        children: [
          jsxs('div', {
            className: 'flex items-center gap-2',
            children: [
              jsx(Codicon, { name: 'circuit-board', className: 'text-(--ui-text-secondary)' }),
              jsx('span', { className: 'text-sm font-medium uppercase tracking-wider text-(--ui-text-primary) abyss-mono', children: 'hermès brain' })
            ]
          }),
          jsx('div', {
            className: 'flex items-center gap-2 abyss-micro text-(--ui-text-tertiary) abyss-mono tabular-nums',
            children: [
              jsx(Badge, { variant: 'outline', size: 'xs', children: `${nodeCount} nodes` }),
              jsx(Badge, { variant: 'outline', size: 'xs', children: `${edgeCount} edges` }),
              sessionTraceId && onOpenTrace && jsx(Button, {
                variant: 'ghost', size: 'sm',
                onClick: () => onOpenTrace(sessionTraceId),
                title: 'Open this session trace',
                'aria-label': `Open trace for session ${String(sessionTraceId).slice(0, 8)}`,
                className: 'abyss-tiny abyss-mono h-6 px-1.5',
                children: 'trace ›'
              }),
              jsx(Button, {
                variant: 'ghost', size: 'sm',
                onClick: fetchGraphData,
                title: 'Refresh graph',
                'aria-label': 'Refresh graph',
                children: jsx(Codicon, { name: 'refresh' })
              })
            ]
          })
        ]
      }),
      jsx('div', {
        ref: containerRef,
        className: 'flex-1 relative rounded-lg overflow-hidden bg-(--ui-bg-editor) abyss-row-hover',
        // m-1 is NOT compiled in the live host bundle (index-ChgG27Ex.css) —
        // the intended 4px breathing margin around the dithered graph ground
        // silently vanished, leaving the canvas flush with the pane edges.
        // Applied inline per DESIGN.md (inline styles for values with no class).
        style: { margin: 4 },
        children: loading ? jsxs('div', {
          className: 'absolute inset-0 flex items-center justify-center',
          children: [
            jsx(GlyphSpinner, { ariaLabel: 'Building brain graph', className: 'text-(--ui-text-tertiary)' }),
            jsx('span', { className: 'ml-2 text-sm text-(--ui-text-secondary) abyss-mono', children: 'building brain…' })
          ]
        }) : error ? jsx(ErrorState, {
          title: 'Graph unavailable',
          description: error,
          children: jsx(Button, { variant: 'secondary', size: 'sm', onClick: fetchGraphData, children: 'Retry' })
        }) : !nodeCount ? jsx(EmptyState, {
          title: 'No graph data',
          description: 'Graph data will appear once activity is recorded.'
        }) : jsx('canvas', {
          ref: canvasRef,
          className: 'w-full h-full cursor-crosshair',
          role: 'application',
          tabIndex: 0,
          'aria-label': `Hermes brain graph: ${nodeCount} nodes, ${edgeCount} edges${selectedNode ? `, selected ${selectedNode.label || selectedNode.id}` : ''} — arrow keys move selection, Enter opens the session trace`
        })
      }),
      !loading && nodeCount > 0 && jsxs('div', {
        className: 'px-3 py-1.5 border-t border-(--ui-stroke-tertiary) flex gap-4 abyss-micro flex-wrap items-center',
        children: [
          ...legend.map(item =>
            jsxs('div', {
              key: item.label,
              className: 'flex items-center gap-1',
              children: [
                jsx('span', { className: 'inline-block h-1.5 w-1.5 rounded-full', style: item.style, children: '' }),
                jsx('span', { className: 'text-(--ui-text-tertiary)', children: item.label })
              ]
            })
          ),
          jsx('span', {
            className: 'ml-auto text-(--ui-text-quaternary) abyss-mono select-none',
            children: 'click: select · arrows: move · enter: trace (session)'
          })
        ]
      })
    ]
  })
}

// --- Signals & Incidents (triage wired) ---
const SeverityBadge = ({ severity }) => {
  const variants = { critical: 'destructive', error: 'destructive', warning: 'warn', info: 'outline' }
  return jsx(Badge, {
    variant: variants[severity] || 'outline',
    size: 'xs',
    className: 'uppercase tracking-wider abyss-mono',
    children: severity
  })
}

function SignalsIncidentsView({ ctx, onOpenTrace }) {
  const [activeTab, setActiveTab] = useState('signals')
  const [signals, setSignals] = useState([])
  const [incidents, setIncidents] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [busyId, setBusyId] = useState(null)
  const [clustering, setClustering] = useState(false)
  const [clusterError, setClusterError] = useState(null)
  // Visible triage error surface: a failed acknowledge/resolve/reopen/close
  // must never be a silent no-op (DESIGN.md States — errors surface as
  // recovery paths). Carries the row that failed + action; cleared when a
  // new action starts (retry) and never set on success.
  const [actionError, setActionError] = useState(null)

  const fetchSignals = useCallback(async () => {
    if (!ctx) return
    setLoading(true)
    setError(null)
    try {
      const data = await ctx.rest('/signals?limit=50', { method: 'GET', timeoutMs: 5000 })
      setSignals(Array.isArray(data) ? data : [])
    } catch (e) {
      console.error('Failed to fetch signals:', e)
      setError(String(e?.message || e))
      // Preserve the cached signal list on background poll failure: an 8s
      // resolution-status poll blipping the network would otherwise clear the
      // triage the operator is reading. The ErrorState only surfaces when the
      // cached list is also empty (see the early return below).
    } finally {
      setLoading(false)
    }
  }, [ctx])

  const fetchIncidents = useCallback(async () => {
    if (!ctx) return
    setLoading(true)
    setError(null)
    try {
      const data = await ctx.rest('/incidents?limit=50', { method: 'GET', timeoutMs: 5000 })
      setIncidents(Array.isArray(data) ? data : [])
    } catch (e) {
      console.error('Failed to fetch incidents:', e)
      setError(String(e?.message || e))
      // Same preserve-on-blink policy as fetchSignals above.
    } finally {
      setLoading(false)
    }
  }, [ctx])

  const clusterIncidents = useCallback(async () => {
    if (!ctx || clustering) return
    setClustering(true)
    setClusterError(null)
    try {
      await ctx.rest('/incidents/cluster', { method: 'POST', timeoutMs: 10000 })
      fetchIncidents()
    } catch (e) {
      console.error('Failed to cluster incidents:', e)
      setClusterError('cluster failed — incidents were not updated')
    } finally {
      setClustering(false)
    }
  }, [ctx, fetchIncidents, clustering])

  const fetchData = useCallback(async () => {
    // Fetch BOTH lists on every cycle so the tab counters ('signals (N)' /
    // 'incidents (N)') are truthful from the first paint. The former
    // active-tab-only fetch left the UNVISITED tab's counter at a
    // misleading 0 until the operator clicked it — a false zero on the
    // triage surface. Both read the same SQLite store (limit=50, cheap),
    // and their catch blocks preserve cached rows on a background poll
    // blip (the skeleton guard below only fires when both lists are gone).
    // Keeping activeTab in the deps preserves the immediate refetch on tab
    // switch (fresh data for the newly-visible list); the effect's interval
    // restarts with it, same cadence as before.
    await Promise.all([fetchSignals(), fetchIncidents()])
  }, [activeTab, fetchSignals, fetchIncidents])

  useEffect(() => {
    // Live watch: re-poll the active tab every 30s (cadence parity with
    // StatusStrip/ActivityFeed/WaveView) so signals arriving from a running
    // cron job surface without remounting the view. The loading guard above
    // only renders the skeleton when the cached list is also gone, so the
    // background poll silently refreshes the rows in place.
    fetchData()
    const t = setInterval(fetchData, 30000)
    return () => clearInterval(t)
  }, [fetchData])

  const runAction = useCallback(async (kind, id, action) => {
    if (!ctx || busyId) return
    setActionError(null)
    setBusyId(`${kind}:${id}:${action}`)
    try {
      await ctx.rest(`/${kind}/${id}/${action}`, { method: 'POST', timeoutMs: 5000 })
      if (kind === 'signals') await fetchSignals()
      else await fetchIncidents()
    } catch (e) {
      console.error(`Failed to ${action} ${kind.slice(0, -1)}:`, e)
      setActionError({ kind, id, action, message: String(e?.message || e) })
    } finally {
      setBusyId(null)
    }
  }, [ctx, busyId, fetchSignals, fetchIncidents])

  // Agent-powered resolve: dispatch a free-Nous Hermes agent to diagnose + fix
  // the issue on the backend. The backend marks it resolved only from the
  // agent's report — the button no longer just makes the row disappear.
  const resolveAgent = useCallback(async (kind, id) => {
    if (!ctx || busyId) return
    setActionError(null)
    setBusyId(`${kind}:${id}:resolve-agent`)
    try {
      await ctx.rest(`/${kind}/${id}/resolve-agent`, { method: 'POST', timeoutMs: 8000 })
      if (kind === 'signals') await fetchSignals()
      else await fetchIncidents()
    } catch (e) {
      console.error(`Failed to dispatch resolver for ${kind.slice(0, -1)}:`, e)
      setActionError({ kind, id, action: 'resolve-agent', message: String(e?.message || e) })
    } finally {
      setBusyId(null)
    }
  }, [ctx, busyId, fetchSignals, fetchIncidents])

  // While any resolution is running, poll so the row shows live progress.
  const runningCount = useMemo(
    () => [...signals, ...incidents].filter(x => x.resolution_status === 'running').length,
    [signals, incidents]
  )
  useEffect(() => {
    if (runningCount === 0) return
    const t = setInterval(() => { fetchSignals(); fetchIncidents() }, 8000)
    return () => clearInterval(t)
  }, [runningCount, fetchSignals, fetchIncidents])

  if (loading && signals.length === 0 && incidents.length === 0) {
    // Skeleton mirrors the watch row list: dots + badge line + two text
    // lines per row, so the first paint matches what loads instead of a
    // single hollow bar (DESIGN.md "Loading: pulsing skeleton blocks").
    return jsx('div', { className: 'p-3', children: jsxs('div', { className: 'space-y-2', children: Array.from({ length: 4 }).map((_, i) =>
      jsxs('div', { key: i, className: 'flex items-start gap-2.5', children: [
        jsx('div', { className: 'mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-(--ui-bg-tertiary) animate-pulse abyss-mute-pulse' }),
        jsxs('div', { className: 'flex-1 space-y-1', children: [
          jsx('div', { className: 'h-3 bg-(--ui-bg-tertiary) rounded animate-pulse abyss-mute-pulse', style: { width: '66.666667%' } }),
          jsx('div', { className: 'h-2.5 bg-(--ui-bg-tertiary) rounded animate-pulse abyss-mute-pulse', style: { width: '50%' } })
        ] })
      ] })
    ) }) })
  }

  if (error && signals.length === 0 && incidents.length === 0) {
    return jsx(ErrorState, {
      title: 'Signals unavailable',
      description: error,
      children: jsx(Button, { variant: 'secondary', size: 'sm', onClick: fetchData, children: 'Retry' })
    })
  }

  // Counter honesty (tick-23/29/34 policy): /signals and /incidents are
  // fetched with limit=50, so a list pinned at exactly 50 rows usually means
  // MORE exist beyond the visible sample — the StatusStrip's SIG metric and
  // the INC metric carry the exact OPEN totals from /status. Printing a bare
  // `signals (50)` next to a strip that says `200 open` would be a false
  // count. The cap is disclosed as `50+`, and a title on each tab points the
  // operator at the strip for the exact total. A list under 50 is a true
  // count, so it renders without the marker.
  const signalCap = signals.length >= 50
  const incidentCap = incidents.length >= 50
  const signalLabel = `signals (${signals.length}${signalCap ? '+' : ''})`
  const incidentLabel = `incidents (${incidents.length}${incidentCap ? '+' : ''})`

  return jsxs('div', {
    className: 'flex h-full flex-col',
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between px-3 py-2 border-b border-(--ui-stroke-tertiary)',
        children: [
          jsxs('div', {
            className: 'flex items-center gap-2',
            children: [
              jsx(Codicon, { name: 'warning', className: 'text-(--ui-text-secondary)' }),
              jsx('span', { className: 'text-sm font-medium uppercase tracking-wider text-(--ui-text-primary) abyss-mono', children: 'watch' })
            ]
          }),
          jsxs('div', {
            className: 'flex items-center gap-2',
            children: [
              clusterError && jsx('span', { className: 'abyss-micro text-(--ui-red) abyss-mono', children: clusterError }),
              jsx(Button, {
                variant: 'ghost', size: 'sm',
                disabled: clustering,
                onClick: clusterIncidents,
                title: clustering ? 'Clustering incidents…' : 'Run incident clustering',
                'aria-label': clustering ? 'Clustering incidents' : 'Run incident clustering',
                // Busy affordance: a disabled state alone is silent feedback
                // while the 10s cluster dispatch runs — a spinner announces
                // the mutation (DESIGN.md Busy: buttons disabled + visible
                // motion instead of a static grey-out).
                children: clustering
                  ? jsx(GlyphSpinner, { ariaLabel: 'Clustering incidents', className: 'text-(--ui-text-secondary)' })
                  : jsx(Codicon, { name: 'combine' })
              })
            ]
          })
        ]
      }),
      jsx('div', {
        className: 'flex gap-1 px-3 py-1 border-b border-(--ui-stroke-tertiary)',
        children: [
          jsx(Button, {
            variant: activeTab === 'signals' ? 'default' : 'ghost',
            size: 'sm',
            onClick: () => setActiveTab('signals'),
            'aria-pressed': activeTab === 'signals',
            title: signalCap ? 'showing up to 50 signals — the SIG metric in the strip shows the exact open total' : undefined,
            className: 'text-xs h-7 abyss-mono',
            children: signalLabel
          }),
          jsx(Button, {
            variant: activeTab === 'incidents' ? 'default' : 'ghost',
            size: 'sm',
            onClick: () => setActiveTab('incidents'),
            'aria-pressed': activeTab === 'incidents',
            title: incidentCap ? 'showing up to 50 incidents — the INC metric in the strip shows the exact open total' : undefined,
            className: 'text-xs h-7 abyss-mono',
            children: incidentLabel
          })
        ]
      }),
      jsx('div', {
        className: 'flex-1 overflow-y-auto',
        children: activeTab === 'signals' ? (
          signals.length === 0 ? jsx(EmptyState, {
            title: 'No signals detected',
            description: 'The abyss is calm. All agent behavior looks normal.'
          }) : jsx('div', {
            className: 'flex flex-col',
            children: signals.map((s, idx) => {
              const resolved = s.resolved
              const acknowledged = s.acknowledged
              // Prefix-match the busy key (signals:{id}:acknowledge|resolve|
              // resolve-agent): the old exact match never matched ':resolve-
              // agent', so the cloud-resolve button stayed enabled while its
              // dispatch was in flight and a double-click double-dispatched.
              const busy = !!busyId && busyId.startsWith(`signals:${s.id}:`)
              return jsxs('div', {
                key: s.id,
                className: cn(
                  'px-3 py-2 abyss-row-hover',
                  idx < signals.length - 1 && 'border-b border-(--ui-stroke-tertiary)'
                ),
                children: [
                  jsxs('div', {
                    className: 'flex items-start gap-2.5',
                    children: [
                      jsx('span', {
                        className: 'inline-block h-1.5 w-1.5 rounded-full mt-1.5 shrink-0',
                        style: {
                          backgroundColor: s.severity === 'critical' || s.severity === 'error' ? 'var(--ui-red)'
                            : s.severity === 'warning' ? 'var(--ui-yellow)' : 'var(--ui-blue)'
                        },
                        children: ''
                      }),
                      jsxs('div', {
                        className: 'flex-1 min-w-0',
                        children: [
                          jsxs('div', {
                            className: 'flex items-center gap-2 mb-1 flex-wrap',
                            children: [
                              jsx(SeverityBadge, { severity: s.severity }),
                              jsx(Badge, { variant: 'outline', size: 'xs', className: 'uppercase tracking-wider abyss-mono', children: s.signal_type }),
                              jsx('span', { className: 'abyss-micro text-(--ui-text-quaternary) abyss-mono', title: timeTitle(s.timestamp), children: relativeTime(s.timestamp) }),
                              resolved && jsx(Badge, { variant: 'default', size: 'xs', children: 'resolved' }),
                              acknowledged && !resolved && jsx(Badge, { variant: 'muted', size: 'xs', children: 'acknowledged' }),
                              s.resolution_status === 'running' && jsx(Badge, { variant: 'warn', size: 'xs', children: 'resolving…' }),
                              s.resolution_status === 'failed' && jsx(Badge, { variant: 'destructive', size: 'xs', children: 'fix failed' })
                            ]
                          }),
                          jsx('div', { className: 'font-medium text-sm text-(--ui-text-primary) abyss-mono', children: s.label }),
                          jsx('div', { className: 'text-xs text-(--ui-text-secondary) mt-0.5 abyss-mono', children: s.description }),
                          (s.resolution_status === 'succeeded' || s.resolution_status === 'failed') && s.resolution_note && jsx('div', {
                            className: 'abyss-micro text-(--ui-text-tertiary) mt-1 abyss-mono line-clamp-2',
                            title: s.resolution_note,
                            children: `fix: ${s.resolution_note}`
                          }),
                          (s.session_id || s.source) && jsxs('div', {
                            className: 'abyss-micro text-(--ui-text-quaternary) mt-1 abyss-mono flex items-center gap-2',
                            children: [
                              jsx('span', { className: 'truncate', title: `session: ${s.session_id || '—'}  source: ${s.source || '—'}`, children: `session: ${s.session_id?.slice(0, 8) || '—'}  source: ${s.source || '—'}` }),
                              s.session_id && onOpenTrace && jsx(Button, {
                                variant: 'ghost', size: 'xs',
                                onClick: () => onOpenTrace(s.session_id),
                                title: 'Open this session trace',
                                'aria-label': `Open trace for session ${s.session_id.slice(0, 8)}`,
                                className: 'abyss-tiny abyss-mono h-6 px-1.5 shrink-0',
                                children: 'trace ›'
                              })
                            ]
                          }),
                          !resolved && jsxs('div', {
                            className: 'flex gap-1.5 mt-1.5 flex-wrap',
                            children: [
                              jsx(Button, {
                                variant: 'outline', size: 'xs',
                                disabled: busy || acknowledged || s.resolution_status === 'running',
                                onClick: () => runAction('signals', s.id, 'acknowledge'),
                                children: acknowledged ? 'acknowledged' : 'acknowledge'
                              }),
                              jsx(Button, {
                                variant: 'secondary', size: 'xs',
                                disabled: busy || s.resolution_status === 'running',
                                onClick: () => resolveAgent('signals', s.id),
                                title: 'Dispatch a free-Nous cloud agent to diagnose and fix (observation stays local)',
                                children: s.resolution_status === 'running' ? 'resolving…' : s.resolution_status === 'failed' ? 'retry fix' : 'resolve (cloud agent)'
                              }),
                              s.id === actionError?.id && actionError?.kind === 'signals' && jsx('div', {
                                className: 'w-full shrink-0 abyss-micro text-(--ui-red) abyss-mono mt-0.5',
                                children: `✗ ${actionError.action} failed — ${actionError.message}`
                              })
                            ]
                          })
                        ]
                      })
                    ]
                  })
                ]
              })
            })
          })
        ) : (
          incidents.length === 0 ? jsx(EmptyState, {
            title: 'No incidents',
            description: 'No clustered incidents found. Run cluster to detect patterns.'
          }) : jsx('div', {
            className: 'flex flex-col',
            children: incidents.map((i, idx) => {
              // Same prefix-match policy as the signal rows above: any running
              // mutation on THIS incident disables all of its row buttons.
              const busy = !!busyId && busyId.startsWith(`incidents:${i.id}:`)
              const isOpen = i.status === 'open'
              const isAcked = i.status === 'acknowledged'
              const isResolved = i.status === 'resolved'
              const isClosed = i.status === 'closed'
              const showResolve = isOpen || isAcked
              return jsxs('div', {
                key: i.id,
                className: cn(
                  'px-3 py-2.5 abyss-row-hover',
                  idx < incidents.length - 1 && 'border-b border-(--ui-stroke-tertiary)'
                ),
                children: [
                  jsxs('div', {
                    className: 'flex items-center justify-between mb-1.5',
                    children: [
                      jsx(SeverityBadge, { severity: i.severity }),
                      jsxs('div', {
                        className: 'flex items-center gap-1.5',
                        children: [
                          jsx(Badge, {
                            variant: isOpen ? 'destructive' : isClosed ? 'muted' : 'default',
                            size: 'xs',
                            className: 'uppercase tracking-wider abyss-mono',
                            children: i.status
                          }),
                          i.resolution_status === 'running' && jsx(Badge, { variant: 'warn', size: 'xs', children: 'resolving…' }),
                          i.resolution_status === 'failed' && jsx(Badge, { variant: 'destructive', size: 'xs', children: 'fix failed' })
                        ]
                      })
                    ]
                  }),
                  jsx('div', { className: 'font-medium text-sm text-(--ui-text-primary) abyss-mono', children: i.title }),
                  jsx('div', { className: 'text-xs text-(--ui-text-secondary) mt-1 abyss-mono', children: i.description }),
                  (i.resolution_status === 'succeeded' || i.resolution_status === 'failed') && i.resolution_note && jsx('div', {
                    className: 'abyss-micro text-(--ui-text-tertiary) mt-1 abyss-mono line-clamp-2',
                    title: i.resolution_note,
                    children: `fix: ${i.resolution_note}`
                  }),
                  jsx('div', {
                    className: 'flex gap-4 mt-1.5 abyss-micro text-(--ui-text-quaternary) abyss-mono tabular-nums',
                    children: [
                      jsx('span', { children: `signals: ${i.signal_count}` }),
                      jsx('span', { children: `pattern: ${i.pattern || '—'}` }),
                      jsx('span', { title: timeTitle(i.created_at), children: `created: ${relativeTime(i.created_at)}` })
                    ]
                  }),
                  firstSessionId(i.session_ids) && onOpenTrace && jsx('div', {
                    className: 'mt-1',
                    children: jsx(Button, {
                      variant: 'ghost', size: 'xs',
                      onClick: () => onOpenTrace(firstSessionId(i.session_ids)),
                      title: 'Open this incident session trace',
                      'aria-label': 'Open trace for incident session',
                      className: 'abyss-tiny abyss-mono h-6 px-1.5',
                      children: 'trace ›'
                    })
                  }),
                  jsxs('div', {
                    className: 'flex gap-1.5 mt-2 flex-wrap',
                    children: [
                      showResolve && jsx(Button, {
                        variant: 'secondary', size: 'xs',
                        disabled: busy || i.resolution_status === 'running',
                        onClick: () => resolveAgent('incidents', i.id),
                        title: 'Dispatch a free-Nous cloud agent to diagnose and fix (observation stays local)',
                        children: i.resolution_status === 'running' ? 'resolving…' : i.resolution_status === 'failed' ? 'retry fix' : 'resolve (cloud agent)'
                      }),
                      isOpen && jsx(Button, {
                        variant: 'outline', size: 'xs',
                        disabled: busy,
                        onClick: () => runAction('incidents', i.id, 'acknowledge'),
                        children: 'acknowledge'
                      }),
                      (isResolved || isClosed) && jsx(Button, {
                        variant: 'outline', size: 'xs',
                        disabled: busy,
                        onClick: () => runAction('incidents', i.id, 'reopen'),
                        children: 'reopen'
                      }),
                      (isOpen || isAcked || isResolved) && jsx(Button, {
                        variant: 'ghost', size: 'xs',
                        disabled: busy,
                        onClick: () => runAction('incidents', i.id, 'close'),
                        children: 'close'
                      }),
                      i.id === actionError?.id && actionError?.kind === 'incidents' && jsx('div', {
                        className: 'w-full shrink-0 abyss-micro text-(--ui-red) abyss-mono mt-0.5',
                        children: `✗ ${actionError.action} failed — ${actionError.message}`
                      })
                    ]
                  })
                ]
              })
            })
          })
        )
      })
    ]
  })
}

// --- Main Dashboard ---
function HealthView({ ctx }) {
  const [health, setHealth] = useState(null)
  const [trends, setTrends] = useState(null)
  const [failures, setFailures] = useState(null)
  // /status liveness metadata — the score is computed from 7-day windows,
  // so it can look confident while hooks have been dark for hours; the
  // header discloses the silence since last recorded activity (tick-42
  // glance parity, tick-43).
  const [status, setStatus] = useState(null)
  const [loading, setLoading] = useState(true)
  // Doctor flow: idle | running | review | applying | done | error
  const [doctorPhase, setDoctorPhase] = useState('idle')
  const [doctorReport, setDoctorReport] = useState(null)
  const [doctorReportId, setDoctorReportId] = useState(null)
  const [doctorError, setDoctorError] = useState(null)
  // Wall-clock start of the in-flight phase — a backstop so 'running' /
  // 'applying' can NEVER spin forever even if the backend never writes a
  // terminal report state (agent lost, backend restarted mid-flow, etc.).
  const [doctorStartedAt, setDoctorStartedAt] = useState(null)
  const [doctorLog, setDoctorLog] = useState('')
  const doctorLogRef = useRef(null)
  // Benchmark (Abyss Bench Layer 1 probe suite)
  const [benchmark, setBenchmark] = useState(null)
  const [benchmarkRunning, setBenchmarkRunning] = useState(false)
  const [benchmarkError, setBenchmarkError] = useState(null)

  const fetchAll = useCallback(async () => {
    if (!ctx) return
    setLoading(true)
    try {
      // Guarded fetches (WaveView precedent): one flaky endpoint must NOT take
      // down the whole health view. Previously Promise.all rejected as a unit,
      // so a /trends or /failures blip threw away a perfectly good /health
      // payload and printed the misleading "did not return a health score"
      // ErrorState — burying the doctor/benchmark controls behind one flaky
      // surface. Now each surface degrades independently: a failed /trends
      // simply omits its section (partial failures stay silent), and the
      // !health ErrorState below only fires when /health itself failed.
      const guard = (p) => p.catch(() => null)
      const [h, t, f, st] = await Promise.all([
        guard(ctx.rest('/health', { method: 'GET', timeoutMs: 5000 })),
        guard(ctx.rest('/trends?days=7&bucket=day', { method: 'GET', timeoutMs: 5000 })),
        guard(ctx.rest('/failures?limit=8', { method: 'GET', timeoutMs: 5000 })),
        guard(ctx.rest('/status', { method: 'GET', timeoutMs: 5000 }))
      ])
      if (h && typeof h === 'object') setHealth(h)
      setTrends(t && typeof t === 'object' ? t : null)
      setFailures(f && typeof f === 'object' ? f : null)
      if (st && typeof st === 'object') setStatus(st)
    } catch (e) {
      console.error('abyss: health fetch failed', e)
    } finally {
      setLoading(false)
    }
  }, [ctx])

  useEffect(() => {
    // Live health: re-poll every 30s (cadence parity with StatusStrip /
    // ActivityFeed / Watch / Calendar / Trace-agent surfaces; Wave runs at
    // 15s). The health score previously froze at mount — the exact tab that
    // answers "are my agents OK right now?" went stale while every other
    // pane refreshed. The skeleton guard below only fires when NOTHING is
    // cached, so the background poll silently refreshes in place (no-blink,
    // tick-2 policy).
    fetchAll()
    const t = setInterval(fetchAll, 30000)
    return () => clearInterval(t)
  }, [fetchAll])

  // Doctor: dispatch the agent, review its diagnosis, approve fixes.
  const runDoctor = useCallback(async () => {
    if (!ctx || doctorPhase === 'running' || doctorPhase === 'applying') return
    setDoctorError(null)
    setDoctorReport(null)
    try {
      const r = await ctx.rest('/doctor/run', { method: 'POST', timeoutMs: 8000 })
      if (!r || !r.report_id) {
        setDoctorError(String(r?.error || 'doctor dispatch failed'))
        setDoctorPhase('error')
        return
      }
      setDoctorReportId(r.report_id)
      setDoctorStartedAt(Date.now())
      setDoctorPhase('running')
    } catch (e) {
      setDoctorError(String(e?.message || e))
      setDoctorPhase('error')
    }
  }, [ctx, doctorPhase])

  const approveDoctor = useCallback(async () => {
    // Allowed in the 'review' phase (normal) and the 'error' phase (retry —
    // the report file persists on disk, so an interrupted approve can be
    // resumed without a fresh diagnosis run).
    if (!ctx || !doctorReportId || (doctorPhase !== 'review' && doctorPhase !== 'error')) return
    setDoctorError(null)
    try {
      const r = await ctx.rest('/doctor/approve', {
        method: 'POST',
        // NOTE: pass an OBJECT — the desktop IPC layer (fetchJson) always
        // JSON.stringify's the body itself; pre-stringifying here double-
        // encodes it and the backend fails with "'str' object has no
        // attribute 'get'".
        body: { report_id: doctorReportId },
        timeoutMs: 20000
      })
      if (!r || r.status !== 'dispatched') {
        setDoctorError(String(r?.error || 'approve failed'))
        setDoctorPhase('error')
        return
      }
      setDoctorStartedAt(Date.now())
      setDoctorPhase('applying')
    } catch (e) {
      setDoctorError(String(e?.message || e))
      setDoctorPhase('error')
    }
  }, [ctx, doctorPhase, doctorReportId])

  const dismissDoctor = useCallback(() => {
    setDoctorPhase('idle')
    setDoctorReport(null)
    setDoctorReportId(null)
    setDoctorError(null)
  }, [])

  // Resume the most recent completed diagnosis without re-running the agent
  // (survives reloads, interrupted approves, transient failures).
  const resumeDoctor = useCallback(async () => {
    if (!ctx) return
    try {
      const r = await ctx.rest('/doctor/last', { method: 'GET', timeoutMs: 5000 })
      if (r && r.status === 'ready' && r.report) {
        setDoctorReportId(r.report_id)
        setDoctorReport(r.report)
        setDoctorError(null)
        // Show the approve button whenever proposed fixes remain (including
        // partial apply runs that still have un-applied fixes).
        const remaining = (r.report.proposed_fixes || []).filter(
          f => !(r.report.fixes || []).some(x => x.id === f.id && x.status === 'applied')
        ).length
        setDoctorPhase(remaining > 0 ? 'review' : 'done')
      } else {
        setDoctorError('no previous diagnosis to resume — run doctor first')
        setDoctorPhase('error')
      }
    } catch (e) {
      setDoctorError(String(e?.message || e))
      setDoctorPhase('error')
    }
  }, [ctx])

  // Run the deterministic benchmark (Layer 1 probe suite) — scores whether the
  // doctor's fixes hold their regression gate.
  const runBenchmark = useCallback(async () => {
    if (!ctx || benchmarkRunning) return
    setBenchmarkRunning(true)
    setBenchmarkError(null)
    setBenchmark(null)
    try {
      const r = await ctx.rest('/benchmark/run', { method: 'POST', timeoutMs: 200000 })
      if (r && (r.status === 'ok' || r.status === 'failures')) {
        setBenchmark(r)
      } else {
        setBenchmarkError(String(r?.error || 'benchmark failed'))
      }
    } catch (e) {
      setBenchmarkError(String(e?.message || e))
    } finally {
      setBenchmarkRunning(false)
    }
  }, [ctx, benchmarkRunning])

  // Poll the doctor report while a phase is in flight (8s cadence).
  useEffect(() => {
    if ((doctorPhase !== 'running' && doctorPhase !== 'applying') || !doctorReportId || !ctx) return
    const poll = async () => {
      try {
        // Backstop: never let the phase spin forever even if the backend
        // never writes a terminal state. Doctor: 25 min (agent timeout is
        // 20 min). Apply: 45 min (same agent timeout + application work).
        const maxMs = doctorPhase === 'running' ? 25 * 60 * 1000 : 45 * 60 * 1000
        if (doctorStartedAt && Date.now() - doctorStartedAt > maxMs) {
          setDoctorError(doctorPhase === 'running'
            ? 'doctor agent did not finish within 25 minutes — dismiss and run doctor again'
            : 'apply agent did not finish within 45 minutes — dismiss and retry approve & fix')
          setDoctorPhase('error')
          return
        }
        const r = await ctx.rest(`/doctor/report?report_id=${encodeURIComponent(doctorReportId)}`, { method: 'GET', timeoutMs: 5000 })
        if (!r || !r.report) return
        const rep = r.report
        setDoctorReport(rep)
        if (doctorPhase === 'running') {
          if (rep.status === 'failed') {
            setDoctorError(rep.summary || rep.error || 'doctor agent failed')
            setDoctorPhase('error')
          } else {
            setDoctorPhase('review')
          }
        } else if (doctorPhase === 'applying') {
          if (rep.fixes && rep.fixes.length) setDoctorPhase('done')
          else if (rep.status === 'failed') {
            setDoctorError(rep.summary || rep.error || 'apply agent failed')
            setDoctorPhase('error')
          }
        }
      } catch (e) {
        console.error('abyss: doctor poll failed', e)
      }
    }
    poll()
    const t = setInterval(poll, 8000)
    return () => clearInterval(t)
  }, [doctorPhase, doctorReportId, doctorStartedAt, ctx])

  // Stream the doctor agent's live stdout log into an embedded box while a
  // phase is in flight (2.5s cadence) — turns a silent "running" into a
  // visible transcript the operator can watch.
  useEffect(() => {
    if ((doctorPhase !== 'running' && doctorPhase !== 'applying') || !doctorReportId || !ctx) return
    let alive = true
    const poll = async () => {
      try {
        const r = await ctx.rest(`/doctor/log?report_id=${encodeURIComponent(doctorReportId)}`, { method: 'GET', timeoutMs: 5000 })
        if (alive && r && r.status === 'ok' && typeof r.log === 'string') setDoctorLog(r.log)
      } catch (e) {
        // transient — keep the last tail
      }
    }
    poll()
    const t = setInterval(poll, 2500)
    return () => { alive = false; clearInterval(t) }
  }, [doctorPhase, doctorReportId, ctx])

  // Keep the live tail pinned to the newest output.
  useEffect(() => {
    const el = doctorLogRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [doctorLog])

  // NOTE: hooks must be declared BEFORE any early return — React counts hook
  // calls per render, and a hook that only runs on the loaded-data render
  // (skipped during `loading`) throws React error 310 "Rendered more hooks
  // than during the previous render" and crashes the whole dashboard page.
  const trendMax = useMemo(() => {
    if (!trends) return 1
    return Math.max(1, ...(trends.activity || []), ...(trends.errors || []))
  }, [trends])

  if (loading && !health && !trends && !failures) {
    // Skeleton only on the FIRST load (or a full-cache wipe): every 30s
    // background poll flips `loading` back to true momentarily (tick-17
    // fetchAll starts with setLoading(true)), and without the cached-data
    // guard the whole health report would punch out to a skeleton every
    // half minute. With cached health/trends/failures present, the poll
    // silently refreshes in place — the doctor/benchmark report never has
    // to re-mount (its phase state lives above, unharmed).
    return jsx('div', {
      className: 'p-3 space-y-3',
      children: jsxs('div', { className: 'space-y-3', children: [
        jsx('div', { className: 'h-8 w-full bg-(--ui-bg-tertiary) rounded animate-pulse abyss-mute-pulse' }),
        jsx('div', { className: 'space-y-1.5', children: Array.from({ length: 4 }).map((_, i) =>
          jsx('div', { key: i, className: 'flex items-center gap-2', children: [
            jsx('div', { className: 'h-4 w-24 bg-(--ui-bg-tertiary) rounded animate-pulse abyss-mute-pulse' }),
            jsx('div', { className: 'h-2 flex-1 rounded bg-(--ui-bg-tertiary) overflow-hidden animate-pulse abyss-mute-pulse' })
          ] })
        ) }),
        jsx('div', { className: 'flex items-end gap-1', style: { height: '4rem' }, children: Array.from({ length: 7 }).map((_, i) =>
          jsx('div', { key: i, className: 'flex-1 rounded-sm bg-(--ui-bg-tertiary) animate-pulse abyss-mute-pulse', style: { height: `${30 + ((i * 37) % 60)}%` } })
        ) })
      ] })
    })
  }
  if (!health) {
    return jsx(ErrorState, {
      title: 'Health unavailable',
      description: 'The Abyss backend did not return a health score.',
      children: jsx(Button, { variant: 'secondary', size: 'sm', onClick: fetchAll, children: 'Retry' })
    })
  }

  const score = health.score
  const level = health.level
  const levelTone = level === 'critical' ? 'text-(--ui-red)'
    : level === 'degraded' ? 'text-(--ui-yellow)'
    : level === 'fair' ? 'text-(--ui-yellow)'
    : 'text-(--ui-green)'

  const comps = health.components || {}
  const counts = health.counts || {}

  const compRows = [
    { label: 'error score', value: comps.error_score ?? 0, max: 40 },
    { label: 'signals', value: comps.signal_score ?? 0, max: 25 },
    { label: 'incidents', value: comps.incident_score ?? 0, max: 25 },
    { label: 'liveliness', value: comps.activity_score ?? 0, max: 10 }
  ]

  const failureLists = [
    { title: 'by type', items: failures?.by_type || [] },
    { title: 'by tool', items: failures?.by_tool || [] },
    { title: 'common errors', items: failures?.by_message || [] }
  ]

  // Data-freshness disclosure (tick-42 glance parity): the score and every
  // count here are computed from 7-day windows — when hooks stopped
  // recording long ago (gateway down, plugin misload, session death), the
  // number can sit at a confident-looking 46 even though the system has
  // been dark for hours. The header prints the silence since last recorded
  // activity, same tones as the StatusStrip verdict (null while fresh —
  // no suffix noise; yellow in the quiet window; red past an hour).
  const idle = idleLabel(status?.last_activity_at)
  const idleEl = idle ? jsx('span', {
    className: cn('shrink-0 abyss-tiny uppercase tracking-widest', idle.tone),
    title: `last recorded activity ${timeTitle(status.last_activity_at)} — hooks may have stopped firing`,
    children: `· idle ${idle.text}`
  }) : null

  return jsxs('div', {
    className: 'flex h-full flex-col overflow-auto',
    children: [
      // Header: terminal status line — score, level, counters, actions
      jsxs('div', {
        className: 'px-4 py-3 border-b border-(--ui-stroke-tertiary) flex items-center gap-3 flex-wrap',
        children: [
          jsx('span', { className: 'text-xs uppercase tracking-widest text-(--ui-text-quaternary) abyss-mono select-none', children: '$ abyss health' }),
          jsxs('span', { className: 'flex items-baseline gap-1', children: [
            jsx('span', { className: cn('text-xl font-bold abyss-mono tabular-nums', levelTone), children: score }),
            jsx('span', { className: 'abyss-tiny text-(--ui-text-quaternary) abyss-mono', children: '/100' })
          ]}),
          jsxs('span', { className: 'flex items-center gap-1.5 text-xs abyss-mono', children: [
            jsx('span', { className: 'inline-block h-2.5 w-2.5 rounded-full', style: { backgroundColor: level === 'critical' ? 'var(--ui-red)' : (level === 'degraded' || level === 'fair') ? 'var(--ui-yellow)' : 'var(--ui-green)' }, children: '' }),
            jsx('span', { className: cn('capitalize', levelTone), children: level })
          ]}),
          // fmtCount (tick-47): these lifetime/24h totals exceed four digits
          // at current data volumes (1,073 errors · 4,437 open signals).
          jsx('span', { className: 'text-xs text-(--ui-text-tertiary) abyss-mono tabular-nums', children: `${fmtCount(counts.errors ?? 0)} errors · ${fmtCount(counts.signals_open ?? 0)} open signals · ${fmtCount(counts.incidents_open ?? 0)} open incidents · ${fmtCount(counts.activity_24h ?? 0)} actions/24h` }),
          idleEl,
          jsxs('div', {
            className: 'ml-auto flex items-center gap-1.5',
            children: [
              jsx(Button, {
                variant: 'outline', size: 'sm',
                className: 'abyss-mono shrink-0',
                disabled: doctorPhase === 'running' || doctorPhase === 'applying',
                onClick: runDoctor,
                title: 'Run the doctor agent: full overarching diagnosis with approval-gated fixes',
                children: doctorPhase === 'running' ? 'diagnosing…' : doctorPhase === 'applying' ? 'applying…' : 'doctor'
              }),
              doctorPhase === 'idle' && jsx(Button, {
                variant: 'ghost', size: 'sm',
                className: 'abyss-mono shrink-0',
                onClick: resumeDoctor,
                title: 'Load the most recent diagnosis report without re-running the doctor',
                children: 'resume last'
              }),
              jsx(Button, {
                variant: 'ghost', size: 'sm',
                className: 'abyss-mono shrink-0',
                disabled: benchmarkRunning,
                onClick: runBenchmark,
                title: 'Run the deterministic benchmark — scores whether the doctor fixes hold their regression gate',
                children: benchmarkRunning ? 'benchmarking…' : 'benchmark'
              })
            ]
          })
        ]
      }),
      // Doctor — agent-powered diagnosis with approval-gated fixes
      (doctorPhase !== 'idle' || doctorReport) && jsxs('div', {
        className: 'px-4 py-3 border-b border-(--ui-stroke-tertiary)',
        children: [
          jsxs('div', {
            className: 'flex items-center justify-between mb-2',
            children: [
              jsx('div', { className: 'text-xs uppercase tracking-widest text-(--ui-text-quaternary) abyss-mono', children: 'doctor' }),
              jsx(Button, { variant: 'ghost', size: 'xs', onClick: dismissDoctor, children: 'dismiss' })
            ]
          }),
          doctorPhase === 'running' && jsxs('div', {
            className: 'text-sm text-(--ui-text-secondary)',
            children: [
              jsxs('div', {
                className: 'flex items-center gap-2',
                children: [
                  jsx(GlyphSpinner, { ariaLabel: 'Doctor agent running', className: 'text-(--ui-accent)' }),
                  jsx('span', { children: `doctor agent diagnosing… ${doctorReportId ? `(${doctorReportId})` : ''}` })
                ]
              }),
              jsx('pre', {
                ref: doctorLogRef,
                className: 'mt-2 overflow-y-auto rounded border border-(--ui-stroke-tertiary) bg-(--ui-bg-editor) p-2 abyss-micro leading-snug text-(--ui-text-tertiary) abyss-mono whitespace-pre-wrap',
                style: { maxHeight: '11rem' },
                children: doctorLog || 'waiting for agent output…'
              })
            ]
          }),
          doctorPhase === 'applying' && jsxs('div', {
            className: 'text-sm text-(--ui-text-secondary)',
            children: [
              jsxs('div', {
                className: 'flex items-center gap-2',
                children: [
                  jsx(GlyphSpinner, { ariaLabel: 'Applying approved fixes', className: 'text-(--ui-accent)' }),
                  jsx('span', { children: 'applying approved fixes…' })
                ]
              }),
              jsx('pre', {
                ref: doctorLogRef,
                className: 'mt-2 overflow-y-auto rounded border border-(--ui-stroke-tertiary) bg-(--ui-bg-editor) p-2 abyss-micro leading-snug text-(--ui-text-tertiary) abyss-mono whitespace-pre-wrap',
                style: { maxHeight: '11rem' },
                children: doctorLog || 'waiting for agent output…'
              })
            ]
          }),
          doctorPhase === 'error' && doctorError && jsxs('div', {
            className: 'text-xs text-(--ui-red) mb-2',
            children: [
              jsx('div', { className: 'mb-2', children: doctorError }),
              jsxs('div', {
                className: 'flex gap-2',
                children: [
                  doctorReport && jsx(Button, {
                    variant: 'default', size: 'sm',
                    onClick: approveDoctor,
                    title: 'Retry dispatching the apply agent — the diagnosis report is still valid',
                    children: 'retry approve & fix'
                  }),
                  jsx(Button, { variant: 'ghost', size: 'sm', onClick: runDoctor, children: 'run doctor again' }),
                  jsx(Button, { variant: 'ghost', size: 'sm', onClick: dismissDoctor, children: 'dismiss' })
                ]
              })
            ]
          }),
          doctorReport && (doctorPhase === 'review' || doctorPhase === 'done') && jsxs('div', {
            children: [
              jsx('div', { className: 'text-xs text-(--ui-text-secondary) mb-2 abyss-mono', children: doctorReport.summary }),
              (doctorReport.findings || []).length > 0 && jsxs('div', {
                className: 'mb-2',
                children: [
                  jsx('div', { className: 'abyss-micro uppercase tracking-widest text-(--ui-text-quaternary) mb-1', children: 'findings' }),
                  (doctorReport.findings || []).map((f, idx) => jsx('div', {
                    key: idx,
                    className: 'text-xs text-(--ui-text-secondary) mb-1 flex gap-1.5',
                    children: [
                      jsx('span', { className: 'text-(--ui-text-quaternary) abyss-mono', children: '▸' }),
                      jsx('span', { className: 'abyss-mono', children: f.title })
                    ]
                  }))
                ]
              }),
              (doctorReport.proposed_fixes || []).length > 0 && doctorPhase === 'review' && jsxs('div', {
                className: 'mb-3',
                children: [
                  jsx('div', { className: 'abyss-micro uppercase tracking-widest text-(--ui-text-quaternary) mb-1', children: 'proposed fixes' }),
                  (doctorReport.proposed_fixes || []).map((fx, idx) => jsx('div', {
                    key: fx.id || idx,
                    className: 'text-xs text-(--ui-text-secondary) mb-1.5 border border-(--ui-stroke-tertiary) rounded p-2',
                    children: [
                      jsx('div', {
                        className: 'font-medium text-(--ui-text-primary) abyss-mono',
                        children: `${fx.title}${fx.target_signals?.length || fx.target_incidents?.length ? `  → ${fx.target_signals?.length || 0} sig / ${fx.target_incidents?.length || 0} inc` : ''}`
                      }),
                      fx.action && jsx('div', {
                        className: 'mt-0.5 text-(--ui-text-tertiary) line-clamp-2',
                        // Clamped to 2 lines but the full action text was
                        // unreachable — the doctor's remediation step is often
                        // longer than the clamp, and a post-mortem operator
                        // must read what the agent proposes (tick-13/24/38
                        // hover-hygiene parity for clamped dynamic data).
                        title: fx.action,
                        children: fx.action
                      })
                    ]
                  }))
                ]
              }),
              (doctorReport.fixes || []).length > 0 && doctorPhase === 'done' && jsxs('div', {
                className: 'mb-2',
                children: [
                  jsx('div', { className: 'abyss-micro uppercase tracking-widest text-(--ui-text-quaternary) mb-1', children: 'applied' }),
                  (doctorReport.fixes || []).map((fx, idx) => jsx('div', {
                    key: fx.id || idx,
                    className: 'text-xs mb-1 flex gap-1.5 abyss-mono',
                    children: [
                      jsx('span', {
                        className: fx.status === 'applied' ? 'text-(--ui-green)' : fx.status === 'failed' ? 'text-(--ui-red)' : 'text-(--ui-yellow)',
                        children: fx.status
                      }),
                      jsx('span', { className: 'text-(--ui-text-secondary)', children: `${fx.title || fx.id}: ${fx.note || ''}` })
                    ]
                  }))
                ]
              }),
              doctorPhase === 'review' && jsxs('div', {
                className: 'flex gap-2 mt-1',
                children: [
                  jsx(Button, {
                    variant: 'default', size: 'sm',
                    disabled: (doctorReport.proposed_fixes || []).length === 0,
                    onClick: approveDoctor,
                    title: 'Approve — a free-Nous agent applies these fixes on the backend',
                    children: `approve & fix (${(doctorReport.proposed_fixes || []).length})`
                  }),
                  jsx(Button, { variant: 'ghost', size: 'sm', onClick: dismissDoctor, children: 'later' })
                ]
              }),
              doctorPhase === 'done' && jsxs('div', {
                className: 'flex gap-2 mt-1',
                children: [
                  jsx(Button, { variant: 'ghost', size: 'sm', onClick: fetchAll, children: 'refresh' }),
                  jsx(Button, { variant: 'ghost', size: 'sm', onClick: dismissDoctor, children: 'dismiss' })
                ]
              })
            ]
          })
        ]
      }),
      // Benchmark — deterministic probe suite (scores the doctor's fixes)
      (benchmark || benchmarkError) && jsxs('div', {
        className: 'px-4 py-3 border-b border-(--ui-stroke-tertiary)',
        children: [
          jsx('div', { className: 'text-xs uppercase tracking-widest text-(--ui-text-quaternary) mb-2 abyss-mono', children: 'benchmark' }),
          benchmarkError && jsx('div', { className: 'text-xs text-(--ui-red) mb-2', children: benchmarkError }),
          benchmark && jsxs('div', {
            className: 'flex flex-col gap-1',
            children: [
              jsx('div', {
                className: 'text-xs mb-1',
                children: `probes: ${benchmark.passed} pass · ${benchmark.failed} fail · ${benchmark.pending} pending`
              }),
              (benchmark.results || []).map(p => jsxs('div', {
                key: p.id,
                className: 'flex items-center gap-2 text-xs',
                children: [
                  jsx('span', {
                    className: cn('w-16 abyss-mono shrink-0',
                      p.status === 'pass' ? 'text-(--ui-green)' : p.status === 'pending' ? 'text-(--ui-yellow)' : 'text-(--ui-red)'),
                    children: p.status
                  }),
                  jsx('span', { className: 'text-(--ui-text-secondary) shrink-0 abyss-mono', children: p.id }),
                  // min-w-0 + title: p.detail is a truncating span in a flex row
                  // (tick-18 flex-bug: truncate alone cannot shrink below content
                  // width, so long probe details overflowed instead of
                  // ellipsizing) and offered no way to read the full value
                  // (tick-13 hover-hygiene). abyss-mono keeps the diagnostic
                  // report in the phosphor stack (DESIGN.md Type).
                  jsx('span', { className: 'truncate text-(--ui-text-tertiary) min-w-0 abyss-mono', title: p.detail, children: p.detail })
                ]
              }))
            ]
          })
        ]
      }),
      // Component breakdown bars
      jsxs('div', {
        className: 'px-4 py-3 border-b border-(--ui-stroke-tertiary)',
        children: [
          jsx('div', { className: 'abyss-tiny uppercase tracking-widest text-(--ui-text-quaternary) mb-2 abyss-mono', children: 'score breakdown' }),
          compRows.map(row => {
            const barColor = row.value / row.max > 0.66 ? 'var(--ui-green)' : row.value / row.max > 0.33 ? 'var(--ui-yellow)' : 'var(--ui-red)'
            return jsxs('div', {
              key: row.label,
              className: 'flex items-center gap-2 mb-1.5',
              title: `${row.label}: ${row.value} / ${row.max}`,
              children: [
                jsx('span', { className: 'w-24 text-xs text-(--ui-text-secondary) abyss-mono', children: row.label }),
                jsx('div', { className: 'flex-1 h-2 rounded bg-(--ui-bg-tertiary) overflow-hidden', children:
                  jsx('div', {
                    className: 'h-full rounded',
                    style: { width: `${Math.min(100, (row.value / row.max) * 100)}%`, backgroundColor: barColor },
                    children: ''
                  })
                }),
                jsx('span', { className: 'w-8 text-right text-xs abyss-mono tabular-nums text-(--ui-text-tertiary)', children: row.value })
              ]
            })
          })
        ]
      }),
      // Trends sparkline bars
      trends && jsxs('div', {
        className: 'px-4 py-3 border-b border-(--ui-stroke-tertiary)',
        children: [
          jsx('div', { className: 'abyss-tiny uppercase tracking-widest text-(--ui-text-quaternary) mb-2 abyss-mono', children: '7-day activity' }),
          jsxs('div', {
            className: 'flex items-end gap-1',
            style: { height: '4rem' },
            children: (trends.timestamps || []).map((ts, i) =>
              jsx('div', {
                key: i,
                className: 'flex-1 flex flex-col justify-end gap-0.5',
                title: `${ts || ''} — ${trends.errors?.[i] || 0} errors · ${trends.activity?.[i] || 0} actions`,
                children: [
                  jsx('div', {
                    className: 'w-full rounded-sm',
                    style: { height: `${Math.min(100, ((trends.errors?.[i] || 0) / trendMax) * 100)}%`, backgroundColor: 'var(--ui-red)' },
                    children: ''
                  }),
                  jsx('div', {
                    className: 'w-full rounded-sm bg-(--ui-accent)',
                    style: { height: `${Math.min(100, ((trends.activity?.[i] || 0) / trendMax) * 100)}%` },
                    children: ''
                  })
                ]
              })
            )
          }),
          jsxs('div', {
            className: 'flex gap-4 mt-2 abyss-micro text-(--ui-text-quaternary)',
            children: [
              jsxs('span', { className: 'flex items-center gap-1', children: [
                jsx('span', { className: 'inline-block h-2 w-2 rounded-sm bg-(--ui-accent)', children: '' }),
                'activity'
              ] }),
              jsxs('span', { className: 'flex items-center gap-1', children: [
                jsx('span', { className: 'inline-block h-2 w-2 rounded-sm', style: { backgroundColor: 'var(--ui-red)' }, children: '' }),
                'errors'
              ] })
            ]
          })
        ]
      }),
      // Failure taxonomy
      failures && jsxs('div', {
        className: 'px-4 py-3',
        children: [
          jsx('div', { className: 'abyss-tiny uppercase tracking-widest text-(--ui-text-quaternary) mb-2 abyss-mono', children: 'failure taxonomy' }),
          failureLists.map(list =>
            jsxs('div', {
              key: list.title,
              className: 'mb-3',
              children: [
                jsx('div', { className: 'text-xs font-medium text-(--ui-text-secondary) mb-1 abyss-mono', children: list.title }),
                list.items.length === 0
                  ? jsx('div', { className: 'text-xs text-(--ui-text-tertiary) abyss-mono', children: 'none' })
                  : list.items.slice(0, 5).map((it, idx) =>
                      jsxs('div', {
                        key: idx,
                        className: 'flex items-center gap-2 text-xs mb-0.5',
                        children: [
                          jsx('span', { className: 'abyss-mono tabular-nums text-(--ui-text-quaternary) w-8', children: `${it.count}x` }),
                          jsx('span', {
                            // min-w-0 + title: like the benchmark p.detail row,
                            // this truncates inside a flex row — without min-w-0
                            // long error messages overflow (tick-18 flex-bug) and
                            // without a title the full value is unreachable
                            // (tick-13 hover-hygiene).
                            className: 'truncate text-(--ui-text-secondary) abyss-mono min-w-0',
                            title: it.type || it.tool || it.message,
                            children: it.type || it.tool || it.message
                          })
                        ]
                      })
                    )
              ]
            })
          )
        ]
      })
    ]
  })
}

// ---------------------------------------------------------------------------
// Wave view — August 2026 plugin-interface expansion surfaces (event bus,
// streaming telemetry, API requests, subagents, approvals).
// ---------------------------------------------------------------------------
const WAVE_SURFACES = [
  { key: 'plugin_events', label: 'events', tone: 'var(--ui-blue)' },
  { key: 'streams', label: 'streams', tone: 'var(--ui-purple)' },
  { key: 'api_requests', label: 'api', tone: 'var(--ui-orange)' },
  { key: 'subagents', label: 'subagents', tone: 'var(--ui-yellow)' },
  { key: 'approvals', label: 'approvals', tone: 'var(--ui-red)' },
  { key: 'commands', label: 'commands', tone: 'var(--ui-green)' },
  { key: 'platform_events', label: 'platform', tone: 'var(--ui-blue)' },
  { key: 'skills', label: 'skills', tone: 'var(--ui-purple)' }
]

const WAVE_TAG_TONE = {
  events: 'var(--ui-blue)',
  streams: 'var(--ui-purple)',
  api: 'var(--ui-orange)',
  subagents: 'var(--ui-yellow)',
  approvals: 'var(--ui-red)',
  commands: 'var(--ui-green)',
  platform: 'var(--ui-blue)',
  skills: 'var(--ui-purple)'
}

function WaveView({ ctx }) {
  const [summary, setSummary] = useState(null)
  const [feed, setFeed] = useState([])
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)

  const fetchAll = useCallback(() => {
    if (!ctx) return
    setLoading(true)
    // Count hard failures: a dead wave backend must print an ErrorState, not
    // the "no wave activity yet" empty state (DESIGN.md States contract:
    // errors never masquerade as empty data). Partial failures stay silent —
    // one missing surface must not blank the whole feed.
    let failed = 0
    const guard = (p) => p.catch(() => { failed += 1; return null })
    Promise.all([
      guard(ctx.rest('/wave/summary', { method: 'GET', timeoutMs: 5000 })),
      guard(ctx.rest('/wave/events?limit=12', { method: 'GET', timeoutMs: 5000 })),
      guard(ctx.rest('/wave/api?limit=12', { method: 'GET', timeoutMs: 5000 })),
      guard(ctx.rest('/wave/subagents?limit=12', { method: 'GET', timeoutMs: 5000 })),
      guard(ctx.rest('/wave/approvals?limit=12', { method: 'GET', timeoutMs: 5000 })),
      guard(ctx.rest('/wave/streams?limit=12', { method: 'GET', timeoutMs: 5000 })),
      guard(ctx.rest('/wave/commands?limit=12', { method: 'GET', timeoutMs: 5000 })),
      guard(ctx.rest('/wave/platform?limit=12', { method: 'GET', timeoutMs: 5000 })),
      guard(ctx.rest('/wave/skills?limit=12', { method: 'GET', timeoutMs: 5000 }))
    ]).then(([s, events, api, subagents, approvals, streams, commands, platform, skills]) => {
      // All EIGHT surface endpoints failing = dead wave backend. The old
      // `failed === 6` compared against the TOTAL request count including the
      // summary — a backend whose /wave/summary still answered from SQLite
      // while every surface route was down fell through to the empty state
      // (the DESIGN.md P0: errors never masquerade as empty data). 8+ failures
      // of the 9 requests can only mean the telemetry layer is unreachable.
      if (failed >= 8) {
        setSummary(null)
        setFeed([])
        setError('wave backend unreachable — every telemetry surface failed')
        return
      }
      setSummary(s && typeof s === 'object' ? s : null)
      const items = []
      ;(events || []).forEach(e => items.push({
        ts: e.timestamp, tag: 'events',
        text: `${e.namespace || 'abyss'}:${e.event}`,
        sub: e.payload ? String(e.payload).slice(0, 120) : ''
      }))
      ;(api || []).forEach(r => items.push({
        ts: r.timestamp, tag: 'api',
        text: `${r.provider || '?'} ${r.model || ''}`.trim(),
        sub: `${r.status || ''} ${r.finish_reason || ''} ${r.duration_ms != null ? fmtDur(r.duration_ms) : ''}`.trim()
      }))
      ;(subagents || []).forEach(r => items.push({
        ts: r.timestamp, tag: 'subagents',
        text: `${r.child_role || 'subagent'} ${r.child_session_id ? r.child_session_id.slice(0, 8) : ''}`.trim(),
        sub: `${r.status || ''} ${r.duration_ms != null ? fmtDur(r.duration_ms) : ''}`.trim()
      }))
      ;(approvals || []).forEach(r => items.push({
        ts: r.timestamp, tag: 'approvals',
        text: `${r.pattern_key || 'command'} ${r.choice || 'pending'}`.trim(),
        sub: r.command_preview || ''
      }))
      ;(streams || []).forEach(r => items.push({
        ts: r.timestamp, tag: 'streams',
        text: `${r.provider || '?'} ${r.model || ''} ${fmtCount(r.chars || 0)} chars`.trim(),
        sub: `${fmtCount(r.deltas || 0)} deltas ${r.error ? 'error' : ''}`.trim()
      }))
      ;(commands || []).forEach(r => items.push({
        ts: r.timestamp, tag: 'commands',
        text: `${r.surface || 'cmd'} ${r.command || ''}`.trim(),
        sub: [r.alias_used ? `alias ${r.alias_used}` : '', r.args_preview || ''].filter(Boolean).join(' ')
      }))
      ;(platform || []).forEach(r => items.push({
        ts: r.timestamp, tag: 'platform',
        text: `${r.platform || '?'} ${r.event_type || 'event'}`.trim(),
        sub: r.payload ? String(r.payload).slice(0, 120) : ''
      }))
      ;(skills || []).forEach(r => items.push({
        ts: r.timestamp, tag: 'skills',
        text: `${r.action || 'use'} ${r.name || ''}`.trim(),
        sub: [r.provenance ? `from ${r.provenance}` : '', r.details ? String(r.details).slice(0, 100) : ''].filter(Boolean).join(' ')
      }))
      items.sort((a, b) => String(b.ts || '').localeCompare(String(a.ts || '')))
      setFeed(items.slice(0, 40))
      setError(null)
    }).catch(() => setError('wave backend unavailable'))
      .finally(() => setLoading(false))
  }, [ctx])

  useEffect(() => {
    fetchAll()
    const t = setInterval(fetchAll, 15000)
    return () => clearInterval(t)
  }, [fetchAll])

  const tables = summary?.tables || {}
  // First-load honesty: while the summary fetch is in flight, the surface
  // table above would otherwise show ZERO counts and em-dash 'last' cells —
  // a transient "all surfaces empty" lie on a fresh mount (tick-23
  // counter-honesty precedent; the table is part of the same glance). Pending
  // rows render dimmed with counting-dot placeholders; real numbers replace
  // them when /wave/summary lands. Partial poll refreshes (summary already
  // present) never dim — cached truth stays visible.
  const pending = loading && !summary
  // Summary-down honesty (tick-34): `pending` only covers the FIRST load. A
  // later poll whose /wave/summary fails — while the surface endpoints still
  // answer (failed < 8, so no top-level ErrorState) — sets summary to null,
  // and the old `info ? info.count : 0` printed ZERO for every surface next
  // to a live feed: a fabricated "all surfaces empty" after the operator had
  // real numbers. When the summary is absent AFTER a settled load, that is a
  // data-loss state, not a zero — print an em-dash, dim the rows (parity with
  // the pending dots), and title the count cell so the failure is discoverable
  // instead of a silent lie.
  const summaryDown = !loading && !summary

  return jsxs('div', {
    className: 'flex h-full flex-col overflow-auto',
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between px-3 py-2 border-b border-(--ui-stroke-tertiary) shrink-0',
        children: [
          jsx('span', { className: 'text-xs uppercase tracking-widest text-(--ui-text-tertiary) abyss-mono', children: '$ abyss wave — live interface telemetry' }),
          jsx(Button, {
            variant: 'ghost', size: 'sm',
            onClick: fetchAll,
            title: 'Refresh wave telemetry',
            'aria-label': 'Refresh wave telemetry',
            children: jsx(Codicon, { name: 'refresh', className: 'text-(--ui-text-tertiary)' })
          })
        ]
      }),
      // Surface counters — terminal table (no tiles, no stat monuments)
      jsxs('div', {
        className: 'border-b border-(--ui-stroke-tertiary)',
        children: [
          jsxs('div', {
            className: 'flex items-center px-3 py-1 abyss-tiny uppercase tracking-widest text-(--ui-text-quaternary) abyss-mono',
            children: [
              jsx('span', { className: 'flex-1', children: 'surface' }),
              jsx('span', { className: 'w-16 text-right', children: 'count' }),
              jsx('span', { className: 'w-16 text-right', children: 'last' })
            ]
          }),
          WAVE_SURFACES.map(s => {
            const info = tables[s.key]
            return jsxs('div', {
              key: s.key,
              className: 'flex items-center px-3 py-1 abyss-row-hover' + ((pending || summaryDown) ? ' opacity-60' : ''),
              children: [
                jsxs('span', { className: 'flex-1 flex items-center gap-1.5 min-w-0', children: [
                  jsx('span', { className: 'inline-block h-1.5 w-1.5 rounded-full shrink-0', style: { backgroundColor: s.tone }, children: '' }),
                  jsx('span', { className: 'abyss-tiny uppercase tracking-wider truncate', style: { color: s.tone }, children: s.label })
                ]}),
                jsx('span', { className: 'w-16 text-right text-xs abyss-mono tabular-nums text-(--ui-text-primary)', title: pending ? 'loading…' : summaryDown ? 'summary link down — counts unavailable' : undefined, children: pending ? '·' : summaryDown ? '—' : fmtCount(info ? info.count : 0) }),
                jsx('span', { className: 'w-16 text-right abyss-tiny abyss-mono tabular-nums text-(--ui-text-quaternary)', title: info && info.last ? timeTitle(info.last) : undefined, children: pending ? '·' : (info && info.last ? relativeTime(info.last) : '—') })
              ]
            })
          })
        ]
      }),
      // Merged feed header — terminal-table hierarchy: the surface table
      // above has a 'surface | count | last' header, so the feed gets its own
      // micro row too. Also the LAST undisclosed list-surface cap (tick-35/36
      // counter-honesty policy — ActivityFeed/Watch/GlobalSearch all disclose
      // their fetch caps; WaveView was the holdout): the merged feed is
      // sliced to 40 items, so a feed pinned at 40 usually means MORE exist.
      // The exact per-surface totals live in the table above, so the marker
      // points there instead of printing a false total.
      jsxs('div', {
        className: 'flex items-center px-3 py-1 abyss-tiny uppercase tracking-widest text-(--ui-text-quaternary) abyss-mono border-b border-(--ui-stroke-tertiary) shrink-0',
        children: [
          jsx('span', { className: 'flex-1', children: 'feed' }),
          feed.length >= 40 && jsx('span', {
            className: 'abyss-micro whitespace-nowrap',
            title: 'showing the most recent 40 merged items — more may exist; per-surface totals are in the table above',
            children: '40+'
          })
        ]
      }),
      // Merged feed — hairline rows
      jsx('div', {
        className: 'flex-1 min-h-0 overflow-y-auto',
        children: loading && feed.length === 0 ? jsxs('div', {
          className: 'p-3 flex items-center justify-center',
          children: [
            jsx(GlyphSpinner, { ariaLabel: 'Loading wave telemetry', className: 'text-(--ui-text-tertiary)' }),
            jsx('span', { className: 'ml-2 text-sm text-(--ui-text-secondary) abyss-mono', children: 'listening for wave telemetry…' })
          ]
        }) : error && feed.length === 0 ? jsx(ErrorState, {
          title: 'Wave backend unavailable',
          description: error,
          children: jsx(Button, { variant: 'secondary', size: 'sm', onClick: fetchAll, children: 'Retry' })
        }) : feed.length === 0
          ? jsx('div', { className: 'p-3', children: jsx(EmptyState, {
              title: 'No wave activity yet',
              description: 'Events, streams, API calls, subagents and approvals will appear here as they happen.'
            }) })
          : jsx('div', {
              className: 'flex flex-col',
              children: feed.map((it, idx) => jsxs('div', {
                key: idx,
                className: cn(
                  'flex items-start gap-2 px-3 py-1.5 abyss-row-hover',
                  idx < feed.length - 1 && 'border-b border-(--ui-stroke-tertiary)'
                ),
                children: [
                  jsx('span', { className: 'mt-0.5 shrink-0 abyss-tiny uppercase tracking-wider abyss-mono', style: { color: WAVE_TAG_TONE[it.tag] || 'var(--ui-text-secondary)' }, children: it.tag }),
                  jsxs('div', { className: 'min-w-0 flex-1', children: [
                    jsx('div', { className: 'truncate text-xs text-(--ui-text-secondary) abyss-mono', title: it.text, children: it.text }),
                    it.sub && jsx('div', { className: 'truncate abyss-micro text-(--ui-text-quaternary) abyss-mono', title: it.sub, children: it.sub })
                  ]}),
                  jsx('span', { className: 'shrink-0 abyss-tiny text-(--ui-text-quaternary) abyss-mono tabular-nums', title: timeTitle(it.ts), children: relativeTime(it.ts) })
                ]
              }))
            })
      })
    ]
  })
}

function AbyssDashboard({ ctx }) {
  const [activeTab, setActiveTab] = useState('brain')
  // Drill-down: every symptom surface (activity rows, signals, incidents,
  // session search hits, brain session nodes, calendar chips) funnels into
  // the same session-trace jump.
  const [tracePreset, setTracePreset] = useState(null)
  const openTrace = useCallback((sid) => {
    if (!sid) return
    setTracePreset(sid)
    setActiveTab('tracing')
  }, [])

  // The drill preset is a ONE-SHOT: TracingView applies it once and reports
  // back through onPresetConsumed, at which point the dashboard forgets it.
  // Stability matters because TracingView lists onPresetConsumed in its
  // effect deps — an inline arrow re-created per render would churn the
  // effect; a NULL preset gates the effect body anyway, but a stable
  // callback keeps the contract tidy.
  const clearTracePreset = useCallback(() => setTracePreset(null), [])

  useEffect(() => {
    ensureConsoleCss()
    if (ctx) {
      ctx.rest('/activity', {
        method: 'POST',
        body: {
          action: 'plugin_loaded',
          description: 'Abyss dashboard plugin loaded',
          category: 'system',
          status: 'completed'
        },
        timeoutMs: 3000
      }).catch(e => console.error('Failed to log activity:', e))
    }
  }, [ctx])

  // Operational instruments first: the Brain graph opens by default (the
  // centerpiece), then triage (watch) and health — the symptom surfaces —
  // then the browsing/telemetry views.
  const tabs = [
    { value: 'brain', label: 'brain' },
    { value: 'signals', label: 'watch' },
    { value: 'health', label: 'health' },
    { value: 'activity', label: 'activity' },
    { value: 'tracing', label: 'trace' },
    { value: 'wave', label: 'wave' },
    { value: 'search', label: 'search' },
    { value: 'calendar', label: 'calendar' }
  ]

  return jsxs('div', {
    className: 'flex h-full flex-col bg-background text-foreground',
    children: [
      jsx(Masthead, {}),
      jsx(StatusStrip, { ctx, onNavigate: setActiveTab }),
      jsxs(Tabs, {
        value: activeTab,
        onValueChange: setActiveTab,
        className: 'flex-1 min-h-0',
        children: [
          jsx(TabsList, {
            className: 'flex w-full items-center justify-start overflow-x-auto shrink-0 bg-(--ui-bg-quaternary) border-b border-(--ui-stroke-tertiary)',
            children: tabs.map(tab =>
              jsx(TabsTrigger, {
                key: tab.value,
                value: tab.value,
                className: 'text-xs h-8 abyss-mono shrink-0',
                children: tab.label
              })
            )
          }),
          jsx('div', {
            className: 'flex-1 min-h-0 overflow-hidden',
            children: activeTab === 'brain' ? jsx(BrainGraph, { ctx, onOpenTrace: openTrace })
              : activeTab === 'signals' ? jsx(SignalsIncidentsView, { ctx, onOpenTrace: openTrace })
              : activeTab === 'health' ? jsx(HealthView, { ctx })
              : activeTab === 'activity' ? jsx(ActivityFeed, { ctx, onOpenTrace: openTrace })
              : activeTab === 'tracing' ? jsx(TracingView, { ctx, presetSessionId: tracePreset, onPresetConsumed: clearTracePreset })
              : activeTab === 'wave' ? jsx(WaveView, { ctx })
              : activeTab === 'search' ? jsx(GlobalSearch, { ctx, onOpenTrace: openTrace })
              : jsx(CalendarView, { ctx, onOpenTrace: openTrace })
          })
        ]
      })
    ]
  })
}

function AbyssStatusChip({ ctx }) {
  const [status, setStatus] = useState(null)
  // Distinguish "first fetch still in flight" from a truly unreachable
  // backend: the pane's StatusStrip prints 'status link down' (DESIGN.md
  // States — errors never masquerade as all-clear), so the chip must not go
  // silently blank when /status fails. `fetched` gates the down marker so
  // the very first render (before any fetch resolves) doesn't flash a false
  // alarm.
  const [fetched, setFetched] = useState(false)

  const refresh = useCallback(() => {
    if (!ctx) return
    ctx.rest('/status', { method: 'GET', timeoutMs: 5000 })
      .then(d => { setStatus(d && typeof d === 'object' ? d : null); setFetched(true) })
      .catch(() => { setStatus(null); setFetched(true) })
  }, [ctx])

  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 30000)
    return () => clearInterval(t)
  }, [refresh])

  const open = status?.signals_open ?? 0
  const score = status?.score
  const level = status?.level || ''
  // Silence overrides even a healthy-looking score: if nothing has been
  // recorded for an hour+, the chip must not read as "fine" (tick-42,
  // parity with the pane's verdict idle disclosure).
  const idle = idleLabel(status?.last_activity_at)
  const idleCritical = idle && idle.tone === 'text-(--ui-red)'
  // In-flight remediation disclosure (tick-45, StatusStrip tick-44 parity):
  // the chip is the ONLY abyss surface visible while the dashboard is
  // closed, so a running cloud-agent fix must be disclosed here too — a
  // healthy-looking score with a resolver actively working is not "fine".
  const resolvingCount = status?.resolutions_running ?? 0
  const tone = idleCritical ? 'text-(--ui-red)'
    : level === 'critical' ? 'text-(--ui-red)'
    : level === 'degraded' ? 'text-(--ui-yellow)'
    : level === 'fair' ? 'text-(--ui-yellow)'
    : 'text-(--ui-green)'

  // Toggle: clicking the chip while the dashboard is open closes it
  // (navigates back to chat) instead of doing nothing.
  const isOpen = () => {
    try { return (window.location.hash || '').startsWith('#/abyss') } catch { return false }
  }

  return jsxs(Button, {
    variant: 'ghost',
    size: 'sm',
    className: 'h-6 px-2 text-xs gap-1.5',
    onClick: () => host.navigate(isOpen() ? '/' : '/abyss'),
    title: isOpen() ? 'Close Abyss dashboard' : 'Open Abyss dashboard',
    children: [
      jsx(Codicon, { name: 'eye', className: 'text-(--ui-text-tertiary)' }),
      jsx('span', { className: 'text-(--ui-text-secondary) abyss-mono', children: 'abyss' }),
      status && jsxs('span', {
        className: cn('flex items-center gap-1 abyss-mono tabular-nums', tone),
        title: (idle ? `last activity ${timeTitle(status.last_activity_at)} · idle ${idle.text}` : (`health ${score ?? '—'}` + (level ? ` · ${level}` : '')))
          + (resolvingCount > 0 ? ` · ${resolvingCount} cloud-agent fix${resolvingCount === 1 ? '' : 'es'} in flight` : ''),
        children: [
          jsx('span', {
            className: 'inline-block h-1.5 w-1.5 rounded-full',
            style: {
              backgroundColor: idleCritical ? 'var(--ui-red)'
                : level === 'critical' ? 'var(--ui-red)'
                : level === 'degraded' || level === 'fair' ? 'var(--ui-yellow)'
                : 'var(--ui-green)'
            },
            children: ''
          }),
          score !== null && score !== undefined ? `${score}` : fmtCount(open),
          // Resolving marker: inline var(--ui-blue) — no compiled
          // text-(--ui-blue) class exists in the host bundle (only
          // red/yellow/green/accent do); matches the Calendar "running"
          // glyph convention (DESIGN.md inline-style rule).
          resolvingCount > 0 && jsx('span', {
            className: 'inline-block h-1.5 w-1.5 rounded-full',
            style: { backgroundColor: 'var(--ui-blue)' },
            children: ''
          }),
          // Screen-reader parity with the pane's StatusStrip echoes (ticks
          // 27/39/44): the chip's idle tone, red/blue companion dots and
          // hover title are all visual-only — a screen-reader operator
          // tabbing through the statusbar hears just 'abyss 87' and cannot
          // tell critical silence from a healthy score, nor that a
          // cloud-agent fix is in flight. This sr-only child joins the
          // button's accessible name so focusing the chip speaks the full
          // disclosure (idle phrase + level + in-flight count). sr-only is
          // verified compiled in the host bundle (StatusStrip precedent).
          jsx('span', {
            className: 'sr-only',
            children: 'abyss health ' + (score ?? 'unknown')
              + (level ? `, ${level}` : '')
              + (idle ? `, idle ${idle.text}` : '')
              + (resolvingCount > 0 ? `, ${resolvingCount} cloud-agent fix${resolvingCount === 1 ? '' : 'es'} in flight` : '')
          })
        ]
      }),
      // Backend unreachable — never a silent blank or a false all-clear
      // (DESIGN.md States parity with the pane's 'status link down' strip).
      !status && fetched && jsxs('span', {
        className: 'flex items-center gap-1 abyss-mono tabular-nums text-(--ui-red)',
        title: 'Abyss backend unreachable — status link down',
        children: [
          jsx('span', {
            className: 'inline-block h-1.5 w-1.5 rounded-full',
            style: { backgroundColor: 'var(--ui-red)' },
            children: ''
          }),
          'down'
        ]
      })
    ]
  })
}

export default {
  id: ID,
  name: 'Abyss',
  description: 'Raindrop-style observability: activity, tracing, brain graph, signals & incidents, wave telemetry',
  defaultEnabled: true,
  register(ctx) {
    ctx.i18n.register({
      en: {
        paneTitle: 'Abyss',
        dashboard: 'Abyss Dashboard',
        activityFeed: 'Activity Feed',
        calendar: 'Calendar',
        globalSearch: 'Search',
        tracing: 'Tracing',
        brain: 'Hermes Brain',
        signals: 'Watch',
        noResults: 'No results found'
      }
    })

    // Dashboard pane (right sidebar by default, user can drag it)
    ctx.register({
      id: 'abyss-dashboard-pane',
      area: 'panes',
      title: 'abyss',
      data: { placement: 'right', width: '420px' },
      render: () => jsx(AbyssDashboard, { ctx })
    })

    // Full-page route
    ctx.register({
      id: 'abyss-dashboard-page',
      area: 'routes',
      data: { path: '/abyss' },
      render: () => jsx(AbyssDashboard, { ctx })
    })

    // Sidebar navigation
    ctx.register({
      id: 'abyss-nav-item',
      area: 'sidebarNav',
      data: {
        path: '/abyss',
        label: 'Abyss',
        codicon: 'eye'
      }
    })

    // Command palette entry — toggles: if Abyss is already open, closing it
    // gives the operator a way out that doesn't depend on the masthead ✕.
    ctx.register({
      id: 'abyss-palette-command',
      area: 'palette',
      data: { label: 'Open Abyss Dashboard', icon: 'eye' },
      onSelect: () => {
        try {
          const open = (window.location.hash || '').startsWith('#/abyss')
          host.navigate(open ? '/' : '/abyss')
        } catch { /* navigation guard */ }
      }
    })

    // Status bar chip (shows open signal count)
    ctx.register({
      id: 'abyss-status-chip',
      area: 'statusBar.right',
      render: () => jsx(AbyssStatusChip, { ctx })
    })
  }
}

// reload-tick

// size-fix-tick

// final-clean-tick

// centroid-fix-tick

// night-shift-tick: reduced-motion honor for animate-pulse skeletons (abyss-mute-pulse);
//   error-no-longer-masquerades-as-empty in TracingView / ActivityFeed /
//   SignalsIncidentsView (Preserve on background-poll failure, surface ErrorState
//   only when cached data is also gone); StatusStrip skeleton only on first load
//   (silent background refresh); Wave empty state now uses SDK EmptyState
//   instead of a plain dim text blob (DESIGN.md "Empty: SDK EmptyState").
//   No backend/Python touched.
//
// night-shift-tick-2: fixed StatusStrip nav-to-watch bug — SIG metric and the
//   trailing verdict dot called onNavigate('watch') but the actual tab value is
//   'signals' (the tab is LABELED 'watch' but its value is 'signals'); the
//   navigation fell through to the Conditional render default (CalendarView).
//   Changed to onNavigate('signals') + nav:'signals'. Also: HealthView and
//   Watch loading skeletons upgraded from a single hollow h-12 bar to multi-
//   block skeletons that mirror the real content stack (DESIGN.md "Loading:
//   pulsing skeleton blocks", plural). Trend sparkline bars now carry a hover
//   title (date + error/action counts), key simplified to index, and bar
//   heights capped at 100% (Math.min guard). Score breakdown rows gained a
//   hover title (label: value / max). No backend/Python touched.
//
// night-shift-tick-3: PhosphorGraphRenderer label render wrapped in try/catch
//   so a roundRect() failure (older canvas impls) no longer blanks the graph;
//   ActivityFeed filter buttons gained aria-label for screen readers; Calendar
//   day cells gained a hover title (full date + today/week-boundary context).
//   No backend/Python touched.
//
// night-shift-tick-4 (this shift): WaveView error state folded into the feed
//   container so loading/empty/error are co-located (was rendering as a
//   separate block between surface counters and feed, creating a visual gap);
//   CalendarView loading skeleton upgraded from single h-12 bar to 7-cell
//   week-grid skeleton mirroring the real layout; Calendar day cells gained
//   abyss-row-hover for visual consistency with other interactive rows;
//   Brain graph container gained abyss-row-hover. No backend/Python touched.
//
// night-shift-tick-5 (this shift): host CSS self-update drift sweep — re-verified every
//   Tailwind class token in this file against the CURRENT live bundle
//   (apps/desktop/dist/assets/index-BwB1iTEf.css; installed app + repo dist match).
//   The app's stylesheet was regenerated since tick-4 and dropped several utilities
//   this file relied on, all fixed with inline styles (DESIGN.md constraint: inline
//   styles for values with no compiled class):
//     - TraceTimelineView agents-overview lanes: bg-(--ui-red)/40, bg-(--ui-green)/40
//       and bg-(--ui-red) were ALL dead → healthy/failed lane bars and error ticks
//       rendered colorless; now inline backgroundColor (+opacity 0.4 for the fill).
//     - TraceTimelineView detail failure segments: bg-(--ui-red) dead → failures did
//       not glow red; now inline backgroundColor.
//     - TraceGraphView stats badge: text-(--ui-orange) dead → "N open" badge lost its
//       tone; now inline color.
//     - HealthView trend skeletons + 7-day activity chart: h-16 dead → containers had
//       zero height (bars invisible); now style height 4rem.
//     - Doctor agent-log <pre> x2: max-h-44 dead → unbounded growth; now maxHeight
//       11rem.
//     - Watch skeleton row: w-2/3 dead; now inline width.
//   Also: GlobalSearch stale-response race fixed — a slow response for an older query
//   can no longer overwrite results/count-line for the newer one (fetch sequence ref).
//   Verified post-edit: node --check OK, check-hook-order clean, 0 missing utility
//   classes vs live bundle, import surface = @hermes/plugin-sdk + react +
//   react/jsx-runtime only. Regression guard intact: list/graph/timeline modes,
//   TraceGraphView, TraceTimelineView, /trace routes untouched structurally.
//   No backend/Python touched.
//
// night-shift-tick-6 (this shift): out-of-order response guard extended to ALL
//   identity-keyed fetches. Tick-5 fixed this race class in GlobalSearch only;
//   audit found five more instances of the same bug — a slow response for an
//   OLD identity (filter / week / session) resolving after a newer request and
//   overwriting state for the NEW one:
//     - ActivityFeed.fetchActivities: 30s poll + filter switch overlap → rows
//       for the old category could replace the new filter's rows.
//     - CalendarView.fetchTasks: rapid week navigation → old week's tasks
//       painted into the new week's grid (wrong days).
//     - TracingView.fetchTraces: fast session switching (Select or drill-in) →
//       old session's events shown under the new session's header.
//     - TraceGraphView.fetchGraph: same, for the canvas trajectory DAG; also
//       fixed a stale-closure read of `selected` (now functional setSelected).
//     - TraceTimelineView.fetchTimeline: same, for per-agent trajectory lanes.
//   Each now carries a fetch-sequence ref; stale responses are dropped before
//   any setState, and loading/error only clear for the latest request.
//   Verified post-edit: node --check OK, check-hook-order clean, import surface
//   unchanged, no cross-module import strings. Regression guard intact: list/graph/timeline
//   modes, TraceGraphView, TraceTimelineView, view toggle, /trace routes all
//   preserved (guards added inside existing fetch callbacks only). No
//   backend/Python touched.
//
// night-shift-tick-7 (this shift): five fixes found by re-auditing against
//   DESIGN.md contracts:
//     1. CalendarView dropped every task later than Saturday 00:00 — the
//        `taskDay <= weekEnd` bound excluded the whole last day column.
//        Now an exclusive bound against the start of the following day.
//     2. GlobalSearch stuck-spinner: aborting a query (shortening below 2
//        chars) bumped the fetch sequence, so the stale response's finally
//        skipped setLoading(false). The early-return path now clears loading.
//     3. WaveView error contract: all six endpoint catches swallowed failures,
//        so a dead backend rendered the empty state. Now a failure counter —
//        ALL surfaces failing prints ErrorState; partial failure stays silent.
//     4. Watch rows: busy key was exact-matched and never matched ':resolve-
//        agent', so the cloud-resolve button stayed enabled mid-dispatch
//        (double-click double-dispatched). Both signal and incident rows now
//        prefix-match their busy keys.
//     5. A11y parity: trace view-mode toggle aria-label moved onto the Button;
//        TraceGraphView canvas is now focusable with role/aria-label and
//        Escape clears selection (matching the Brain graph keyboard contract).
//   Verified post-edit: node --check OK, import surface unchanged (@hermes/
//   plugin-sdk + react + react/jsx-runtime only), no cross-module import
//   strings. Regression
//   guard intact: list/graph/timeline modes, TraceGraphView, TraceTimelineView,
//   view toggle, /trace routes all preserved. No backend/Python touched.
//
// night-shift-tick-8 (this shift): five small fixes from a full DESIGN.md
//   contract re-audit of all eight views:
//     1. Masthead boot line ('$ ./abyss --observe --local --cloud-fix') now
//        min-w-0 truncate — at 420px pane width the string sat right at the
//        overflow boundary and the masthead's overflow-hidden clipped it
//        mid-glyph; it now ellipsizes gracefully instead.
//     2. GlobalSearch count line hidden while a fetch is in flight — the old
//        render showed the NEW query text with the OLD result count for one
//        debounce frame (300ms lie about what was just typed).
//     3. TraceTimelineView detail header no longer prints a dangling
//        'trajectory · ' with an empty id when no session is picked — it
//        prints 'trajectory · pick an agent' instead.
//     4. TraceGraphView depthOf gained a cycle guard (onStack set): a malformed
//        cyclic spawn edge in /trace/graph payload would recurse forever inside
//        the draw effect and crash the pane; on-stack nodes now contribute
//        depth 0. View structure, layout, and hit-testing unchanged.
//     5. Changelog hygiene: deduplicated a doubled phrase in the tick-4 note.
//   Verified post-edit: node --check OK, import surface unchanged (@hermes/
//   plugin-sdk + react + react/jsx-runtime only), no cross-module import
//   strings in code or comments. Regression guard intact: list/graph/timeline
//   modes, TraceGraphView, TraceTimelineView, view-mode toggle, /trace routes
//   all preserved. No backend/Python touched.
//
// night-shift-tick-9 (this shift): dead codicon name sweep — the host's
//   Codicon component maps a name to a CSS class with no fallback, so an
//   unknown name renders an invisible empty box (aria-hidden, no glyph).
//   Re-verified every icon name in this file against the live bundle
//   (apps/desktop/dist/assets/index-BwB1iTEf.css) and replaced the six names
//   that do NOT exist in the codicon set:
//     - EVENT_ICONS: checkmark-circle → pass, sparkles → sparkle, bulb → wand
//       (three of the eight trace event glyphs were invisible — a session
//       trace on the list view lost its tool-completion/reasoning icons).
//     - TracingView header: history-timestamp → history (trace tab header
//       icon was invisible).
//     - Trace view-mode toggle: sitemap → graph (graph-mode toggle icon was
//       invisible; list/history were already valid).
//     - BrainGraph header: network-flow-diagram → circuit-board (brain tab
//       header icon was invisible).
//   TraceTimelineView also gained two message-honesty empty states: a picked
//   session whose trajectory fetch returned nothing now prints 'no trajectory
//   data for <shortID>' instead of the misleading 'pick a session…', and an
//   all-empty trajectory prints 'no events in this trajectory' instead of a
//   blank panel under the header (DESIGN.md States: empty states never
//   masquerade as 'pick an agent'). All replacements verified present in the
//   live bundle first. node --check OK, check-hook-order clean, import
//   surface unchanged (@hermes/plugin-sdk + react + react/jsx-runtime only),
//   no cross-module import strings. Regression guard intact: list/graph/
//   timeline modes, TraceGraphView, TraceTimelineView, view-mode toggle,
//   /trace routes all preserved (icon names + message strings only).
//   No backend/Python touched.
//
// night-shift-tick-10 (this shift): live-bundle class re-audit + a11y +
//   dead-code sweep:
//     1. Watch skeleton row: w-1/2 was DEAD in the current bundle
//        (index-BwB1iTEf.css) — the second skeleton line rendered full-width
//        instead of the half-width it meant (tick-5 fixed w-2/3 in the same
//        row but missed w-1/2). Now inline width 50%, matching its sibling.
//     2. TraceTimelineView overview lanes are click-to-drill but were not
//        keyboard-operable — no focus, no activation. Added role="button",
//        tabIndex 0, Enter/Space activation (parity with the Brain/Trace
//        canvas keyboard contracts, DESIGN.md a11y), and a stateful
//        aria-label (events/failed/selected).
//     3. Removed unused formatTime() helper (dead code, zero call sites).
//   Verified post-edit: node --check OK, check-hook-order clean, class
//   inventory re-scanned against the live bundle (only abyss-* custom classes
//   remain uninventoried — they are injected by the plugin's own CSS),
//   import surface unchanged (@hermes/plugin-sdk + react + react/jsx-runtime
//   only, no cross-module import strings). Regression guard intact:
//   list/graph/timeline modes, TraceGraphView, TraceTimelineView, view-mode
//   toggle, /trace routes all preserved (lane patch adds props to existing
//   rows only). No backend/Python touched.
//
// night-shift-tick-11 (this shift): TraceTimelineView fixes from a full
//   DESIGN.md States-contract audit:
//     1. P0 error-masquerades-as-empty: a failed /trace/agents fetch wiped
//        overview to [] in the catch, and the render line `error && !overview`
//        never fired for an empty array — a dead backend printed the
//        EmptyState "No agents" (indistinguishable from a fresh system).
//        The catch no longer clears cached rows (preserve-on-blip, matching
//        ActivityFeed/SignalsIncidentsView) and the ErrorState now fires when
//        `error && (!overview || !overview.length)` — i.e. any failure with
//        nothing cached.
//     2. NaN lane widths: `Math.max(...overview.map(a => a.duration_ms), 1)`
//        → NaN when any agent lacks duration_ms (undefined poisons the
//        spread) → every iframe bar width became 'NaN%' (invalid CSS → bars
//        fell back to full-width). Both maxDur and the per-lane pct now
//        coalesce `|| 0`.
//     3. A11y label/tooltip coalesce: lane aria-label and row title no
//        longer print "undefined events/failed/reasoning" when the backend
//        omits a count.
//     4. Trajectory detail cap: the bottom lane list can render up to 300
//        events unbounded and swallow the pane, squeezing out the
//        agents-overview (the primary surface); now maxHeight 12rem with
//        internal scroll (inline style, per DESIGN.md constraint).
//     5. relativeTime(): removed a dead ternary whose branches were
//        identical (`future ? 'just now' : 'just now'`).
//   Verified post-edit: node --check OK, import surface unchanged
//   (@hermes/plugin-sdk + react + react/jsx-runtime only), no cross-module
//   import strings. Regression guard intact: list/graph/timeline modes,
//   TraceGraphView, TraceTimelineView, view-mode toggle, /trace routes all
//   preserved (state/render guards + inline styles only). No backend/Python
//   touched.
//
// night-shift-tick-12 (this shift): HiDPI / spec-consistency sweep:
//     1. TraceGraphView canvas used a hardcoded "JetBrains Mono" font stack
//        for its node labels, sub-labels and empty hint — DESIGN.md Type
//        mandates the host mono stack for everything, and PhosphorGraphRenderer
//        (brain graph) already used the correct `ui-monospace, SFMono-Regular,
//        Menlo, Consolas, monospace`. On hosts where JetBrains Mono isn't
//        installed the trace-graph text fell back to a different face than the
//        rest of the instrument. All three font declarations now use the host
//        mono stack (canvas cannot resolve var(), so the literal stack matches
//        the established convention).
//     2. Brain graph backing store was sized in CSS px only — on HiDPI
//        displays (Windows scaling ≥100%) the compositor upscaled the canvas,
//        so the phosphor dots, labels and dither ground rendered soft/blurry
//        while TraceGraphView was already DPR-correct. PhosphorGraphRenderer
//        now reads devicePixelRatio: the backing store is sized in device
//        pixels (mount + rAF-throttled resize in BrainGraph), _calculateLayout
//        and arrow-key fallback math stay in logical CSS px (dividing by dpr),
//        and _render() folds dpr into the transform (setTransform(dpr*scale,
//        dpr*offset)) so all coordinates, radii, glyphs and clears remain
//        logical. Stroke widths/dashes divide by scale as before — dpr scaling
//        composes transparently. Renderer API, layout determinism, keyboard
//        contract and hit-testing unchanged.
//   Verified post-edit: node --check OK, hook-order scan clean (the one
//   flagged useMemo/return pair in HealthView is a scanner false-positive —
//   trendMax useMemo at the top of the component precedes the loading/error
//   early returns, as the in-file comment documents), class-token sweep
//   against live bundle index-BwB1iTEf.css: 0 dead utilities (the two
//   reported misses are string literals, not class tokens), codicon sweep:
//   0 missing (all 19 names present). Import surface unchanged
//   (@hermes/plugin-sdk + react + react/jsx-runtime only), no cross-module
//   import strings. Regression guard intact: list/graph/timeline modes,
//   TraceGraphView, TraceTimelineView, view-mode toggle, /trace routes all
//   preserved (font strings + dpr math only, inside existing draw/size paths).
//   No backend/Python touched.
//
// night-shift-tick-13 (this shift): drill-down hijack fix + hover hygiene.
//     1. TracingView preset consumed ONCE: the drill-down effect depended on
//        both presetSessionId and selectedSession and never consumed the
//        preset, so after any drill-in (activity row, signal, brain session
//        node, search hit) picking a DIFFERENT session in the trace Select
//        re-fired the effect and snapped the Selection back to the drilled
//        session — manual choice was impossible until the view remounted.
//        A presetHandledRef now records the applied preset; the effect only
//        runs when the preset value actually changes, leaving the operator's
//        later dropdown picks alone. Same hook count + one useRef, all hooks
//        still before any early return (React 310-safe).
//     2. Truncation hover hygiene: rows whose body text truncates now carry
//        a title attribute so the full value is readable on hover — Activity
//        Feed action + description, Global Search result title + description,
//        Wave feed text (6 sites).
//     3. Calendar cross-month header: a week spanning December→January
//        printed only the start year ('Dec 29 – Jan 4, 2026'); the label now
//        appends the end year when it differs ('–2027').
//   Verified post-edit: node --check OK, import surface unchanged
//   (@hermes/plugin-sdk + react + react/jsx-runtime only), no cross-module
//   import strings in code or comments. Regression guard intact: list/graph/
//   timeline modes, TraceGraphView, TraceTimelineView, view-mode toggle,
//   /trace routes all preserved (state guard + title/template strings only).
//   No backend/Python touched.
//
// night-shift-tick-14 (this shift): live-pane polling + trajectory error
//   honesty + cursor parity:
//   1. Watch pane (SignalsIncidentsView) now re-polls the active tab every
//      30s — signals arriving from a running cron job surface without
//      remounting the view (cadence parity with StatusStrip/ActivityFeed/
//      WaveView; the loading guard only shows the skeleton when the cached
//      list is also gone, so background polls refresh in place, no blink).
//   2. TraceTimelineView agents-overview now polls every 30s too, and its
//      loading guard became `loadingO && (!overview || !overview.length)` so
//      the cached agent lanes survive background polls instead of punching
//      out to a spinner every half minute.
//   3. Trajectory detail (TraceTimelineView): a failed /trace/timeline fetch
//      previously printed "no trajectory data" — the exact DESIGN.md P0
//      (errors never masquerade as empty data). New tlError state renders
//      'timeline link down' + a retry button; success clears it. Switching
//      sessions also clears the previous session's lanes immediately so the
//      old trajectory is never painted under the new session's header while
//      its fetch is in flight.
//   4. TraceGraphView canvas gained cursor-crosshair — click-select
//      affordance parity with the Brain graph canvas (class verified live in
//      index-BwB1iTEf.css).
//   Two audit non-findings recorded for confidence: (a) TraceGraphView's
//   column formula `maxD - depth[n.id]` uses SUBTREE HEIGHT, not BFS level,
//   so the session root lands in column 0 and leaves flow right — the
//   "left-to-right DAG" contract holds; (b) /status shape
//   (score/level/signals_open/signals_critical/signals_error/incidents_open)
//   verified against abyss_analytics.get_status() — StatusStrip/chip contracts
//   intact.
//   Verified post-edit: node --check OK, check-hook-order clean, import
//   surface unchanged (@hermes/plugin-sdk + react + react/jsx-runtime only),
//   no cross-module import strings. Regression guard intact: list/graph/
//   timeline modes, TraceGraphView, TraceTimelineView, view-mode toggle,
//   /trace routes all preserved (polling effects + state/guard additions +
//   one className only). No backend/Python touched.
//
// night-shift-tick-15 (this shift): drill-path integrity + skeleton/key/a11y
// hygiene sweep:
//   1. TracingView drill-path robustness: a drill into an old/deep session
//      (activity row, signal, brain node, search hit) can point at a session
//      that fell off the /trace?limit=50 recency list. The Select previously
//      held a value with no matching SelectItem (Radix dev warning + the
//      trigger showed the "select session…" placeholder while trace data was
//      already loaded), and when the recency list was empty the whole view
//      collapsed to the "No sessions" EmptyState — burying the preset
//      session's trace. Fixes: sessionOptions useMemo appends a synthetic
//      "(drill)" SelectItem for the selected session when missing; the
//      EmptyState and ErrorState guards now fire only when there is genuinely
//      nothing selected (preset keeps the view alive; the per-mode trace
//      fetch has its own error surface). Hook added above all early returns
//      (React 310-safe).
//   2. Watch skeleton rows (SignalsIncidentsView) missing React key — every
//      other map in the file carries keys; React dev warned "Each child in a
//      list should have a unique key prop" for the 4 skeleton rows.
//   3. Doctor flow GlyphSpinners (running/applying) lacked ariaLabel — every
//      other spinner in the file announces its state; two now carry
//      'Doctor agent running' / 'Applying approved fixes'.
//   4. Trace list rows (TracingView list mode) truncate tool/model/result
//      preview without a title — hover-hygiene parity with tick-13's
//      activity/search/wave titles; full values now readable on hover.
//   5. ActivityFeed empty state is now filter-aware — "No activity yet" was
//      a lie when a category filter was active but had zero rows; prints
//      "No <filter> activity" + honest description instead.
//   Verified post-edit: node --check OK, check-hook-order clean, import
//   surface unchanged (@hermes/plugin-sdk + react + react/jsx-runtime only),
//   no cross-module import strings in code or comments, class-token sweep vs
//   live bundle index-BwB1iTEf.css: 0 dead, codicon sweep: 0 missing.
//   Regression guard intact: list/graph/timeline modes, TraceGraphView,
//   TraceTimelineView, view-mode toggle, /trace routes all preserved
//   (guards/props/copy only inside existing components). No backend/Python
//   touched.
//
// night-shift-tick-16 (this shift): keyboard/drag contract completion:
//   1. TraceGraphView canvas arrow-key selection parity with the Brain graph —
//      the canvas was focusable with Escape-to-clear only; now Arrow keys move
//      the selection between DAG nodes using the same direction-half-plane
//      scoring model as PhosphorGraphRenderer._selectByDirection (first press
//      starts from the viewport center), and the aria-label announces the
//      key map. Click hit-testing, layout, and the draw effect untouched.
//   2. PhosphorGraphRenderer (brain graph) drag end outside the canvas — a
//      mouseup off-canvas never cleared draggedNode, so the node stayed glued
//      to the cursor and later passive mousemoves teleported it. A window-level
//      mouseup listener drops the drag; it self-removes once its canvas is
//      detached (renderer remount), so replaced renderers don't accumulate.
//   Live-bundle re-audit first: host CSS still index-BwB1iTEf.css (repo dist +
//   installed win-unpacked match) — all --ui-* arbitrary utilities used here
//   re-verified compiled (134 arbitrary-value selectors scanned); codicon set
//   unchanged since tick-9. Verified post-edit: node --check OK,
//   check-hook-order clean, import surface unchanged (@hermes/plugin-sdk +
//   react + react/jsx-runtime only), no cross-module import strings.
//   Regression guard intact: list/graph/timeline modes, TraceGraphView,
//   TraceTimelineView, view-mode toggle, /trace routes all preserved
//   (keyboard handler + one window listener only). No backend/Python touched.
//
// night-shift-tick-17 (this shift): HealthView partial-failure tolerance
//   (WaveView precedent). fetchAll used a bare Promise.all over /health,
//   /trends and /failures — one flaky endpoint rejected the unit, so a
//   /trends or /failures blip threw away a perfectly good /health payload
//   and printed the misleading "The Abyss backend did not return a health
//   score" ErrorState, burying the doctor/benchmark triage controls behind
//   a single failing surface. Each fetch is now individually guarded (catch
//   → null, WaveView's "partial failures stay silent" policy): a failed
//   /trends or /failures just omits its section (both are conditional
//   renders), while the !health ErrorState now fires only when /health
//   itself failed — making its message accurate. Doctor flow, benchmark,
//   score breakdown, trend bars and failure taxonomy untouched. Also
//   re-ran the full baseline audit: node --check OK; hook-order scan clean;
//   156 class tokens verified against the live bundle (index-BwB1iTEf.css,
//   unchanged since tick-16 — repo dist + installed win-unpacked match;
//   0 dead utilities); all 18 codicon names present in the bundle; import
//   surface unchanged (@hermes/plugin-sdk + react + react/jsx-runtime only),
//   no cross-module import strings in code or comments. Regression guard
//   intact: list/graph/timeline modes, TraceGraphView, TraceTimelineView,
//   view-mode toggle, /trace routes all preserved (fetch guards inside
//   HealthView only). No backend/Python touched.
//
// night-shift-tick-18 (this shift): flex truncation + triage error honesty.
//   1. Truncation flex-bug: `truncate` on a flex item WITHOUT min-w-0 cannot
//      shrink below its content width (flex min-width:auto), so long
//      Activity Feed action titles and Calendar task chips overflowed their
//      row/cell instead of ellipsizing — the relative-time label was pushed
//      out of the row. Added min-w-0 to the truncating spans and shrink-0
//      to the timestamps (the timestamp must never squish).
//   2. P0 error-masquerades-as-silent-no-op (watch/triage surface): a failed
//      acknowledge/resolve/reopen/close/resolve-agent printed only to the
//      console — the button simply re-enabled with zero feedback. New
//      actionError state carries the failing row + action; the button row
//      now prints a red `✗ <action> failed — <message>` line beneath its
//      buttons (flex-wrap + w-full so the error wraps to its own line),
//      cleared at the start of the next action (retry hygiene). DESIGN.md
//      States: errors surface as recovery paths, never as silent no-ops.
//   Verified post-edit: node --check OK; check-hook-order clean; class-token
//   sweep vs live bundle index-BwB1iTEf.css (unchanged since tick-17): all
//   new tokens LIVE (min-w-0, shrink-0, flex-wrap, w-full, mt-0.5), 155
//   className tokens, only the 8 plugin-injected abyss-* classes omitted
//   (expected — CONSOLE_CSS defines them); import surface unchanged
//   (@hermes/plugin-sdk + react + react/jsx-runtime only), no cross-module
//   import strings. Regression guard intact: list/graph/timeline modes,
//   TraceGraphView, TraceTimelineView, view-mode toggle, /trace routes all
//   preserved (className additions + one state + two catch-set lines only).
//   No backend/Python touched.
//
// night-shift-tick-19 (this shift): flex-parent restoration in the Trace view
//   modes. TraceGraphView and TraceTimelineView roots used
//   'flex-1 min-h-0 flex-col' WITHOUT the `flex` display class — and the live
//   bundle proves `.flex-col{flex-direction:column}` alone does NOT set
//   display:flex, so the root rendered as a block element and every inner
//   `flex-1`/`min-h-0` child was inert:
//     1. TraceGraphView: the canvas wrap ('relative flex-1 min-h-0
//        overflow-hidden') never stretched, so the canvas locked at its
//        Math.max(wrap.clientHeight, 340) minimum — a thin band of nodes with
//        dead space under it in a tall pane, and a cramped draw in a short
//        one. Legacy fallback to `rounded` caps made it look "fine" but the
//        pane did not fill.
//     2. TraceTimelineView: the agents-overview scroller
//        ('flex-1 min-h-0 overflow-y-auto px-3 py-2') never became the middle
//        flex lane — it grew with content instead, so with many lanes the
//        bottom trajectory panel (12rem, own internal scroll) was pushed out
//        of view, and the overview column did not scroll.
//   Both roots now read 'flex flex-1 min-h-0 flex-col' — the canvas wrap and
//   the overview lane stretch to fill the pane, the trajectory panel pins to
//   the bottom. Class-only change: layout intent restored, component names,
//   view-mode toggle, /trace routes, and both canvas renderers untouched.
//   Baseline re-verified: node --check OK; check-hook-order clean; class
//   token sweep vs live bundle index-BwB1iTEf.css: 0 dead (the lone scanner
//   report 'last:mb-0' is a false positive — grep proves
//   'last\:mb-0:last-child{margin-bottom:0}' IS compiled; the checker regex
//   misses the CSS backslash escape); codicon sweep: 0 missing (11 names);
//   import surface unchanged (@hermes/plugin-sdk + react + react/jsx-runtime
//   only, no cross-module import strings in code or comments). Regression
//   guard intact: list/graph/timeline modes, TraceGraphView, TraceTimelineView,
//   view-mode toggle, /trace routes all preserved (two className strings
//   only). No backend/Python touched.
//
// night-shift-tick-20 (this shift): WaveView surface coverage + state-honesty
// copy fixes (verified against abyss_wave.py + plugin_api.py, read-only):
//   1. WaveView fetched only 6 of the 8 wave surfaces — /wave/commands,
//      /wave/platform and /wave/skills exist in the backend (abyss_wave.py
//      tables commands/platform_events/skills, rows dicts with timestamp +
//      surface/command/alias_used/args_preview, platform/event_type/payload,
//      name/action/provenance/details), so their surface counters could grow
//      while their events NEVER appeared in the merged feed. Three guarded
//      fetches + feed mappers added; tones already present in WAVE_TAG_TONE.
//   2. All-failed threshold corrected: the old `failed === 6` counted the
//      summary request too, so a backend whose /wave/summary still answered
//      from SQLite while every surface route was down slid into the empty
//      state (DESIGN.md P0: errors never masquerade as empty data). Now
//      8+ failures of 9 requests (i.e. all eight surface endpoints) print
//      ErrorState; partial failures stay silent as before.
//   3. relativeTime(): dates older than a week printed "Dec 15" with no year
//      even when the entry was from a different year — ambiguous in a
//      transcript. The year is now appended when it differs from the current
//      year (same terminal-dim voice).
//   4. GlobalSearch all-sources-off honesty: with every source toggle off the
//      empty state claimed "No matches for <query> in the selected sources"
//      when NO source was selected. Now prints "All sources off — enable at
//      least one source to see matches."
//   Verified post-edit: node --check OK; braces/parens balanced; import
//   surface unchanged (@hermes/plugin-sdk + react + react/jsx-runtime only),
//   no cross-module import strings, no from- strings in comments; 0 new
//   class tokens (all additions are data strings / plain logic). Regression
//   guard intact: list/graph/timeline modes, TraceGraphView, TraceTimelineView,
//   view-mode toggle, /trace routes all preserved (WaveView fetch/mapping +
//   one pure helper + copy only). No backend/Python touched.
//
// night-shift-tick-21 (this shift): keyboard focus visibility (DESIGN.md
// a11y — "focus rings via theme accent") + a no-op-refetch kill:
//   1. Three keyboard-operable surfaces had ZERO visible focus indication.
//      TraceTimelineView overview lanes (role=button, tabIndex 0, Enter/Space
//      wired in tick-10) got an injected .abyss-focus-ring:focus-visible rule
//      (2px solid var(--ui-accent), offset 1px — theme-driven, added to
//      CONSOLE_CSS; no compiled utility exists for an accent ring). The two
//      canvas keyboards now draw a native phosphor dashed ring in the accent
//      color when focused: PhosphorGraphRenderer (Brain graph) tracks
//      focus/blur on the canvas and paints the ring in _render()
//      (ctx.setLineDash([4,3]) + accent stroke, drawn BEFORE the empty-nodes
//      early return so an empty graph still shows focus); TraceGraphView
//      (trajectory DAG) gained a canvasFocused state + onFocus/onBlur, paints
//      the same ring in draw(), and resets to false on each fetch because the
//      loading spinner unmounts the canvas and React does NOT fire blur on
//      unmount — without the reset a remounted canvas repaints a stale ring.
//   2. CalendarView "today" button no-op refetch: clicking when already on the
//      current week constructed a fresh Date() whose identity change re-fired
//      fetchTasks and flashed the whole 7-cell grid to skeleton for zero data
//      change. The handler now compares getWeekStart() and only navigates when
//      actually off the current week.
//   Verified post-edit: node --check OK; braces/parens balanced; hook-order
//   scan clean (new useState sits with the other hooks above the returns);
//   class-token sweep vs live bundle index-BwB1iTEf.css: 156 tokens, 0 dead
//   (abyss-focus-ring is plugin-injected via CONSOLE_CSS, correctly skipped);
//   codicon sweep: 0 missing (11 names); import surface unchanged
//   (@hermes/plugin-sdk + react + react/jsx-runtime only), no cross-module
//   import strings, no from- strings in comments. Regression guard intact:
//   list/graph/timeline modes, TraceGraphView, TraceTimelineView, view-mode
//   toggle, /trace routes all preserved (focus state + CSS rules + one handler
//   guard only). No backend/Python touched.
//
// night-shift-tick-22 (this shift): host CSS self-update drift re-audit +
// two visual/honesty fixes:
//   1. MUST-DO drift sweep: the host stylesheet self-updated since tick-21 —
//      live bundle is now index-ChgG27Ex.css (was index-BwB1iTEf.css; repo
//      dist + installed win-unpacked match, Aug 22 09:28). Full re-audit of
//      every className token (130 live + 9 plugin-injected abyss-* via
//      CONSOLE_CSS) and all 11 codicon names against the NEW bundle: 0 dead
//      classes, 0 missing codicons — the previous build's verified set
//      survives intact. node --check OK; hook-order scan clean.
//   2. TraceTimelineView error-tick alignment: the failure tick was
//      positioned at `right: 6%` of the LANE (or left edge for bars <=3%),
//      not at the end of the agent's activity bar — for a lane whose bar
//      spans 50% of the axis the red tick floated at ~94%, visually
//      disconnected from the bar it marks. It is now pinned to the bar's
//      end (`left: calc(pct% - 2px)`, parent has overflow-hidden so a
//      100% bar's tick stays inside), so "this agent's activity ended with
//      N failures" reads where the activity actually ended.
//   3. AbyssStatusChip backend-down honesty: with /status unreachable the
//      status-bar chip went silently blank (just the 'abyss' label, no dot)
//      while the pane's StatusStrip prints 'status link down' — a dead
//      backend looked indistinguishable from a first-load flicker. The chip
//      now tracks `fetched` (set after the FIRST fetch resolves) and renders
//      a red 'down' marker once the backend has actually failed; the very
//      first render stays quiet so a healthy startup never flashes a false
//      alarm. Same clear-on-failure policy as the pane's StatusStrip.
//   Verified post-edit: node --check OK; braces/parens balanced; hook-order
//   scan clean (new useState sits with the other hooks above the returns);
//   class-token sweep vs live bundle index-ChgG27Ex.css: 130 tokens, 0 dead
//   (tick-22 additions: text-(--ui-red) confirmed live; the rest are inline
//   styles + a template-string calc()); codicon sweep: 0 missing (11 names);
//   import surface unchanged (@hermes/plugin-sdk + react + react/jsx-runtime
//   only), no cross-module import strings, no from- strings in comments.
//   Regression guard intact: list/graph/timeline modes, TraceGraphView,
//   TraceTimelineView, view-mode toggle, /trace routes all preserved
//   (one tick-position style + chip state/render only). No backend/Python
//   touched.
//
// night-shift-tick-23 (this shift): glance learnability + counter honesty +
// search ergonomics:
//   1. StatusStrip acronym hints: ACT/HLTH/INC/CRN/CAT/SIG is the FIRST
//      VIEWPORT per DESIGN.md, but the bare acronyms offered zero way to
//      learn what they mean. Each metric now carries a `hint` title on its
//      value span (hover tooltip) — including the HLTH/SIG nav buttons —
//      so the meaning is one hover away instead of living only in the docs.
//   2. Watch tab counters truthful from first paint: fetchData was
//      active-tab-only, so the UNVISITED tab's counter read a misleading 0
//      (e.g. 'incidents (0)' while incidents existed) until the operator
//      clicked it. fetchData now Promise.all's BOTH list fetches every poll;
//      activeTab stays in the deps so a tab switch still refetches
//      immediately (fresh list for the newly-visible tab, same cadence).
//      Both endpoints are cheap SQLite reads (limit=50); the stale-preserve
//      catches keep the skeleton guard semantics unchanged.
//   3. GlobalSearch SearchField gained onClear — the SDK's canonical one-
//      click clear for long queries (the cleared fetch short-circuits in
//      fetchResults, so no stale-response race).
//   Verified post-edit: node --check OK; check-hook-order clean; class-token
//   sweep vs live bundle index-ChgG27Ex.css (unchanged since tick-22, repo
//   dist + installed win-unpacked match): 152 tokens, 0 dead (9 abyss-*
//   injected by CONSOLE_CSS, expected); codicon sweep: 0 missing (18 names);
//   braces/parens balanced; import surface unchanged (@hermes/plugin-sdk +
//   react + react/jsx-runtime only), no cross-module import strings, no
//   from- strings in comments. Regression guard intact: list/graph/timeline
//   modes, TraceGraphView, TraceTimelineView, view-mode toggle, /trace
//   routes all preserved (hint titles + one fetch strategy + one SDK prop).
//   No backend/Python touched.
//
// night-shift-tick-24 (this shift): DESIGN.md contract re-check + exact-time
// hover titles.
//   1. Cluster button codicon contract fix: DESIGN.md Components names the
//      Watch 'cluster' button as the `combine` codicon ("deliberately
//      distinct from `refresh`"), but the implementation used `git-merge`.
//      No session history records a conscious choice, and both names exist
//      in the live codicon set — this is drift, now aligned: name 'combine'.
//   2. Exact-time hover titles: relativeTime() prints "2h ago" / "Dec 15"
//      voice throughout, but several surfaces offered no way to see the
//      exact moment — a post-mortem needs the precise timestamp, not just
//      the age. New timeTitle() helper (same cross-year honesty as
//      relativeTime: appends the year only when it differs, plus time of
//      day) wired onto the relative-time spans of Activity Feed rows,
//      Global Search results, Trace list events, Watch signal rows, incident
//      created-meta, and Wave feed rows — 6 sites. Tooltip-verbosity is
//      fine: hover-only, so the fuller date+time is welcome (tick-13
//      hover-hygiene policy extended to timestamps).
//   Audited non-fixes recorded for confidence: (a) TraceTimelineView detail
//   segments use `n.start_ms / L * 100` with no coalesce — verified against
//   the backend get_trace_timeline() that every emitted start_ms passes
//   `_off()` (None → 0), so NaN% cannot occur; no change needed.
//   Verified post-edit: node --check OK; check-hook-order clean; braces/
//   parens balanced; live bundle still index-ChgG27Ex.css (unchanged since
//   tick-22, repo dist + installed win-unpacked match) — no new class
//   tokens added, codicon `combine` verified present; import surface
//   unchanged (@hermes/plugin-sdk + react + react/jsx-runtime only), no
//   cross-module import strings, no from- strings in comments. Regression
//   guard intact: list/graph/timeline modes, TraceGraphView,
//   TraceTimelineView, view-mode toggle, /trace routes all preserved
//   (one codicon name + one helper + six title attributes only).
//   No backend/Python touched.
//
// night-shift-tick-25 (this shift): dead-utility sweep with precise
// boundary matching + two genuine improvements.
//   1. Host CSS re-audit (live bundle index-ChgG27Ex.css, unchanged since
//      tick-22 — no self-update) using a class-token extractor that matches
//      compiled selectors at exact boundaries (\: \. \( \) escapes). Found
//      2 dead utilities the tick-24 grep-style sweep missed:
//      a. m-1 on the BrainGraph container — not compiled, so the intended
//         4px breathing margin around the dithered graph ground silently
//         vanished and the canvas sat flush against the pane edges; margin
//         now inline (style {margin: 4}) per DESIGN.md.
//      b. ml-3 on every TracingView list-mode event row — not compiled
//         (only ml-3.5 exists in the bundle), so the row box shifted 12px
//         left and the absolute event glyph (left:-19px) landed off the
//         timeline spine; marginLeft 12px now inline.
//   2. TraceGraphView keyboard-map hint: the trajectory canvas gained
//      arrow-key selection in tick-16 (same contract as the Brain graph)
//      but had no affordance; a dim non-interactive 'click: select ·
//      arrows: move · esc: clear' overlay now sits bottom-right on the
//      drawn ground (discoverability parity with DESIGN.md's Brain-footer
//      hint). Inline position, no layout impact.
//   3. Watch cluster button busy affordance: while the 10s cluster
//      dispatch runs the disabled button was a static grey-out; it now
//      shows a GlyphSpinner (ariaLabel 'Clustering incidents') so the
//      mutation is visible, not silent.
//   Verified post-edit: node --check OK; check-hook-order clean; class
//   sweep 142 tokens, 0 dead; codicon sweep 11 names, 0 missing; braces/
//   parens balanced; import surface unchanged (@hermes/plugin-sdk + react +
//   react/jsx-runtime only), no from- strings in comments. Regression
//   guard intact: list/graph/timeline modes, TraceGraphView,
//   TraceTimelineView, view-mode toggle, /trace routes all preserved
//   (two inline styles + one hint overlay + one button children only).
//   No backend/Python touched.
//
// night-shift-tick-26 (this shift): dynamic-codicon regression +
// hover-hygiene parity fix.
//   1. P0-viz regression: the Trace view-mode toggle renders its icons via
//      a DYNAMIC name ('list'/'graph'/'history' from a [mode, icon] array),
//      so the static Codicon sweeps of tick-9/tick-12/tick-22 never checked
//      'list' — and the host CSS self-update (index-BwB1iTEf.css →
//      index-ChgG27Ex.css, noted in tick-22) dropped .codicon-list (only
//      codicon-list-flat/ordered/tree/... remain). The list-mode toggle
//      button rendered an invisible empty box (Codicon has NO fallback for
//      an unknown name — the tick-9 failure mode). Verified: .codicon-list
//      has ZERO rules in the live bundle; .codicon-list-flat:before has a
//      real glyph. Toggle array now emits 'list-flat' for the list mode —
//      a flat-list glyph, semantically correct for the flat event list and
//      visually distinct from graph/history. All 20 codicon names in the
//      file (static + dynamic) re-verified live vs the bundle.
//   2. WaveView surface table: the 'last' relative-time cell lacked the
//      exact-time hover title that tick-24 wired onto the feed rows — a
//      post-mortem on a surface counter ('events · 2h ago') could not see
//      the exact moment without hunting the feed. title: timeTitle() added
//      (undefined when no timestamp, so no empty tooltip).
//   Verified post-edit: node --check OK; check-hook-order clean; class
//   sweep 156 tokens, 0 dead (only plugin-injected abyss-* + the known
//   'pass' cn() expression false-positive); full codicon sweep incl.
//   dynamic names: 20/20 live; braces/parens balanced; import surface
//   unchanged (@hermes/plugin-sdk + react + react/jsx-runtime only), no
//   from- strings in comments. Regression guard intact: list/graph/timeline
//   modes, TraceGraphView, TraceTimelineView, view-mode toggle, /trace
//   routes all preserved (one icon-name string + one title attribute only).
//   No backend/Python touched.
//
// night-shift-tick-27 (this shift): glance a11y (screen-reader verdict
// announcement) + WaveView sub-line hover-hygiene parity.
//   1. StatusStrip's ONLY authored motion — the 0.5s opacity flash
//      (flashTick) — is purely VISUAL: a screen-reader operator gets zero
//      announcement when health numbers change and the verdict flips from
//      'all clear' to 'N critical'. The strip is the FIRST VIEWPORT / the
//      "are my agents OK right now?" answer per DESIGN.md THESIS, so the
//      answer must be spoken, not just flashed. A sr-only span with
//      role=status + aria-live=polite now echoes the verdict phrase
//      ('abyss health: all clear / N open / N critical'); polite (not an
//      alert) so a background poll update is a low-priority announcement,
//      and the phrase only changes when the numbers actually change
//      (identical text does not re-fire an aria-live). sr-only verified
//      compiled in the live bundle (index-ChgG27Ex.css) before use.
//   2. WaveView merged-feed rows: the main text line truncates with a
//      title (tick-13 hover-hygiene) but the sub line immediately below it
//      also truncates without one — full detail unreachable at 420px pane
//      width. title: it.sub added (parity with its sibling line).
//   Verified post-edit: node --check OK; check-hook-order clean; class
//   sweep 156 tokens (sr-only added), 0 dead; codicon sweep unchanged
//   (no icon strings touched); braces/parens balanced; import surface
//   unchanged (@hermes/plugin-sdk + react + react/jsx-runtime only), no
//   from- strings in comments. Regression guard intact: list/graph/timeline
//   modes, TraceGraphView, TraceTimelineView, view-mode toggle, /trace
//   routes all preserved (one aria-live span + one title attribute only).
//   No backend/Python touched.
//
// night-shift-tick-28 (this shift): header-pinning + hover-hygiene parity.
//   1. AbyssDashboard fixed headers could compress in a short pane: Masthead
//      and StatusStrip (skeleton/error/data variants) sat in the flex column
//      with the default flex-shrink:1, so when the pane was short the two
//      header rows crushed toward zero height (boot line, metrics, verdict
//      clipped) instead of the tabs area giving up space. TabsList already
//      carried shrink-0 — the headers now do too (4 className strings),
//      so the glance line the operator reads is never squeezed away.
//   2. Full-session-id hover titles on truncated short-id labels (tick-13/24
//      hover-hygiene parity — the expanded value must be readable on hover):
//      Activity Feed 'sid xxxxxxxx', Watch signal 'session:' line, Trace
//      timeline agent-lane short id — the pre-hover text stays 8-char, the
//      title carries the full id for cross-surface correlation.
//   3. Calendar day overflow reveal: '+N more' on a day cell was a dead-end
//      count — tasks 3+ were unreachable. The line now carries a title
//      listing the remaining task titles (coalesced, ' · '-joined).
//   Live-bundle re-audit: index-ChgG27Ex.css unchanged (no self-update since
//   tick-22) — class-token sweep 143/143 LIVE, 0 dead (shrink-0 already in
//   the verified set; the title attributes add no classes); codicon sweep
//   19/19 present; node --check OK; check-hook-order clean; import surface
//   unchanged (@hermes/plugin-sdk + react + react/jsx-runtime only), no
//   cross-module import strings, no new from- strings in comments.
//   Regression guard intact: list/graph/timeline modes, TraceGraphView,
//   TraceTimelineView, view-mode toggle, /trace routes all preserved
//   (className + title attributes only). No backend/Python touched.
//
// night-shift-tick-29 (this shift): first-load counter honesty (WaveView) +
// trajectory lane legend beads (TraceTimelineView). Full DESIGN.md/PRODUCT.md
// contract audit found no state-machinery defects — 28 ticks of hardening
// hold; two genuine UX gaps shipped instead:
//   1. WaveView surface table false-zeroed on a fresh mount: while the
//      /wave/summary fetch was in flight the eight surface rows printed
//      count 0 and '—' last — a transient "all surfaces empty" lie on the
//      very glance the feed below it was still loading (tick-23
//      counter-honesty precedent on the Watch tabs). Pending rows now
//      render dimmed (opacity-60, verified compiled) with counting-dot '·'
//      placeholders in count/last (title 'loading…' on count); real
//      numbers replace them when the summary lands, and background poll
//      refreshes with a cached summary never dim (cached truth stays
//      visible).
//   2. TraceTimelineView trajectory lane labels: backend emits
//      'Reasoning'/'Tools'/'Failures' (verified in get_trace_timeline());
//      'Reasoning' sat within 2px of the w-16 label box with no truncate,
//      crowding the lane bar at 420px pane width. Labels now sit beside a
//      legend-style tone bead (h-1.5 w-1.5 rounded-full, inline
//      backgroundColor = the lane's bar tone — Reasoning purple / Tools
//      green / Failures red), in a w-20 min-w-0 container with truncate +
//      title so longer labels ellipsize cleanly and the full name is one
//      hover away (tick-13/24 hover-hygiene policy).
//   Verified post-edit: node --check OK; check-hook-order clean; new class
//   tokens (opacity-60, w-20, min-w-0, truncate) grep-verified compiled in
//   the live bundle index-ChgG27Ex.css (unchanged since tick-22 — no
//   self-update); braces/parens balanced; import surface unchanged
//   (@hermes/plugin-sdk + react + react/jsx-runtime only), no cross-module
//   import strings, no quoted-specifier strings in comments. Regression
//   guard intact: list/graph/timeline modes, TraceGraphView,
//   TraceTimelineView, view-mode toggle, /trace routes all preserved
//   (one pending const + two cell ternaries + one lane-label span only).
//   No backend/Python touched.
//
// night-shift-tick-30 (this shift): HealthView mono-voice + truncation
// hygiene parity (DESIGN.md Type — "body copy is mono too… the diagnostic
// report prints in-world" — and the tick-13/18 hover/truncation contracts):
//   1. Five previously-sans body spans in the health report now carry
//      abyss-mono, matching every sibling span in the instrument (the
//      w-8 counts, bar values and findings rows were already mono; the
//      benchmark probe id, benchmark probe detail, score-breakdown
//      label, failure-taxonomy section title and its 'none' empty line,
//      and the doctor applied-fixes row rendered in the host sans face —
//      a type-stack seam in the first report an operator reads after a
//      signal). All are text copies of the report, so the whole sheet now
//      reads in the phosphor stack.
//   2. Benchmark probe detail (p.detail) and failure-taxonomy item
//      (it.type||it.tool||it.message) are `truncate` spans inside flex
//      rows that lacked min-w-0 (tick-18 flex-bug: truncate alone cannot
//      shrink below content width, so long probe details / error
//      messages overflowed the row instead of ellipsizing) and lacked a
//      title (tick-13 hover-hygiene: the full value was unreachable at
//      420px pane width). Both now carry min-w-0 + title, so long text
//      ellipsizes cleanly and the full value is one hover away.
//   Verified post-edit: node --check OK; check-hook-order clean (0
//   violations); class-token sweep vs live bundle index-ChgG27Ex.css
//   (unchanged since tick-22 — no self-update): new tokens min-w-0 +
//   truncate grep-confirmed compiled; abyss-mono is plugin-injected via
//   CONSOLE_CSS (expected); codicon sweep 11/11 present; import surface
//   unchanged (@hermes/plugin-sdk + react + react/jsx-runtime only), no
//   cross-module import strings, no from- strings in comments. Regression
//   guard intact: list/graph/timeline modes, TraceGraphView,
//   TraceTimelineView, view-mode toggle, /trace routes all preserved
//   (6 className strings + 2 title attributes only). No backend/Python
//   touched.
//
// night-shift-tick-31 (this shift): two UX-honesty/defensive fixes found by
// a full 30-tick-state re-audit:
//   1. ActivityFeed filter-switch honesty: the 30s poll intentionally keeps
//      cached rows while it runs (tick-2 no-blink policy), but a MANUAL
//      category-switch fetch had the same keep-cached behavior — the previous
//      category's rows stayed visible underneath the newly-highlighted filter
//      button for the whole switch fetch, and a failed switch fetch left them
//      there indefinitely (errors masquerading as data of a different slice).
//      New loadedFilterRef records which category the visible rows actually
//      belong to: the skeleton now shows while a switch fetch is in flight
//      (ref mismatch), a failed switch fetch drops the stale rows so the
//      ErrorState surfaces, and same-filter background-poll failures still
//      preserve the cached list (ref matched — policy unchanged).
//   2. TraceTimelineView trajectory-detail lanes: the overview lane track
//      clips its error tick with overflow-hidden (tick-22), but the detail
//      lane tracks did NOT — a segment whose start+duration exceeds total_ms
//      (the LAST event in a trajectory almost always has start near L, plus
//      its own duration) leaked out of the rounded track past the right
//      edge. Track now carries overflow-hidden (parity with the overview
//      lane; overflow-hidden verified compiled in the live bundle
//      index-ChgG27Ex.css) and the segment width is capped at 100% so the
//      rounded right corner stays clean rather than clipped mid-radius.
//   Verified post-edit: node --check OK; hooks review clean (one useRef
//   added above all early returns in ActivityFeed, zero hooks touched in
//   TraceTimelineView); braces balanced; no new class tokens beyond
//   overflow-hidden (confirmed compiled); import surface unchanged
//   (@hermes/plugin-sdk + react + react/jsx-runtime only), no cross-module
//   import strings, no from- specifier strings in comments. Regression
//   guard intact: list/graph/timeline modes, TraceGraphView,
//   TraceTimelineView, view-mode toggle, /trace routes all preserved
//   (one ref + two render guards + one className + one Math.min only).
//   No backend/Python touched.
//
// night-shift-tick-32 (this shift): CalendarView live poll with no-blink
// + week-honesty guards — the calendar was the ONLY live surface without
// polling (StatusStrip 30s, ActivityFeed 30s, Watch 30s, Trace agents 30s,
// Wave 15s all refresh in place), so a task scheduled by a running cron
// never appeared while the operator sat on the calendar. Shipped the poll
// with the same two guards every other surface uses:
//   1. loadedWeekRef tracks which week's tasks the visible grid actually
//      belongs to. The 30s background poll flips loading=true but the ref
//      still matches → the grid silently refreshes in place (no-blink,
//      tick-2 policy, ActivityFeed tick-31 parity). A WEEK-SWITCH fetch
//      (ref mismatch) shows the skeleton instead of painting the previous
//      week's rows under the new week's header.
//   2. A week-SWITCH fetch that fails drops the stale rows so the
//      ErrorState surfaces (errors never masquerade as data of a different
//      week). Same-week background-poll failures keep the cached grid — a
//      transient 30s blip must not blank the calendar the operator is
//      reading; the next successful poll clears the error. ErrorState now
//      requires tasks.length === 0 (previously ANY error replaced the
//      grid, which would have made the new poll blink).
//   One useRef added above all early returns (hook order safe);
//   fetchTasks effect gained a 30s setInterval with teardown. All class
//   tokens reused from the verified set (no new tokens). Verified
//   post-edit: node --check OK (4881 lines); check-hook-order clean;
//   class-token sweep vs live bundle index-ChgG27Ex.css (unchanged since
//   tick-22 — no self-update): all 153 tokens LIVE, 0 dead; braces
//   balanced 1215/1215; import surface unchanged (@hermes/plugin-sdk +
//   react + react/jsx-runtime only), no from- specifier strings in
//   comments. Regression guard intact: list/graph/timeline modes,
//   TraceGraphView, TraceTimelineView, view-mode toggle, /trace routes all
//   preserved (one ref + one interval + two guard conditions + one
//   conditional drop + one ErrorState condition only). No backend/Python
//   touched.
//
// night-shift-tick-33 (this shift): discoverability + a11y affordance
// completion. Full DESIGN.md/PRODUCT.md contract re-audit found no state or
// layout defects — 32 ticks of hardening hold; live-bundle re-verification
// (index-ChgG27Ex.css unchanged since tick-22 — no self-update) shows all 153
// class tokens compiled (0 dead; the only misses are the known 'pass'/
// 'pending' benchmark-status string false positives), 19/19 codicon names
// present, node --check OK, hook-order scan clean, every backend route the
// UI calls confirmed present in dashboard/plugin_api.py (read-only). Three
// small gaps shipped instead:
//   1. Trace view-mode toggle tooltips: the segmented toggle is icon-only
//      (list-flat/graph/history) with aria-labels but no hover hint, so a
//      mouse operator guessing which icon means timeline asked an
//      unanswerable question at 420px pane width. Each toggle Button now
//      carries title '<mode> view' (DESIGN.md names the modes list / graph /
//      timeline; hover parity with the tick-23 acronym hints).
//   2. TraceTimelineView overview lanes announce their activation: the lanes
//      became role=button + tabIndex 0 + Enter/Space drill in tick-10, but
//      the aria-label never told a keyboard/SR operator the lane is
//      activateable — 'press Enter' affordance was invisible outside the
//      code. aria-label now appends '— Enter opens its trajectory'.
//   3. StatusStrip 'live:' prefix hover hint: the strip is the FIRST
//      VIEWPORT per DESIGN.md and auto-refreshes every 30s, but a new
//      operator had no way to learn the numbers update on their own; the
//      prefix now carries title 'auto-refreshes every 30s' (learnability
//      parity with tick-23's acronym hints).
//   Verified post-edit: node --check OK; check-hook-order clean; no new
//   class tokens (one title attribute + one aria-label template string +
//   one comment only); braces/parens balanced; import surface unchanged
//   (@hermes/plugin-sdk + react + react/jsx-runtime only), no cross-module
//   import strings, no from- specifier strings in comments. Regression
//   guard intact: list/graph/timeline modes, TraceGraphView,
//   TraceTimelineView, view-mode toggle, /trace routes all preserved
//   (title + aria-label strings only). No backend/Python touched.
//
// night-shift-tick-34 (this shift): WaveView state-honesty — the last
// surface following the DESIGN.md States contract every other live surface
// adopted (tick-11/14/31/32 preserve-on-blip + counter-honesty):
//   1. Feed ErrorState was UNGATED — any error (including a transient 15s
//      poll blip) replaced the cached feed with an ErrorState, blanking
//      telemetry the operator was reading. Every sibling surface gates
//      error surfaces on 'cache also gone' (ActivityFeed, CalendarView,
//      SignalsIncidentsView, TraceTimelineView all require length===0);
//      WaveView was the lone outlier. The feed error branch now requires
//      feed.length === 0, so a background blip keeps the cached rows and the
//      next successful poll clears the error. A truly dead backend still
//      surfaces ErrorState (fetchAll clears feed when failed >= 8 — the
//      DESIGN.md P0 'errors never masquerade as empty data' is preserved).
//   2. Surface-count table fabricated ZEROS when /wave/summary failed on a
//      LATER poll: `pending` only covered the first load, so a summary
//      fetch failure mid-session (surfaces answering → failed < 8 → no
//      top-level ErrorState) set summary to null and printed count 0 for
//      every surface next to a live feed — a false 'all surfaces empty'.
//      New summaryDown (!loading && !summary) renders dimmed rows with an
//      em-dash count, title 'summary link down — counts unavailable' —
//      a data-loss state is disclosed, never a silent zero (tick-23/29
//      counter-honesty parity).
//   Verified post-edit: node --check OK; check-hook-order clean; no new
//   class tokens (opacity-60 already in the verified set; em-dash/text are
//   data strings); braces/parens balanced; live bundle still
//   index-ChgG27Ex.css (no self-update); import surface unchanged
//   (@hermes/plugin-sdk + react + react/jsx-runtime only), no cross-module
//   import strings, no from- specifier strings in comments. Regression
//   guard intact: list/graph/timeline modes, TraceGraphView,
//   TraceTimelineView, view-mode toggle, /trace routes all preserved
//   (one const + two render ternaries + one ErrorState gate only).
//   No backend/Python touched.

// night-shift-tick-35 (this shift): hover-parity + counter-cap-honesty.
//   1. Trace list-mode rows (TracingView) gained abyss-row-hover — every
//      other list surface in the instrument (activity rows, search results,
//      signal/incident rows, wave feed) rolls over with the phosphor row
//      hover (DESIGN.md States: "rows hover:bg-(--ui-bg-tertiary)"); the
//      trace event list was the lone exception. Class-only
//      (plugin-injected via CONSOLE_CSS).
//   2. Watch tab counters disclose the 50-row fetch cap: /signals and
//      /incidents are fetched with limit=50, so a list pinned at exactly 50
//      rows usually means MORE exist — the old `signals (50)` next to the
//      StatusStrip's `200 open` was a false count (tick-23/29/34
//      counter-honesty policy). The tabs now render `signals (50+)` /
//      `incidents (50+)` when at the cap, with a title pointing at the
//      strip for the exact open total; lists under 50 are true counts and
//      render unchanged.
//   Baseline re-verified: node --check OK; check-hook-order clean; class-token
//   sweep vs live bundle index-ChgG27Ex.css (unchanged since tick-22 — no
//   self-update): 164 tokens, 0 dead (only the known 'pass'/'pending'
//   benchmark-status string false positives); codicon sweep 0 missing;
//   import surface unchanged (@hermes/plugin-sdk + react + react/jsx-runtime
//   only), no from- strings in comments. NOTE: check_balance_acorn.js reports
//   a stale false failure at 1899:13 on the untouched trace-view section —
//   pre-existing tooling/dialect mismatch; node --check (V8, the actual
//   Electron runtime) is authoritative and passes. Regression guard intact:
//   list/graph/timeline modes, TraceGraphView, TraceTimelineView, view-mode
//   toggle, /trace routes all preserved (one className + two label consts +
//   two title attrs only). No backend/Python touched.
//
// night-shift-tick-36 (this shift): counter-cap-honesty completion + search
// source-facet counts. Full DESIGN.md/PRODUCT.md contract re-audit found no
// state-machinery or layout defects — 35 ticks of hardening hold; the last
// two list surfaces still under-disclosed their 50-row fetch cap (Watch tabs
// got 50+ in tick-35; Activity and Search were the holdouts), and Global
// Search source toggles offered no way to see where hits live before
// clicking. Three small changes:
//   1. ActivityFeed cap disclosure: /activity is fetched with limit=50, so a
//      feed pinned at exactly 50 rows usually means MORE exist — previously
//      the feed sat silently at 50 next to a strip ACT total of hundreds
//      (false total). The filter bar now right-pins a dim `50+` marker
//      (ml-auto shrink-0 abyss-micro) whenever activities.length >= 50,
//      with a title pointing at the strip's ACT metric for the exact total
//      (tick-35 parity, same voice).
//   2. GlobalSearch per-source match counts: the source toggle buttons
//      (Memory/Session/Task/Activity) now carry live counts from the RAW
//      fetched sample — `Memory (3) · Task (0)` — so the operator sees
//      where the hits are before toggling blind. Counts render only when
//      the sample honestly matches the CURRENT query (query >= 2 chars AND
//      not loading — the tick-8 same-query policy), 0-count sources render
//      dimmed (text-quaternary), and the aria-label announces the count to
//      screen readers. When the raw sample is at the backend cap (50), a
//      title discloses 'matches in the first 50 results'.
//   3. GlobalSearch count line cap: `N results for "query"` now prints
//      `50+ results` when the visible set is pinned at the fetch cap
//      (title: 'showing the first 50 matches — more may exist') — the last
//      undislosed list surface, tick-35 counter-honesty completion.
//   Verified post-edit: node --check OK; check-hook-order clean (consts
//   only, zero hooks touched — all additions sit with the existing
//   render-time consts above the early returns); new/used class tokens
//   verified in the live bundle index-ChgG27Ex.css (unchanged since
//   tick-22 — no self-update): ml-auto 1, shrink-0 4, tabular-nums 2,
//   whitespace-nowrap 1, text-(--ui-text-quaternary) 2, text-(--ui-text-
//   tertiary) 5, h-6 3, gap-1 2 — all LIVE, abyss-micro injected via
//   CONSOLE_CSS; import surface unchanged (@hermes/plugin-sdk + react +
//   react/jsx-runtime only), no cross-module import strings, no from-
//   specifier strings in comments. Regression guard intact: list/graph/
//   timeline modes, TraceGraphView, TraceTimelineView, view-mode toggle,
//   /trace routes all preserved (one filter-bar span + two const blocks +
//   button children + one count-line string only). No backend/Python
//   touched.
//
// night-shift-tick-37 (this shift): terminal-voice loading states for the
// Trace family (DESIGN.md States contract: "pulsing skeleton blocks or
// GlyphSpinner (braille) + terminal voice"). Full DESIGN.md/PRODUCT.md
// contract re-audit found no state or layout defects — 36 ticks of
// hardening hold — but the loading contract was only half-implemented:
// BrainGraph ('building brain…') and WaveView ('listening for wave
// telemetry…') carry the terminal voice next to their GlyphSpinners, while
// all FOUR trace loading surfaces rendered a bare braille spinner with no
// voice — an ambiguous centered spinner at 420px pane width says nothing
// about what is being built. Each now prints its terminal line:
//   - TraceGraphView graph load: 'building trajectory…'
//   - TraceTimelineView agents overview: 'loading agents…'
//   - TraceTimelineView trajectory detail: 'loading trajectory…'
//   - TracingView list-mode events: 'loading trace…'
// Voice spans reuse the BrainGraph pattern (text-sm/text-xs +
// text-(--ui-text-secondary|tertiary) + abyss-mono; className-only
// changes + gap-2 on the existing flex containers — no new state, no
// hooks, no layout restructure).
//   Verified post-edit: node --check OK; check-hook-order clean; new class
//   tokens (gap-2, items-center) grep-verified compiled in the live bundle
//   index-ChgG27Ex.css (unchanged since tick-22 — no self-update); braces
//   balanced; codicon sweep unchanged (no icon strings touched); import
//   surface unchanged (@hermes/plugin-sdk + react + react/jsx-runtime
//   only), no cross-module import strings, no from- specifier strings in
//   comments. Regression guard intact: list/graph/timeline modes,
//   TraceGraphView, TraceTimelineView, view-mode toggle, /trace routes all
//   preserved (loading-branch children only). No backend/Python touched.
//
// night-shift-tick-39 (this shift): P0 ESM parse repair + link-down a11y parity.
//   1. P0 (plugin un-loadable): a stray ')' had broken the TraceGraphView
//      children-array terminator (an extra `])` after the keyboard-map hint
//      overlay), so the Electron blob-URL loader's `import(plugin.js)` threw
//      "Unexpected token ')'" and the ENTIRE Abyss plugin failed to register.
//      The CJS `node --check` the verify scripts used PARSED IT AS COMMONJS
//      and reported SYNTAX_OK — the regression guard was toothless for ESM,
//      the exact failure mode the loader uses. Fixed the stray paren; the
//      verify script now runs a real `node --input-type=module --check`
//      (verify_tick39.sh) that reproduces what the loader parses.
//   2. StatusStrip link-down a11y parity: tick-27 added the sr-only
//      role=status echo for the DATA branch ('all clear / N open / N
//      critical'), but the ERROR branch — 'status link down' + retry, which
//      is exactly when the "are my agents OK?" answer becomes unknowable —
//      had no spoken announcement. It now echoes 'abyss health: status link
//      down' in the same polite live region, so a screen-reader operator
//      hears the failure state flip instead of silently losing the glance.
//   Verified post-edit: node --input-type=module --check PASS (the runnable
//   ESM syntax the loader parses) + legacy CJS check OK; check-hook-order
//   clean; class-token sweep vs live bundle index-ChgG27Ex.css (unchanged
//   since tick-22): sr-only confirmed compiled; import surface unchanged
//   (@hermes/plugin-sdk + react + react/jsx-runtime only), no cross-module
//   import strings, no from- specifier strings in comments. Regression
//   guard intact: list/graph/timeline modes, TraceGraphView,
//   TraceTimelineView, view-mode toggle, /trace routes all preserved
//   (one ')' removed + one aria-live span + verify script only). No
//   backend/Python touched.
//
// night-shift-tick-38 (this shift): drill-preset one-shot completion +
// truncation-hygiene parity. Full DESIGN.md/PRODUCT.md contract re-audit,
// live-bundle re-verification (index-ChgG27Ex.css unchanged since tick-22 —
// no self-update; node --check OK; hook-order scan clean; 158 class tokens
// 0 real dead — 'last:mb-0' and 'pass' are the documented scanner false
// positives; codicon sweep 19/19) — the state machinery holds, but the
// audit found one genuine interaction defect and two hover-parity gaps:
//   1. Stale drill-preset resurrection (interaction fix). openTrace() wrote
//      tracePreset into AbyssDashboard state and nothing ever cleared it.
//      TracingView's presetHandledRef (tick-13) lives INSIDE the view, so
//      remounting the trace tab (conditional render on activeTab) reset it
//      to null while the OLD preset still sat in the dashboard — drill into
//      session A, pick session B manually, leave the trace tab, return:
//      the preset re-applied and snapped the selection BACK to A. The same
//      hijack tick-13 fixed within a mount was re-entering through the
//      remount door. TracingView now accepts onPresetConsumed and reports
//      after applying; AbyssDashboard clears tracePreset on consume (stable
//      clearTracePreset useCallback, declared with the other hooks), so a
//      drill is a true one-shot and a later return to the trace tab starts
//      from the natural most-recent session. One prop + one line in the
//      existing effect + one stable callback; hook count per component
//      unchanged in TracingView, +1 useCallback in AbyssDashboard (top of
//      component, React 310-safe).
//   2. Watch resolution-note full-value titles: signal and incident rows
//      clamp `fix: <note>` to 2 lines (line-clamp-2) with no title — the
//      doctor's root-cause summary became unreadable at 420px pane width.
//      Both now carry the full note on hover (tick-13/24 hover-hygiene
//      parity for clamped dynamic data).
//   3. Masthead truncation titles: the boot line
//      ('$ ./abyss --observe --local --cloud-fix', truncates at pane width
//      per tick-8) and the caption line ('self-diagnostics · … · hermès
//      brain') ellipsize with no hover title — the first lines an operator
//      reads were the only truncating text left without one. Both now carry
//      titles.
//   Verified post-edit: node --check OK; check-hook-order clean; braces
//   balanced; no new class tokens (title attributes only); import surface
//   unchanged (@hermes/plugin-sdk + react + react/jsx-runtime only), no
//   cross-module import strings, no new from- specifier strings in comments.
//   Regression guard intact: list/graph/timeline modes, TraceGraphView,
//   TraceTimelineView, view-mode toggle, /trace routes all preserved
//   (one prop + one callback + four title attributes only). No
//   backend/Python touched.

// night-shift-tick-40 (this shift): counter-cap-honesty completion (WaveView
// feed) + doctor proposed-fix hover-hygiene parity. Full DESIGN.md/PRODUCT.md
// contract re-audit found no state or layout defects — 39 ticks of hardening
// hold — but the design review of the last two surfaces found:
//   1. WaveView merged feed cap disclosure: the feed is sliced to 40 items
//      (items.slice(0, 40)) but — unlike ActivityFeed ('50+'), Watch tabs
//      ('50+'), and GlobalSearch ('50+ results', all tick-35/36 counter-
//      honesty policy) — the operator had NO way to know more telemetry
//      exists beyond the visible merged sample next to a table of exact
//      per-surface totals. The feed now gets a terminal-table header row
//      (matching the surface table's own 'surface | count | last' header
//      above it) with a right-pinned dim '40+' marker when feed.length >=
//      40, title pointing at the per-surface table for exact totals.
//   2. HealthView doctor proposed-fix action title: fx.action is clamped to
//      2 lines (line-clamp-2) with no title — the full remediation step the
//      doctor's agent proposes was unreachable at pane width (tick-38 fixed
//      the signal/incident 'fix:' notes but this was the last clamped
//      dynamic block without a hover title). title: fx.action added.
//   Verified post-edit: node --input-type=module --check PASS (the ESM
//   syntax the Electron loader parses) + CJS check OK; acorn parse OK;
//   check-hook-order clean; string-aware brace/paren balance BALANCED
//   (0/0/0); all class tokens re-verified against the live bundle
//   index-ChgG27Ex.css (unchanged since tick-22 — no self-update; only the
//   plugin-injected abyss-* classes are absent from host CSS, expected);
//   import surface unchanged (@hermes/plugin-sdk + react + react/jsx-runtime
//   only), no cross-module import strings, no from- specifier strings in
//   comments. Regression guard intact: list/graph/timeline modes,
//   TraceGraphView, TraceTimelineView, view-mode toggle, /trace routes all
//   preserved (one feed header block + one title attribute only). No
//   backend/Python touched.
//
// night-shift-tick-41 (this shift): calendar type-stack seam + 'today'
// affordance. Full DESIGN.md/PRODUCT.md contract re-audit + live-bundle
// re-verification (index-ChgG27Ex.css unchanged since tick-22 — no
// self-update; node --input-type=module --check PASS; check-hook-order
// clean; strict className sweep 113/113 live, 0 dead; codicon sweep all
// present; import surface unchanged) found no state or layout defects —
// 40 ticks of hardening hold. Two genuine UX gaps shipped instead:
//   1. CalendarView weekday column headers (Sun–Sat) rendered in the host
//      SANS face while the day numbers directly beneath them carry
//      abyss-mono — a type-stack seam inside the grid (DESIGN.md Type:
//      "monospace type… throughout", micro-labels uppercase +
//      tracking-widest; every sibling micro-label row — Wave surface
//      header, feed header, tab labels — wears abyss-mono). The header
//      row now carries abyss-mono (tick-30 HealthView parity).
//   2. CalendarView 'today' button offered zero affordance when the
//      visible week already IS the current week: the tick-21 no-op guard
//      made the click die silently, so the button read as broken. A new
//      isCurrentWeekShown const (plain const, no hook) disables the
//      button with an explanatory title ('already showing the current
//      week' / 'Jump to the current week'); the no-op-refetch guard is
//      kept behind the disabled state, so the tick-21 protection holds.
//   Verified post-edit: node --input-type=module --check PASS + CJS check
//   OK; check-hook-order clean (const only, zero hooks touched); braces/
//   parens balanced; class sweep 0 dead (abyss-mono is plugin-injected
//   via CONSOLE_CSS, expected); codicon sweep unchanged; import surface
//   unchanged (@hermes/plugin-sdk + react + react/jsx-runtime only), no
//   cross-module import strings, no from- specifier strings in comments.
//   Regression guard intact: list/graph/timeline modes, TraceGraphView,
//   TraceTimelineView, view-mode toggle, /trace routes all preserved
//   (one className + one const + two Button props only). No backend/
//   Python touched.
//
// night-shift-tick-42 (this shift): silence disclosure on the glance.
// The Aug-24 backend shift added liveness metadata to /status
// (last_activity_at / last_signal_at / last_error_at) so an operator can
// tell how long hooks have been silent — but the UI ignored it, so a
// gateway that stopped recording (dead hooks, misload, session death)
// still rendered big healthy-looking counts ("1073 critical") while the
// agents had been dark for hours. That is exactly the silent-failure
// class Abyss exists to surface. Now:
//   1. New idleLabel() helper (date-helper section): relative-time
//      "silence since last activity" — null while fresh (<15m, no suffix
//      noise), yellow '· idle 15m' in the quiet window, red '· idle 5h'
//      once the backend's own 30s poll has seen nothing for an hour+.
//   2. StatusStrip verdict append: both verdict branches (nav button and
//      plain span) carry the micro-label suffix with a hover title
//      ('last recorded activity <ts> — hooks may have stopped firing');
//      the nav arrow moves after the suffix ('1073 critical · idle 5h ›').
//   3. StatusStrip sr-only echo now includes the idle phrase so a
//      screen-reader operator hears the silence flip, not just the counts.
//   4. AbyssStatusChip: an idle-critical state (>=1h) overrides even a
//      healthy-looking score to red dot + red text, and the chip tooltip
//      shows 'last activity <ts> · idle <n>' — the always-visible
//      statusbar element must not read as "fine" while agents are dark.
//   Verified post-edit: node --input-type=module --check PASS + CJS check
//   OK; check-hook-order clean (helper + consts only, zero hooks touched);
//   braces/parens balanced; class sweep 0 dead (all tokens reused from
//   existing verified set: abyss-tiny, tracking-widest, text-(--ui-red),
//   text-(--ui-yellow), shrink-0); codicon sweep unchanged; import
//   surface unchanged (@hermes/plugin-sdk + react + react/jsx-runtime
//   only), no cross-module import strings, no new from- specifier strings
//   in comments. Regression guard intact: list/graph/timeline modes,
//   TraceGraphView, TraceTimelineView, view-mode toggle, /trace routes
//   all preserved (one helper + one const + one suffix node + verdict
//   children + chip tone/title only). No backend/Python touched.
//
// night-shift-tick-43 (this shift): HealthView liveness — the last
// non-polling live surface + data-freshness disclosure on the health
// header. A cadence audit (tick-32 fixed Calendar and claimed "the calendar
// was the ONLY live surface without polling") found HealthView had slipped
// through: StatusStrip/ActivityFeed/Watch/Calendar/Trace-agents poll at 30s
// and Wave at 15s, but the health tab — the exact surface that answers
// "are my agents OK right now?" — fetched once at mount and froze. The
// score could sit at a stale 46/100 while every other pane refreshed, and
// a doctor fix or triage change never moved the number until remount.
//   1. Live poll: fetchAll now runs on a 30s interval (cadence parity,
//      returned teardown). The skeleton guard changed from `if (loading)`
//      to `if (loading && !health && !trends && !failures)` so the
//      background poll (which flips loading=true for the fetch's duration)
//      silently refreshes in place instead of punching the whole report to
//      skeleton every half minute — the Doctor/benchmark report state lives
//      above the guard and never remounts. ErrorState still fires only when
//      /health itself has no cached payload.
//   2. Freshness disclosure: fetchAll now also guards /status (4th fetch —
//      partial-failure tolerance keeps a /status blip silent). The header
//      prints `· idle <n>` next to the counts when last_activity_at is old
//      (tick-42 idleLabel tones: yellow <1h, red >=1h, null while fresh —
//      no suffix noise), with a hover title carrying the exact timestamp
//      and "hooks may have stopped firing". A healthy-looking score on dark
//      hooks now reads honestly, mirroring the StatusStrip verdict.
//   Verified post-edit: node --input-type=module --check PASS + CJS check
//   OK; check-hook-order clean (one useState + one interval + one const +
//   one suffix node; zero hooks after early returns); braces/parens
//   balanced; class sweep 0 dead (tokens reused from the existing verified
//   set: abyss-tiny, tracking-widest, shrink-0, text-(--ui-red),
//   text-(--ui-yellow)); import surface unchanged (@hermes/plugin-sdk +
//   react + react/jsx-runtime only), no cross-module import strings, no new
//   from- specifier strings in comments. Regression guard intact:
//   list/graph/timeline modes, TraceGraphView, TraceTimelineView, view-mode
//   toggle, /trace routes all preserved (HealthView state/fetch/render
//   only). No backend/Python touched.
//
// night-shift-tick-44 (this shift): in-flight remediation disclosure on the
// glance. The backend aggregates `resolutions_running` in /status (signals +
// incidents with resolution_status='running'), but the UI never surfaced it —
// the Watch tab's per-row "resolving…" only samples the top-50 fetch, so a
// fix running on row 60 was invisible, and the StatusStrip verdict ("are my
// agents OK right now?") could not tell the operator a doctor/resolver was
// actively working. On dark hooks ("idle 5h") a still-running resolution is
// the one sign of life left. Now:
//   1. New resolvingEl span after idleEl — '· N resolving', tone via inline
//      var(--ui-blue) (no compiled text-(--ui-blue) class exists; only
//      red/yellow/green/accent do — Calendar "running" glyph convention),
//      hover title disclosing the count and the 8s Watch poll cadence.
//   2. Wired into BOTH verdict branches (nav Button and plain span).
//   3. StatusStrip sr-only echo extended so a screen-reader operator hears
//      '· N resolving' (tick-27/39 a11y parity).
//   Verified post-edit: node --input-type=module --check PASS + CJS check
//   OK; check-hook-order clean (const + two render children + one aria
//   string only, zero hooks touched); class tokens reused from the verified
//   set; import surface unchanged. No backend/Python touched.
//
// night-shift-tick-45 (this shift): statusbar-chip remediation disclosure —
// the always-visible surface closes the loop tick-44 opened on the pane's
// StatusStrip. The chip is the ONLY abyss surface an operator sees while the
// dashboard is closed, yet it stayed silent while a cloud-agent fix ran: a
// healthy-looking score with a resolver actively working read as "fine" —
// exactly the silent-state class ticks 42/44 target. Now:
//   1. Blue companion dot next to the health dot when
//      status.resolutions_running > 0 (inline var(--ui-blue), same
//      no-compiled-class convention as tick-44).
//   2. Chip tooltip extended with '· N cloud-agent fix(es) in flight' after
//      the existing activity/health disclosure, so hover explains the blue
//      dot without leaving the statusbar.
//   Zero new imports, zero hooks (one const + one conditional child + one
//   title concatenation). Verified post-edit: node --input-type=module
//   --check PASS + CJS check OK; check-hook-order clean; braces/parens
//   balanced. No backend/Python touched.
//
// night-shift-tick-46 (this shift): statusbar-chip screen-reader parity —
// the always-visible surface's disclosures were visual-only. The chip's
// idle tone, red/blue companion dots and hover title (ticks 42/45) never
// reach a screen reader: an operator tabbing the statusbar heard just
// 'abyss 87' and could not tell critical silence from a healthy score,
// nor that a cloud-agent fix was in flight — the exact silent-to-AT class
// ticks 27/39/44 closed on the pane's StatusStrip, which the chip never
// inherited. Now an sr-only span inside the chip's status span joins the
// button's accessible name so focusing it speaks the full disclosure:
// 'abyss health 87, fair, idle 2h, 1 cloud-agent fix in flight'. Zero new
// imports, zero hooks (one conditional sr-only child only; visible pixels
// unchanged). Verified post-edit: node --input-type=module --check PASS +
// CJS check OK; string-aware brace/paren balance clean; sr-only confirmed
// compiled (StatusStrip precedent); no backend/Python touched.
//
// night-shift-tick-47 (this shift): thousands-separator formatting for
// lifetime counts — the glance stopped being parseable at current data
// volumes. The strip's ACT tile printed total activities as '25875' and SIG
// open signals as '3382'; the HealthView header line printed
// '1073 errors · 4437 open signals · ...'; every four+ digit count rendered
// as an opaque digit blob. Now a fmtCount() helper (en-US grouping, matching
// the dashboard's existing toLocaleString locale use) is applied at every
// big-count render site:
//   1. StatusStrip tiles ACT/INC/CRN/CAT/SIG (HLTH stays raw — a 0–100
//      score never needs a separator).
//   2. StatusStrip verdict phrase ×3 — nav Button branch, plain-span branch,
//      and the sr-only live-region echo (a11y parity kept: screen readers
//      now hear '3,382 open' with the same grouping).
//   3. HealthView header summary line (errors / open signals / open
//      incidents / actions-24h).
//   4. Chip fallback count (shown only when /status has no score yet).
// Sub-1,000 counts are byte-identical, so compact tabular tiles are
// unchanged in the common case. Zero new imports, zero hooks, zero class
// tokens added. Verified post-edit: node --input-type=module --check PASS +
// CJS check OK; string-aware brace/paren balance clean; no backend/Python
// touched.
//
// night-shift-tick-48 (this shift): Wave feed human-readable durations and
// counts — the WaveView merged feed and surface-counter table were the last
// views still printing raw units. API/subagent rows printed LLM call
// durations as raw milliseconds ('125000ms'), stream rows printed char and
// delta totals as ungrouped digits ('128400 chars · 512 deltas'), and the
// surface table's lifetime count cell printed e.g. '25875' — all while every
// other view already speaks fmtDur() ('2m 5s') and fmtCount() ('25,875',
// tick-47). Now:
//   1. api + subagents feed rows use fmtDur(r.duration_ms).
//   2. streams rows wrap chars/deltas in fmtCount().
//   3. surface-counter count cell wraps info.count in fmtCount().
// Sub-second durations ('450ms') and sub-1,000 counts are byte-identical.
// Zero new imports, zero hooks (fmtDur/fmtCount are module-scope helpers,
// hoisted above WaveView). Verified post-edit: node --input-type=module
// --check PASS + CJS check OK; string-aware brace/paren balance clean;
// no backend/Python touched.
