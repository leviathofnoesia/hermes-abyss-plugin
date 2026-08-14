/**
 * Abyss Dashboard Plugin — Raindrop-style Observability for Hermes
 * Activity Feed, Calendar, Global Search, Tracing, Brain Graph, Signals & Incidents.
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
 * (ACT/CRN/CAT/SIG), then the six views behind terminal-style tabs; the Brain
 * graph is the soul, drawn on canvas with Atkinson dithering.
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
  cn, host, Button, Badge, Codicon, Separator, EmptyState, ErrorState,
  SearchField, Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
  Tabs, TabsList, TabsTrigger, GlyphSpinner
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'
import { useEffect, useState, useMemo, useCallback, useRef } from 'react'

const ID = 'abyss'

// ---------------------------------------------------------------------------
// Theme helpers — read computed CSS variable values for canvas (canvas cannot
// resolve var() strings). All colors stay inside the host theme system.
// ---------------------------------------------------------------------------
function themeColor(name, fallback) {
  try {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
    return v || fallback
  } catch {
    return fallback
  }
}
function palette() {
  return {
    session: themeColor('--ui-blue', '#4d9fff'),
    tool: themeColor('--ui-green', '#3fb57a'),
    memory: themeColor('--ui-purple', '#b07bff'),
    category: themeColor('--ui-orange', '#f5a623'),
    task: themeColor('--ui-red', '#f0574f'),
    general: themeColor('--ui-text-secondary', '#8b93a3'),
    accent: themeColor('--ui-accent', '#4d9fff'),
    stroke: themeColor('--ui-stroke-secondary', 'rgba(128,128,128,0.35)'),
    strokeDim: themeColor('--ui-stroke-tertiary', 'rgba(128,128,128,0.18)'),
    ground: themeColor('--ui-bg-editor', '#0d0f14'),
    surface: themeColor('--ui-bg-elevated', '#151821')
  }
}

// ---------------------------------------------------------------------------
// Date helpers
// ---------------------------------------------------------------------------
const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December']
const formatTime = (date) => {
  if (!date) return ''
  return new Date(date).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' })
}
function relativeTime(ts) {
  if (!ts) return ''
  const t = new Date(ts).getTime()
  if (Number.isNaN(t)) return ''
  const diff = Date.now() - t
  const abs = Math.abs(diff)
  const future = diff < 0
  if (abs < 45e3) return future ? 'just now' : 'just now'
  const mins = Math.round(abs / 6e4)
  if (mins < 60) return future ? `in ${mins}m` : `${mins}m ago`
  const hrs = Math.round(mins / 60)
  if (hrs < 24) return future ? `in ${hrs}h` : `${hrs}h ago`
  const days = Math.round(hrs / 24)
  if (days < 7) return future ? `in ${days}d` : `${days}d ago`
  return new Date(ts).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
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
  background: linear-gradient(180deg, transparent, var(--ui-text-quaternary, rgba(128,128,128,0.12)), transparent);
  animation: abyss-scan 9s linear infinite;
}
.abyss-cursor {
  display: inline-block;
  width: 0.5em;
  height: 1.05em;
  vertical-align: text-bottom;
  margin-left: 2px;
  background: var(--ui-green, #55a583);
  animation: abyss-blink 1.1s step-end infinite;
}
.abyss-mono { font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, Consolas, monospace); }
`
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
    this.positions = []
    this._bgPattern = null

    this._computeLayout()
    this._generateBackground()
    this._bindEvents()
    this._render()
  }

  _computeLayout() {
    this.positions = forceLayout(this.nodes, this.edges, 260, this.canvas.width, this.canvas.height)
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
    const c = palette()
    for (let i = 0; i < dithered.length; i++) {
      const idx = i * 4
      const v = dithered[i]
      data[idx] = v; data[idx + 1] = v; data[idx + 2] = v
      data[idx + 3] = Math.min(v, 10)
    }
    octx.putImageData(imageData, 0, 0)
    this._bgPattern = octx.createPattern(off, 'repeat')
    void c
  }

  _bindEvents() {
    const c = this.canvas
    c.addEventListener('mousedown', (e) => {
      const rect = c.getBoundingClientRect()
      const x = (e.clientX - rect.left) / this.scale - this.offsetX
      const y = (e.clientY - rect.top) / this.scale - this.offsetY
      const hit = this._hitTest(x, y)
      if (hit) { this.draggedNode = hit; this.selectedNode = hit }
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

  _withAlpha(hex, alpha) {
    // Accepts #rrggbb or rgb()/color-mix strings; falls back to translucent gray.
    if (/^#([0-9a-fA-F]{6})$/.test(hex)) {
      const r = parseInt(hex.slice(1, 3), 16)
      const g = parseInt(hex.slice(3, 5), 16)
      const b = parseInt(hex.slice(5, 7), 16)
      return `rgba(${r}, ${g}, ${b}, ${alpha})`
    }
    return hex
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
    const w = this.canvas.width, h = this.canvas.height
    ctx.clearRect(0, 0, w, h)
    const p = palette()
    if (this._bgPattern) {
      ctx.fillStyle = this._bgPattern
      ctx.fillRect(0, 0, w, h)
    }
    if (!this.nodes.length) return
    ctx.save()
    ctx.setTransform(this.scale, 0, 0, this.scale, this.offsetX, this.offsetY)
    // Edges — phosphor-dim lines
    for (const edge of this.edges) {
      const si = this.nodes.findIndex(n => n.id === edge.source)
      const ti = this.nodes.findIndex(n => n.id === edge.target)
      if (si === -1 || ti === -1 || !this.positions[si] || !this.positions[ti]) continue
      ctx.lineWidth = Math.min(2.5, 0.5 + (edge.weight || 1) * 0.25) / this.scale
      ctx.setLineDash(edge.type === 'reference' ? [3 / this.scale, 3 / this.scale] : [])
      ctx.strokeStyle = edge.type === 'reference' ? this._withAlpha(p.accent, 0.55) : p.strokeDim
      ctx.beginPath()
      ctx.moveTo(this.positions[si].x, this.positions[si].y)
      ctx.lineTo(this.positions[ti].x, this.positions[ti].y)
      ctx.stroke()
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
      ctx.strokeStyle = selected ? p.accent : this._withAlpha(color, 0.6)
      ctx.lineWidth = selected ? 2 / this.scale : 1 / this.scale
      ctx.stroke()
    }
    // Labels — Obsidian-style: show the node's name on hover/selection so an
    // opaque id (tool:web_search, memory:…) becomes readable.
    const labelNode = this.selectedNode ?? this.hoveredNode
    if (labelNode) {
      const idx = this.nodes.findIndex(n => n.id === labelNode.id)
      const pos = this.positions[idx]
      if (pos) {
        const r = this._radius(labelNode)
        const label = labelNode.label || labelNode.id
        const color = this._color(labelNode)
        ctx.save()
        ctx.font = `500 ${Math.max(10, 11 / this.scale)}px ui-sans-serif, system-ui, sans-serif`
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
        ctx.fillStyle = p.surface || '#151821'
        ctx.beginPath()
        ctx.roundRect(bx, by, boxW, boxH, 4)
        ctx.fill()
        ctx.globalAlpha = 1
        ctx.fillStyle = color
        ctx.fillText(label, bx + pad, by + boxH / 2)
        ctx.restore()
      }
    }
    ctx.restore()
  }
}

// ==================== COMPONENTS ====================

// --- Masthead + status strip (health at a glance) ---
function StatusStrip({ ctx }) {
  const [stats, setStats] = useState(null)
  const [status, setStatus] = useState(null)
  const [loading, setLoading] = useState(true)

  const fetchAll = useCallback(async () => {
    if (!ctx) return
    setLoading(true)
    try {
      // Real totals come from /status (signals_open + severity breakdown).
      // Do NOT derive them by sampling /signals?limit=50 — that undercounted
      // 800+ open signals as "50 SIG / 43 critical".
      const [s, st] = await Promise.all([
        ctx.rest('/stats', { method: 'GET', timeoutMs: 5000 }),
        ctx.rest('/status', { method: 'GET', timeoutMs: 5000 })
      ])
      setStats(s || null)
      setStatus(st && typeof st === 'object' ? st : null)
    } catch (e) {
      console.error('abyss: status fetch failed', e)
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

  const healthScore = status?.score
  const healthTone = (status?.level === 'critical') ? 'text-(--ui-red)'
    : (status?.level === 'degraded' || status?.level === 'fair') ? 'text-(--ui-yellow)'
    : 'text-(--ui-green)'

  const items = [
    { label: 'ACT', value: stats?.total_activities ?? 0, tone: 'text-(--ui-text-primary)' },
    { label: 'HLTH', value: healthScore ?? '—', tone: healthScore != null ? healthTone : 'text-(--ui-text-tertiary)' },
    { label: 'INC', value: status?.incidents_open ?? 0, tone: (status?.incidents_open ?? 0) > 0 ? 'text-(--ui-yellow)' : 'text-(--ui-text-primary)' },
    { label: 'CRN', value: stats?.cron_jobs ?? 0, tone: 'text-(--ui-text-primary)' },
    { label: 'CAT', value: stats?.categories ? Object.keys(stats.categories).length : 0, tone: 'text-(--ui-text-primary)' },
    { label: 'SIG', value: openSignals, tone: criticals > 0 ? 'text-(--ui-red)' : openSignals > 0 ? 'text-(--ui-yellow)' : 'text-(--ui-green)' }
  ]

  if (loading) {
    return jsx('div', {
      className: 'px-3 py-2 border-b border-(--ui-stroke-tertiary)',
      children: jsx('div', { className: 'h-7 w-full bg-(--ui-bg-tertiary) rounded animate-pulse' })
    })
  }

  return jsxs('div', {
    className: 'px-3 py-1.5 border-b border-(--ui-stroke-tertiary) flex items-center gap-4 flex-wrap',
    children: [
      jsx('span', { className: 'text-xs text-(--ui-text-tertiary) abyss-mono select-none', children: 'live:' }),
      items.map(item =>
        jsxs('span', {
          key: item.label,
          className: 'flex items-baseline gap-1',
          children: [
            jsx('span', { className: cn('text-xs font-semibold abyss-mono tabular-nums', item.tone), children: item.value }),
            jsx('span', { className: 'text-[0.65rem] uppercase tracking-widest text-(--ui-text-quaternary)', children: item.label })
          ]
        })
      ),
      jsxs('span', {
        className: 'ml-auto flex items-center gap-1.5 text-xs text-(--ui-text-tertiary) abyss-mono',
        children: [
          jsx('span', {
            className: 'inline-block h-1.5 w-1.5 rounded-full',
            style: { backgroundColor: criticals > 0 ? 'var(--ui-red)' : openSignals > 0 ? 'var(--ui-yellow)' : 'var(--ui-green)' }
          }),
          criticals > 0 ? `${criticals} critical` : openSignals > 0 ? `${openSignals} open` : 'all clear'
        ]
      })
    ]
  })
}

function Masthead() {
  return jsxs('div', {
    className: 'px-3 pt-2 pb-1.5 border-b border-(--ui-stroke-tertiary) relative overflow-hidden',
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
              jsx('span', { className: 'text-xs text-(--ui-text-tertiary) abyss-mono', children: '$ ./abyss --observe --local' }),
              jsx(Button, {
                variant: 'ghost',
                size: 'sm',
                className: 'h-6 w-6 px-0',
                onClick: () => { try { host.navigate('/') } catch { } },
                title: 'Close Abyss dashboard',
                children: jsx(Codicon, { name: 'close', className: 'text-(--ui-text-tertiary)' })
              })
            ]
          })
        ]
      }),
      jsx('div', { className: 'mt-0.5 text-[0.65rem] text-(--ui-text-quaternary) abyss-mono truncate', children: 'self-diagnostics · signal detection · incident clustering · hermès brain' })
    ]
  })
}

// --- Activity Feed ---
function ActivityFeed({ ctx }) {
  const [activities, setActivities] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [filter, setFilter] = useState('all')

  const fetchActivities = useCallback(async () => {
    if (!ctx) return
    setLoading(true)
    setError(null)
    try {
      const q = filter !== 'all' ? `?limit=50&category=${encodeURIComponent(filter)}` : '?limit=50'
      const data = await ctx.rest(`/activity${q}`, { method: 'GET', timeoutMs: 5000 })
      setActivities(Array.isArray(data) ? data : [])
    } catch (e) {
      console.error('Failed to fetch activity:', e)
      setError(String(e?.message || e))
      setActivities([])
    } finally {
      setLoading(false)
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

  if (loading) {
    return jsx('div', {
      className: 'p-3',
      children: jsxs('div', { className: 'space-y-2', children: Array.from({ length: 5 }).map((_, i) =>
        jsx('div', { key: i, className: 'h-12 w-full bg-(--ui-bg-tertiary) rounded animate-pulse' })
      ) })
    })
  }

  if (error) {
    return jsx(ErrorState, {
      title: 'Activity unavailable',
      description: error,
      children: jsx(Button, { variant: 'secondary', size: 'sm', onClick: fetchActivities, children: 'Retry' })
    })
  }

  if (activities.length === 0) {
    return jsx(EmptyState, {
      title: 'No activity yet',
      description: 'Activity entries will appear here as you work.'
    })
  }

  return jsxs('div', {
    className: 'flex h-full flex-col',
    children: [
      jsx('div', {
        className: 'flex gap-1 px-3 py-2 border-b border-(--ui-stroke-tertiary) overflow-x-auto',
        children: categories.map(cat =>
          jsx(Button, {
            key: cat,
            variant: filter === cat ? 'default' : 'ghost',
            size: 'sm',
            onClick: () => setFilter(cat),
            className: 'text-xs h-7 whitespace-nowrap abyss-mono',
            children: cat === 'all' ? 'all' : cat
          })
        )
      }),
      jsx('div', {
        className: 'flex-1 overflow-y-auto',
        children: jsxs('div', {
          className: 'divide-y divide-(--ui-stroke-tertiary)',
          children: activities.map(entry =>
            jsxs('div', {
              key: entry.id,
              className: 'px-3 py-2 flex items-start gap-2.5 hover:bg-(--ui-bg-tertiary)',
              children: [
                jsx('span', {
                  className: 'text-[0.65rem] tabular-nums abyss-mono mt-0.5 select-none',
                  style: categoryStyle(entry.category),
                  children: '▸'
                }),
                jsxs('div', {
                  className: 'flex-1 min-w-0',
                  children: [
                    jsxs('div', {
                      className: 'flex items-center gap-2',
                      children: [
                        jsx('span', { className: 'text-sm font-medium text-(--ui-text-primary) truncate', children: entry.action }),
                        jsx('span', { className: 'text-[0.65rem] tabular-nums text-(--ui-text-quaternary) abyss-mono whitespace-nowrap', children: relativeTime(entry.timestamp) })
                      ]
                    }),
                    entry.description && jsx('div', {
                      className: 'text-xs text-(--ui-text-secondary) mt-0.5 truncate',
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
                          className: 'text-[0.65rem] text-(--ui-text-quaternary) abyss-mono',
                          children: `sid ${entry.session_id.slice(0, 8)}`
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
function CalendarView({ ctx }) {
  const [currentWeek, setCurrentWeek] = useState(new Date())
  const [tasks, setTasks] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const weekStart = getWeekStart(currentWeek)
  const weekEnd = addDays(weekStart, 6)

  const fetchTasks = useCallback(async () => {
    if (!ctx) return
    setLoading(true)
    setError(null)
    try {
      const startISO = weekStart.toISOString()
      const endISO = addDays(weekEnd, 1).toISOString()
      const data = await ctx.rest(`/calendar?start=${encodeURIComponent(startISO)}&end=${encodeURIComponent(endISO)}`, {
        method: 'GET',
        timeoutMs: 5000
      })
      setTasks(Array.isArray(data) ? data : [])
    } catch (e) {
      console.error('Failed to fetch calendar:', e)
      setError(String(e?.message || e))
      setTasks([])
    } finally {
      setLoading(false)
    }
  }, [ctx, currentWeek])

  useEffect(() => { fetchTasks() }, [fetchTasks])

  const weekDays = getWeekDays(currentWeek)

  const tasksByDay = useMemo(() => {
    const grouped = {}
    weekDays.forEach(day => { grouped[formatDateISO(day)] = [] })
    tasks.forEach(task => {
      const taskDate = task.timestamp || task.next_run
      if (!taskDate) return
      const taskDay = new Date(taskDate)
      if (taskDay >= weekStart && taskDay <= weekEnd) {
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

  if (loading) {
    return jsx('div', { className: 'p-3', children: jsx('div', { className: 'h-12 w-full bg-(--ui-bg-tertiary) rounded animate-pulse' }) })
  }

  if (error) {
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
            children: jsx(Codicon, { name: 'chevron-left' })
          }),
          jsxs('div', {
            className: 'flex items-center gap-2 abyss-mono',
            children: [
              jsx('span', { className: 'text-sm font-medium uppercase tracking-wider text-(--ui-text-primary)', children: MONTHS[weekStart.getMonth()] }),
              jsx('span', { className: 'text-xs text-(--ui-text-secondary)', children: `${weekStart.getDate()} – ${weekEnd.getDate()}, ${weekStart.getFullYear()}` })
            ]
          }),
          jsx(Button, {
            variant: 'ghost', size: 'sm',
            onClick: () => setCurrentWeek(addDays(currentWeek, 7)),
            title: 'Next week',
            children: jsx(Codicon, { name: 'chevron-right' })
          })
        ]
      }),
      jsx('div', {
        className: 'px-2 py-1 border-b border-(--ui-stroke-tertiary)',
        children: jsx(Button, {
          variant: 'ghost', size: 'sm', className: 'text-xs abyss-mono',
          onClick: () => setCurrentWeek(new Date()),
          children: 'today'
        })
      }),
      jsx('div', {
        className: 'flex-1 overflow-auto',
        children: jsxs('div', {
          className: 'grid gap-px bg-(--ui-stroke-tertiary) text-xs',
          style: { gridTemplateColumns: 'repeat(7, minmax(0, 1fr))' },
          children: [
            WEEKDAYS.map(day =>
              jsx('div', {
                key: day,
                className: 'bg-(--ui-bg-quaternary) px-1 py-1 text-center font-medium text-(--ui-text-tertiary) uppercase tracking-wider text-[0.6rem]',
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
                  'bg-(--ui-bg-elevated) p-1',
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
                        children: day.getDate()
                      }),
                      dayTasks.length > 0 && jsx(Badge, {
                        variant: 'outline', size: 'xs', className: 'text-[0.6rem] h-4 min-w-4 justify-center rounded-full',
                        children: dayTasks.length
                      })
                    ]
                  }),
                  dayTasks.length > 0 ? jsxs('div', {
                    className: 'mt-1 space-y-0.5',
                    children: dayTasks.slice(0, 2).map(task => {
                      const tone = taskTone[task.status] || taskTone[task.category] || taskTone.general
                      return jsxs('div', {
                        key: task.id,
                        className: 'flex items-center gap-1 min-w-0',
                        children: [
                          jsx('span', { className: 'inline-block h-1.5 w-1.5 rounded-full shrink-0', style: { backgroundColor: tone }, children: '' }),
                          jsx('span', { className: 'text-[0.65rem] truncate', style: { color: tone }, children: task.title || task.action || '' })
                        ]
                      })
                    })
                  }) : null,
                  dayTasks.length > 2 && jsx('div', {
                    className: 'text-[0.6rem] text-(--ui-text-tertiary) mt-0.5 abyss-mono',
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
function GlobalSearch({ ctx }) {
  const [query, setQuery] = useState('')
  const [results, setResults] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [selectedSources, setSelectedSources] = useState({
    memory: true, sessions: true, kanban: true, activity: true
  })

  const fetchResults = useCallback(async (q) => {
    if (!q || q.length < 2 || !ctx) {
      setResults([])
      setError(null)
      return
    }
    setLoading(true)
    setError(null)
    try {
      const data = await ctx.rest(`/search?q=${encodeURIComponent(q)}&limit=50`, {
        method: 'GET',
        timeoutMs: 5000
      })
      setResults(Array.isArray(data) ? data : [])
    } catch (e) {
      console.error('Search failed:', e)
      setError(String(e?.message || e))
      setResults([])
    } finally {
      setLoading(false)
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
                className: 'text-xs h-6 abyss-mono',
                children: sourceLabels[source]
              })
            )
          })
        ]
      }),
      query.length >= 2 && jsx('div', {
        className: 'px-3 py-1 text-[0.65rem] text-(--ui-text-tertiary) abyss-mono border-b border-(--ui-stroke-tertiary)',
        children: `${filteredResults.length} result${filteredResults.length !== 1 ? 's' : ''} for “${query}”`
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
          title: query.length < 2 ? 'Search Abyss' : 'No results found',
          description: query.length < 2
            ? 'Type to search across memories, sessions, tasks, and activity.'
            : `No matches for “${query}” in the selected sources.`
        }) : jsxs('div', {
          className: 'divide-y divide-(--ui-stroke-tertiary)',
          children: filteredResults.map((result, idx) => {
            const srcStyle = sourceStyle[result.source] || { color: 'var(--ui-text-secondary)' }
            return jsxs('div', {
              key: `${result.source}-${result.id}-${idx}`,
              className: 'px-3 py-2 hover:bg-(--ui-bg-tertiary)',
              children: [
                jsxs('div', {
                  className: 'flex items-center gap-2',
                  children: [
                    jsx('span', { className: 'text-[0.65rem] abyss-mono uppercase tracking-wider select-none', style: srcStyle, children: result.source }),
                    result.relevance && jsx(Badge, { variant: 'outline', size: 'xs', className: 'abyss-mono tabular-nums', children: `${Math.round(result.relevance * 100)}%` }),
                    result.timestamp && jsx('span', { className: 'text-[0.65rem] text-(--ui-text-quaternary) abyss-mono ml-auto', children: relativeTime(result.timestamp) })
                  ]
                }),
                jsx('div', { className: 'font-medium text-sm mt-0.5 truncate text-(--ui-text-primary)', children: result.title }),
                result.description && jsx('div', {
                  className: 'text-xs text-(--ui-text-secondary) mt-1 line-clamp-2',
                  children: result.description
                }),
                (result.category || result.status) && jsx('div', {
                  className: 'flex gap-1.5 mt-1 flex-wrap',
                  children: [
                    result.category && jsx(Badge, { variant: 'muted', size: 'xs', children: result.category }),
                    result.status && jsx(Badge, { variant: 'outline', size: 'xs', children: result.status })
                  ]
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
  tool_call_end: 'checkmark-circle',
  llm_call: 'sparkles',
  llm_call_end: 'bulb',
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

function TracingView({ ctx }) {
  const [sessions, setSessions] = useState([])
  const [selectedSession, setSelectedSession] = useState(null)
  const [traces, setTraces] = useState([])
  const [loading, setLoading] = useState(true)
  const [loadingSessions, setLoadingSessions] = useState(true)
  const [error, setError] = useState(null)

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

  const fetchTraces = useCallback(async () => {
    if (!ctx || !selectedSession) return
    setLoading(true)
    setError(null)
    try {
      const data = await ctx.rest(`/trace?session_id=${encodeURIComponent(selectedSession)}&limit=200`, {
        method: 'GET',
        timeoutMs: 5000
      })
      setTraces(Array.isArray(data) ? data : [])
    } catch (e) {
      console.error('Failed to fetch traces:', e)
      setError(String(e?.message || e))
      setTraces([])
    } finally {
      setLoading(false)
    }
  }, [ctx, selectedSession])

  useEffect(() => { fetchSessions() }, [fetchSessions])
  useEffect(() => { if (selectedSession) { fetchTraces() } }, [selectedSession, fetchTraces])

  if (loadingSessions) {
    return jsx('div', { className: 'p-3', children: jsx('div', { className: 'h-8 w-full bg-(--ui-bg-tertiary) rounded animate-pulse' }) })
  }

  if (!sessions || sessions.length === 0) {
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
              jsx(Codicon, { name: 'history-timestamp', className: 'text-(--ui-text-secondary)' }),
              jsx('span', { className: 'text-sm font-medium uppercase tracking-wider text-(--ui-text-primary) abyss-mono', children: 'trace' })
            ]
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
              children: sessions.map(s =>
                jsx(SelectItem, {
                  key: s.session_id,
                  value: s.session_id,
                  children: `${s.session_id?.slice(0, 8) || 'unknown'}… (${s.activity_count || 0} events)`
                })
              )
            })
          ]
        })
      }),
      jsx('div', {
        className: 'flex-1 overflow-y-auto',
        children: loading ? jsx('div', {
          className: 'p-3 flex items-center justify-center',
          children: jsx(GlyphSpinner, { ariaLabel: 'Loading trace', className: 'text-(--ui-text-tertiary)' })
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
              const icon = EVENT_ICONS[t.event_type] || 'dot'
              const tone = EVENT_TONES[t.event_type] || 'var(--ui-text-secondary)'
              return jsxs('div', {
                key: t.id,
                className: 'relative ml-3 pl-3 pb-2.5 last:mb-0',
                children: [
                  jsx('div', {
                    className: 'absolute flex items-center justify-center',
                    style: { left: -19, top: 0, color: tone },
                    children: jsx(Codicon, { name: icon, className: 'text-sm' })
                  }),
                  jsx('div', {
                    className: 'flex items-center gap-2 mb-0.5',
                    children: [
                      jsx(Badge, { variant: 'outline', size: 'xs', className: 'text-[0.6rem] uppercase tracking-wider abyss-mono', children: (t.event_type || '').replace(/_/g, ' ') }),
                      t.timestamp && jsx('span', { className: 'text-[0.65rem] text-(--ui-text-quaternary) abyss-mono tabular-nums', children: relativeTime(t.timestamp) })
                    ]
                  }),
                  data && data.tool && jsx('div', { className: 'text-sm font-medium text-(--ui-text-primary) truncate', children: data.tool }),
                  data && data.model && jsx('div', { className: 'text-xs text-(--ui-text-secondary) truncate', children: data.model }),
                  data && data.result_preview && jsx('div', {
                    className: 'text-xs text-(--ui-text-secondary) mt-0.5 truncate',
                    style: { maxWidth: '90%' },
                    children: data.result_preview
                  }),
                  data && data.source && jsx('div', {
                    className: 'text-[0.65rem] text-(--ui-text-quaternary) mt-0.5 abyss-mono',
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
function BrainGraph({ ctx }) {
  const canvasRef = useRef(null)
  const containerRef = useRef(null)
  const [loading, setLoading] = useState(true)
  const [nodeCount, setNodeCount] = useState(0)
  const [edgeCount, setEdgeCount] = useState(0)
  const [error, setError] = useState(null)
  const graphRef = useRef(null)
  const dataRef = useRef(null)
  const roRef = useRef(null)

  const fetchGraphData = useCallback(async () => {
    if (!ctx) return
    setLoading(true)
    setError(null)
    try {
      const data = await ctx.rest('/graph?limit=300', { method: 'GET', timeoutMs: 10000 })
      dataRef.current = data || null
      setNodeCount(data?.nodes?.length || 0)
      setEdgeCount(data?.edges?.length || 0)
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
    // renderer would draw into a tiny buffer (blank/distorted graph).
    canvas.width = Math.max(1, container.clientWidth)
    canvas.height = Math.max(1, container.clientHeight)
    try {
      if (!graphRef.current) {
        graphRef.current = new PhosphorGraphRenderer(canvas, data)
      } else {
        graphRef.current.setData(data)
      }
    } catch (e) {
      console.error('[abyss-brain] renderer construction THREW', e)
    }
  }, [loading, error, nodeCount])

  useEffect(() => {
    if (!containerRef.current) return
    roRef.current = new ResizeObserver(() => {
      const canvas = canvasRef.current
      const container = containerRef.current
      const g = graphRef.current
      if (!canvas || !container || !g) return
      const w = container.clientWidth
      const h = container.clientHeight
      if (w > 0 && h > 0 && (canvas.width !== w || canvas.height !== h)) {
        canvas.width = w
        canvas.height = h
        // Layout was computed for the old size — recompute so nodes stay inside
        // the (possibly much larger) canvas instead of piling into one corner.
        g._computeLayout()
      }
      g._render()
    })
    roRef.current.observe(containerRef.current)
    return () => roRef.current?.disconnect()
  }, [])

  const legend = [
    { style: { backgroundColor: 'var(--ui-blue)' }, label: 'Sessions' },
    { style: { backgroundColor: 'var(--ui-green)' }, label: 'Tools' },
    { style: { backgroundColor: 'var(--ui-purple)' }, label: 'Memories' },
    { style: { backgroundColor: 'var(--ui-orange)' }, label: 'Categories' },
    { style: { backgroundColor: 'var(--ui-red)' }, label: 'Tasks' }
  ]

  return jsxs('div', {
    className: 'flex h-full flex-col',
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between px-3 py-2 border-b border-(--ui-stroke-tertiary)',
        children: [
          jsxs('div', {
            className: 'flex items-center gap-2',
            children: [
              jsx(Codicon, { name: 'network-flow-diagram', className: 'text-(--ui-text-secondary)' }),
              jsx('span', { className: 'text-sm font-medium uppercase tracking-wider text-(--ui-text-primary) abyss-mono', children: 'hermès brain' })
            ]
          }),
          jsx('div', {
            className: 'flex items-center gap-2 text-[0.65rem] text-(--ui-text-tertiary) abyss-mono tabular-nums',
            children: [
              jsx(Badge, { variant: 'outline', size: 'xs', children: `${nodeCount} nodes` }),
              jsx(Badge, { variant: 'outline', size: 'xs', children: `${edgeCount} edges` }),
              jsx(Button, {
                variant: 'ghost', size: 'sm',
                onClick: fetchGraphData,
                title: 'Refresh graph',
                children: jsx(Codicon, { name: 'refresh' })
              })
            ]
          })
        ]
      }),
      jsx('div', {
        ref: containerRef,
        className: 'flex-1 relative m-1 rounded-lg overflow-hidden bg-(--ui-bg-editor)',
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
          role: 'img',
          'aria-label': `Hermes brain graph: ${nodeCount} nodes, ${edgeCount} edges`
        })
      }),
      !loading && nodeCount > 0 && jsx('div', {
        className: 'px-3 py-1.5 border-t border-(--ui-stroke-tertiary) flex gap-4 text-[0.65rem] flex-wrap',
        children: legend.map(item =>
          jsxs('div', {
            key: item.label,
            className: 'flex items-center gap-1',
            children: [
              jsx('span', { className: 'inline-block h-1.5 w-1.5 rounded-full', style: item.style, children: '' }),
              jsx('span', { className: 'text-(--ui-text-tertiary)', children: item.label })
            ]
          })
        )
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

function SignalsIncidentsView({ ctx }) {
  const [activeTab, setActiveTab] = useState('signals')
  const [signals, setSignals] = useState([])
  const [incidents, setIncidents] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [busyId, setBusyId] = useState(null)
  const [clustering, setClustering] = useState(false)
  const [clusterError, setClusterError] = useState(null)

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
      setSignals([])
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
      setIncidents([])
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
    if (activeTab === 'signals') await fetchSignals()
    else await fetchIncidents()
  }, [activeTab, fetchSignals, fetchIncidents])

  useEffect(() => { fetchData() }, [fetchData])

  const runAction = useCallback(async (kind, id, action) => {
    if (!ctx || busyId) return
    setBusyId(`${kind}:${id}:${action}`)
    try {
      await ctx.rest(`/${kind}/${id}/${action}`, { method: 'POST', timeoutMs: 5000 })
      if (kind === 'signals') await fetchSignals()
      else await fetchIncidents()
    } catch (e) {
      console.error(`Failed to ${action} ${kind.slice(0, -1)}:`, e)
    } finally {
      setBusyId(null)
    }
  }, [ctx, busyId, fetchSignals, fetchIncidents])

  // Agent-powered resolve: dispatch a free-Nous Hermes agent to diagnose + fix
  // the issue on the backend. The backend marks it resolved only from the
  // agent's report — the button no longer just makes the row disappear.
  const resolveAgent = useCallback(async (kind, id) => {
    if (!ctx || busyId) return
    setBusyId(`${kind}:${id}:resolve-agent`)
    try {
      await ctx.rest(`/${kind}/${id}/resolve-agent`, { method: 'POST', timeoutMs: 8000 })
      if (kind === 'signals') await fetchSignals()
      else await fetchIncidents()
    } catch (e) {
      console.error(`Failed to dispatch resolver for ${kind.slice(0, -1)}:`, e)
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
    return jsx('div', { className: 'p-3', children: jsx('div', { className: 'h-12 w-full bg-(--ui-bg-tertiary) rounded animate-pulse' }) })
  }

  if (error && signals.length === 0 && incidents.length === 0) {
    return jsx(ErrorState, {
      title: 'Signals unavailable',
      description: error,
      children: jsx(Button, { variant: 'secondary', size: 'sm', onClick: fetchData, children: 'Retry' })
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
              jsx(Codicon, { name: 'warning', className: 'text-(--ui-text-secondary)' }),
              jsx('span', { className: 'text-sm font-medium uppercase tracking-wider text-(--ui-text-primary) abyss-mono', children: 'watch' })
            ]
          }),
          jsxs('div', {
            className: 'flex items-center gap-2',
            children: [
              clusterError && jsx('span', { className: 'text-[0.65rem] text-(--ui-red) abyss-mono', children: clusterError }),
              jsx(Button, {
                variant: 'ghost', size: 'sm',
                disabled: clustering,
                onClick: clusterIncidents,
                title: clustering ? 'Clustering incidents…' : 'Run incident clustering',
                children: jsx(Codicon, { name: 'refresh' })
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
            className: 'text-xs h-7 abyss-mono',
            children: `signals (${signals.length})`
          }),
          jsx(Button, {
            variant: activeTab === 'incidents' ? 'default' : 'ghost',
            size: 'sm',
            onClick: () => setActiveTab('incidents'),
            className: 'text-xs h-7 abyss-mono',
            children: `incidents (${incidents.length})`
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
            className: 'divide-y divide-(--ui-stroke-tertiary)',
            children: signals.map(s => {
              const resolved = s.resolved
              const acknowledged = s.acknowledged
              const busy = busyId === `signals:${s.id}:${resolved ? 'acknowledge' : 'resolve'}`
              return jsxs('div', {
                key: s.id,
                className: 'px-3 py-2 hover:bg-(--ui-bg-tertiary)',
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
                              jsx('span', { className: 'text-[0.65rem] text-(--ui-text-quaternary) abyss-mono', children: relativeTime(s.timestamp) }),
                              resolved && jsx(Badge, { variant: 'default', size: 'xs', children: 'resolved' }),
                              acknowledged && !resolved && jsx(Badge, { variant: 'muted', size: 'xs', children: 'acknowledged' }),
                              s.resolution_status === 'running' && jsx(Badge, { variant: 'warn', size: 'xs', children: 'resolving…' }),
                              s.resolution_status === 'failed' && jsx(Badge, { variant: 'destructive', size: 'xs', children: 'fix failed' })
                            ]
                          }),
                          jsx('div', { className: 'font-medium text-sm text-(--ui-text-primary)', children: s.label }),
                          jsx('div', { className: 'text-xs text-(--ui-text-secondary) mt-0.5', children: s.description }),
                          (s.resolution_status === 'succeeded' || s.resolution_status === 'failed') && s.resolution_note && jsx('div', {
                            className: 'text-[0.65rem] text-(--ui-text-tertiary) mt-1 abyss-mono line-clamp-2',
                            children: `fix: ${s.resolution_note}`
                          }),
                          (s.session_id || s.source) && jsx('div', {
                            className: 'text-[0.65rem] text-(--ui-text-quaternary) mt-1 abyss-mono',
                            children: `session: ${s.session_id?.slice(0, 8) || '—'}  source: ${s.source || '—'}`
                          }),
                          !resolved && jsxs('div', {
                            className: 'flex gap-1.5 mt-1.5',
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
                                title: 'Dispatch a free-Nous agent to diagnose and fix',
                                children: s.resolution_status === 'running' ? 'resolving…' : s.resolution_status === 'failed' ? 'retry fix' : 'resolve'
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
            className: 'divide-y divide-(--ui-stroke-tertiary)',
            children: incidents.map(i => {
              const busy = busyId === `incidents:${i.id}:${i.status === 'closed' ? 'reopen' : 'resolve-agent'}`
              const isOpen = i.status === 'open'
              const isAcked = i.status === 'acknowledged'
              const isResolved = i.status === 'resolved'
              const isClosed = i.status === 'closed'
              const showResolve = isOpen || isAcked
              return jsxs('div', {
                key: i.id,
                className: 'px-3 py-2.5 hover:bg-(--ui-bg-tertiary)',
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
                  jsx('div', { className: 'font-medium text-sm text-(--ui-text-primary)', children: i.title }),
                  jsx('div', { className: 'text-xs text-(--ui-text-secondary) mt-1', children: i.description }),
                  (i.resolution_status === 'succeeded' || i.resolution_status === 'failed') && i.resolution_note && jsx('div', {
                    className: 'text-[0.65rem] text-(--ui-text-tertiary) mt-1 abyss-mono line-clamp-2',
                    children: `fix: ${i.resolution_note}`
                  }),
                  jsx('div', {
                    className: 'flex gap-4 mt-1.5 text-[0.65rem] text-(--ui-text-quaternary) abyss-mono tabular-nums',
                    children: [
                      jsx('span', { children: `signals: ${i.signal_count}` }),
                      jsx('span', { children: `pattern: ${i.pattern || '—'}` }),
                      jsx('span', { children: `created: ${relativeTime(i.created_at)}` })
                    ]
                  }),
                  jsxs('div', {
                    className: 'flex gap-1.5 mt-2',
                    children: [
                      showResolve && jsx(Button, {
                        variant: 'secondary', size: 'xs',
                        disabled: busy || i.resolution_status === 'running',
                        onClick: () => resolveAgent('incidents', i.id),
                        title: 'Dispatch a free-Nous agent to diagnose and fix',
                        children: i.resolution_status === 'running' ? 'resolving…' : i.resolution_status === 'failed' ? 'retry fix' : 'resolve'
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
  // Benchmark (Abyss Bench Layer 1 probe suite)
  const [benchmark, setBenchmark] = useState(null)
  const [benchmarkRunning, setBenchmarkRunning] = useState(false)
  const [benchmarkError, setBenchmarkError] = useState(null)

  const fetchAll = useCallback(async () => {
    if (!ctx) return
    setLoading(true)
    try {
      const [h, t, f] = await Promise.all([
        ctx.rest('/health', { method: 'GET', timeoutMs: 5000 }),
        ctx.rest('/trends?days=7&bucket=day', { method: 'GET', timeoutMs: 5000 }),
        ctx.rest('/failures?limit=8', { method: 'GET', timeoutMs: 5000 })
      ])
      setHealth(h && typeof h === 'object' ? h : null)
      setTrends(t && typeof t === 'object' ? t : null)
      setFailures(f && typeof f === 'object' ? f : null)
    } catch (e) {
      console.error('abyss: health fetch failed', e)
    } finally {
      setLoading(false)
    }
  }, [ctx])

  useEffect(() => { fetchAll() }, [fetchAll])

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

  // NOTE: hooks must be declared BEFORE any early return — React counts hook
  // calls per render, and a hook that only runs on the loaded-data render
  // (skipped during `loading`) throws #310 "Rendered more hooks than during
  // the previous render" and crashes the whole dashboard page.
  const trendMax = useMemo(() => {
    if (!trends) return 1
    return Math.max(1, ...(trends.activity || []), ...(trends.errors || []))
  }, [trends])

  if (loading) {
    return jsx('div', { className: 'p-3', children: jsx('div', { className: 'h-12 w-full bg-(--ui-bg-tertiary) rounded animate-pulse' }) })
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
  const levelBg = level === 'critical' ? 'bg-(--ui-red)'
    : level === 'degraded' || level === 'fair' ? 'bg-(--ui-yellow)'
    : 'bg-(--ui-green)'

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

  return jsxs('div', {
    className: 'flex h-full flex-col overflow-auto p-4 gap-4',
    children: [
      // Score gauge
      jsxs('div', {
        className: 'flex items-center gap-4 p-4 rounded-lg border border-(--ui-stroke-tertiary)',
        children: [
          jsxs('div', {
            className: 'flex flex-col items-center justify-center',
            children: [
              jsx('span', { className: cn('text-4xl font-bold abyss-mono tabular-nums', levelTone), children: score }),
              jsx('span', { className: 'text-[0.65rem] uppercase tracking-widest text-(--ui-text-quaternary)', children: '/100' })
            ]
          }),
          jsx('span', {
            className: cn('inline-block h-2.5 w-2.5 rounded-full', levelBg),
            children: ''
          }),
          jsxs('div', {
            className: 'flex-1',
            children: [
              jsx('div', { className: 'text-sm font-medium capitalize text-(--ui-text-primary)', children: `${level} agent health` }),
              jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: `${counts.errors ?? 0} errors · ${counts.signals_open ?? 0} open signals · ${counts.incidents_open ?? 0} open incidents · ${counts.activity_24h ?? 0} actions/24h` })
            ]
          }),
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
      }),
      // Doctor — agent-powered diagnosis with approval-gated fixes
      (doctorPhase !== 'idle' || doctorReport) && jsxs('div', {
        className: 'p-4 rounded-lg border border-(--ui-stroke-tertiary)',
        children: [
          jsxs('div', {
            className: 'flex items-center justify-between mb-2',
            children: [
              jsx('div', { className: 'text-xs uppercase tracking-widest text-(--ui-text-quaternary) abyss-mono', children: 'doctor' }),
              jsx(Button, { variant: 'ghost', size: 'xs', onClick: dismissDoctor, children: 'dismiss' })
            ]
          }),
          doctorPhase === 'running' && jsxs('div', {
            className: 'flex items-center gap-2 text-sm text-(--ui-text-secondary)',
            children: [
              jsx(GlyphSpinner, { className: 'text-(--ui-accent)' }),
              jsx('span', { children: `doctor agent diagnosing… ${doctorReportId ? `(${doctorReportId})` : ''}` })
            ]
          }),
          doctorPhase === 'applying' && jsxs('div', {
            className: 'flex items-center gap-2 text-sm text-(--ui-text-secondary)',
            children: [
              jsx(GlyphSpinner, { className: 'text-(--ui-accent)' }),
              jsx('span', { children: 'applying approved fixes…' })
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
              jsx('div', { className: 'text-xs text-(--ui-text-secondary) mb-2', children: doctorReport.summary }),
              (doctorReport.findings || []).length > 0 && jsxs('div', {
                className: 'mb-2',
                children: [
                  jsx('div', { className: 'text-[0.65rem] uppercase tracking-widest text-(--ui-text-quaternary) mb-1', children: 'findings' }),
                  (doctorReport.findings || []).map((f, idx) => jsx('div', {
                    key: idx,
                    className: 'text-xs text-(--ui-text-secondary) mb-1 flex gap-1.5',
                    children: [
                      jsx('span', { className: 'text-(--ui-text-quaternary) abyss-mono', children: '▸' }),
                      jsx('span', { children: f.title })
                    ]
                  }))
                ]
              }),
              (doctorReport.proposed_fixes || []).length > 0 && doctorPhase === 'review' && jsxs('div', {
                className: 'mb-3',
                children: [
                  jsx('div', { className: 'text-[0.65rem] uppercase tracking-widest text-(--ui-text-quaternary) mb-1', children: 'proposed fixes' }),
                  (doctorReport.proposed_fixes || []).map((fx, idx) => jsx('div', {
                    key: fx.id || idx,
                    className: 'text-xs text-(--ui-text-secondary) mb-1.5 border border-(--ui-stroke-tertiary) rounded p-2',
                    children: [
                      jsx('div', {
                        className: 'font-medium text-(--ui-text-primary)',
                        children: `${fx.title}${fx.target_signals?.length || fx.target_incidents?.length ? `  → ${fx.target_signals?.length || 0} sig / ${fx.target_incidents?.length || 0} inc` : ''}`
                      }),
                      fx.action && jsx('div', { className: 'mt-0.5 text-(--ui-text-tertiary) line-clamp-2', children: fx.action })
                    ]
                  }))
                ]
              }),
              (doctorReport.fixes || []).length > 0 && doctorPhase === 'done' && jsxs('div', {
                className: 'mb-2',
                children: [
                  jsx('div', { className: 'text-[0.65rem] uppercase tracking-widest text-(--ui-text-quaternary) mb-1', children: 'applied' }),
                  (doctorReport.fixes || []).map((fx, idx) => jsx('div', {
                    key: fx.id || idx,
                    className: 'text-xs mb-1 flex gap-1.5',
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
        className: 'p-4 rounded-lg border border-(--ui-stroke-tertiary)',
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
                  jsx('span', { className: 'text-(--ui-text-secondary) shrink-0', children: p.id }),
                  jsx('span', { className: 'truncate text-(--ui-text-tertiary)', children: p.detail })
                ]
              }))
            ]
          })
        ]
      }),
      // Component breakdown bars
      jsxs('div', {
        className: 'p-4 rounded-lg border border-(--ui-stroke-tertiary)',
        children: [
          jsx('div', { className: 'text-xs uppercase tracking-widest text-(--ui-text-quaternary) mb-2', children: 'score breakdown' }),
          compRows.map(row =>
            jsxs('div', {
              key: row.label,
              className: 'flex items-center gap-2 mb-1.5',
              children: [
                jsx('span', { className: 'w-24 text-xs text-(--ui-text-secondary)', children: row.label }),
                jsx('div', { className: 'flex-1 h-2 rounded bg-(--ui-bg-tertiary) overflow-hidden', children:
                  jsx('div', {
                    className: cn('h-full rounded', row.value / row.max > 0.66 ? 'bg-(--ui-green)' : row.value / row.max > 0.33 ? 'bg-(--ui-yellow)' : 'bg-(--ui-red)'),
                    style: { width: `${Math.min(100, (row.value / row.max) * 100)}%` },
                    children: ''
                  })
                }),
                jsx('span', { className: 'w-8 text-right text-xs abyss-mono tabular-nums text-(--ui-text-tertiary)', children: row.value })
              ]
            })
          )
        ]
      }),
      // Trends sparkline bars
      trends && jsxs('div', {
        className: 'p-4 rounded-lg border border-(--ui-stroke-tertiary)',
        children: [
          jsx('div', { className: 'text-xs uppercase tracking-widest text-(--ui-text-quaternary) mb-2', children: '7-day activity' }),
          jsxs('div', {
            className: 'flex items-end gap-1 h-16',
            children: (trends.timestamps || []).map((ts, i) =>
              jsx('div', {
                key: ts + i,
                className: 'flex-1 flex flex-col justify-end gap-0.5',
                children: [
                  jsx('div', {
                    className: 'w-full rounded-sm bg-(--ui-red)',
                    style: { height: `${((trends.errors?.[i] || 0) / trendMax) * 100}%` },
                    children: ''
                  }),
                  jsx('div', {
                    className: 'w-full rounded-sm bg-(--ui-accent)',
                    style: { height: `${((trends.activity?.[i] || 0) / trendMax) * 100}%` },
                    children: ''
                  })
                ]
              })
            )
          }),
          jsxs('div', {
            className: 'flex gap-4 mt-2 text-[0.65rem] text-(--ui-text-quaternary)',
            children: [
              jsxs('span', { className: 'flex items-center gap-1', children: [
                jsx('span', { className: 'inline-block h-2 w-2 rounded-sm bg-(--ui-accent)', children: '' }),
                'activity'
              ] }),
              jsxs('span', { className: 'flex items-center gap-1', children: [
                jsx('span', { className: 'inline-block h-2 w-2 rounded-sm bg-(--ui-red)', children: '' }),
                'errors'
              ] })
            ]
          })
        ]
      }),
      // Failure taxonomy
      failures && jsxs('div', {
        className: 'p-4 rounded-lg border border-(--ui-stroke-tertiary)',
        children: [
          jsx('div', { className: 'text-xs uppercase tracking-widest text-(--ui-text-quaternary) mb-2', children: 'failure taxonomy' }),
          failureLists.map(list =>
            jsxs('div', {
              key: list.title,
              className: 'mb-3',
              children: [
                jsx('div', { className: 'text-xs font-medium text-(--ui-text-secondary) mb-1', children: list.title }),
                list.items.length === 0
                  ? jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: 'none' })
                  : list.items.slice(0, 5).map((it, idx) =>
                      jsxs('div', {
                        key: idx,
                        className: 'flex items-center gap-2 text-xs mb-0.5',
                        children: [
                          jsx('span', { className: 'abyss-mono tabular-nums text-(--ui-text-quaternary) w-8', children: `${it.count}x` }),
                          jsx('span', {
                            className: 'truncate text-(--ui-text-secondary)',
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
  { key: 'plugin_events', label: 'events', tone: 'text-(--ui-blue)' },
  { key: 'streams', label: 'streams', tone: 'text-(--ui-purple)' },
  { key: 'api_requests', label: 'api', tone: 'text-(--ui-orange)' },
  { key: 'subagents', label: 'subagents', tone: 'text-(--ui-yellow)' },
  { key: 'approvals', label: 'approvals', tone: 'text-(--ui-red)' },
  { key: 'commands', label: 'commands', tone: 'text-(--ui-green)' },
  { key: 'platform_events', label: 'platform', tone: 'text-(--ui-blue)' },
  { key: 'skills', label: 'skills', tone: 'text-(--ui-purple)' }
]

const WAVE_TAG_TONE = {
  events: 'bg-(--ui-blue)/15 text-(--ui-blue)',
  streams: 'bg-(--ui-purple)/15 text-(--ui-purple)',
  api: 'bg-(--ui-orange)/15 text-(--ui-orange)',
  subagents: 'bg-(--ui-yellow)/15 text-(--ui-yellow)',
  approvals: 'bg-(--ui-red)/15 text-(--ui-red)',
  commands: 'bg-(--ui-green)/15 text-(--ui-green)',
  platform: 'bg-(--ui-blue)/15 text-(--ui-blue)',
  skills: 'bg-(--ui-purple)/15 text-(--ui-purple)'
}

function WaveView({ ctx }) {
  const [summary, setSummary] = useState(null)
  const [feed, setFeed] = useState([])
  const [error, setError] = useState(null)

  const fetchAll = useCallback(() => {
    if (!ctx) return
    Promise.all([
      ctx.rest('/wave/summary', { method: 'GET', timeoutMs: 5000 }).catch(() => null),
      ctx.rest('/wave/events?limit=12', { method: 'GET', timeoutMs: 5000 }).catch(() => []),
      ctx.rest('/wave/api?limit=12', { method: 'GET', timeoutMs: 5000 }).catch(() => []),
      ctx.rest('/wave/subagents?limit=12', { method: 'GET', timeoutMs: 5000 }).catch(() => []),
      ctx.rest('/wave/approvals?limit=12', { method: 'GET', timeoutMs: 5000 }).catch(() => []),
      ctx.rest('/wave/streams?limit=12', { method: 'GET', timeoutMs: 5000 }).catch(() => [])
    ]).then(([s, events, api, subagents, approvals, streams]) => {
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
        sub: `${r.status || ''} ${r.finish_reason || ''} ${r.duration_ms != null ? r.duration_ms + 'ms' : ''}`.trim()
      }))
      ;(subagents || []).forEach(r => items.push({
        ts: r.timestamp, tag: 'subagents',
        text: `${r.child_role || 'subagent'} ${r.child_session_id ? r.child_session_id.slice(0, 8) : ''}`.trim(),
        sub: `${r.status || ''} ${r.duration_ms != null ? r.duration_ms + 'ms' : ''}`.trim()
      }))
      ;(approvals || []).forEach(r => items.push({
        ts: r.timestamp, tag: 'approvals',
        text: `${r.pattern_key || 'command'} ${r.choice || 'pending'}`.trim(),
        sub: r.command_preview || ''
      }))
      ;(streams || []).forEach(r => items.push({
        ts: r.timestamp, tag: 'streams',
        text: `${r.provider || '?'} ${r.model || ''} ${r.chars || 0} chars`.trim(),
        sub: `${r.deltas || 0} deltas ${r.error ? 'error' : ''}`.trim()
      }))
      items.sort((a, b) => String(b.ts || '').localeCompare(String(a.ts || '')))
      setFeed(items.slice(0, 40))
      setError(null)
    }).catch(() => setError('wave backend unavailable'))
  }, [ctx])

  useEffect(() => {
    fetchAll()
    const t = setInterval(fetchAll, 15000)
    return () => clearInterval(t)
  }, [fetchAll])

  const tables = summary?.tables || {}

  return jsxs('div', {
    className: 'flex h-full flex-col overflow-auto p-4 gap-4',
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between gap-3',
        children: [
          jsx('span', { className: 'text-xs uppercase tracking-widest text-(--ui-text-tertiary) abyss-mono', children: 'plugin wave — aug 2026 interface' }),
          jsx(Button, { variant: 'ghost', size: 'sm', onClick: fetchAll, children: jsx(Codicon, { name: 'refresh', className: 'text-(--ui-text-tertiary)' }) })
        ]
      }),
      // Surface counts
      jsxs('div', {
        className: 'grid grid-cols-4 gap-2',
        children: WAVE_SURFACES.map(s => {
          const info = tables[s.key]
          return jsxs('div', {
            key: s.key,
            className: 'rounded-lg border border-(--ui-stroke-tertiary) px-3 py-2 bg-(--ui-bg-elevated)',
            children: [
              jsx('div', { className: cn('text-[0.6rem] uppercase tracking-widest abyss-mono', s.tone), children: s.label }),
              jsx('div', { className: cn('text-xl font-bold abyss-mono tabular-nums', s.tone), children: info ? info.count : 0 }),
              jsx('div', { className: 'text-[0.6rem] text-(--ui-text-quaternary) abyss-mono', children: info && info.last ? relativeTime(info.last) : '—' })
            ]
          })
        })
      }),
      error && jsx(ErrorState, { title: 'Wave backend unavailable', description: error, children: jsx(Button, { variant: 'secondary', size: 'sm', onClick: fetchAll, children: 'Retry' }) }),
      // Merged feed
      jsxs('div', {
        className: 'flex flex-col gap-1',
        children: feed.length === 0 && !error
          ? jsx('div', { className: 'text-xs text-(--ui-text-quaternary) abyss-mono px-1', children: 'No wave activity recorded yet — events, streams, API calls, subagents and approvals will appear here.' })
          : feed.map((it, idx) => jsxs('div', {
              key: idx,
              className: 'flex items-start gap-2 rounded-md border border-(--ui-stroke-tertiary) px-3 py-1.5 bg-(--ui-bg-elevated)',
              children: [
                jsx('span', { className: cn('mt-0.5 shrink-0 rounded px-1.5 py-0.5 text-[0.6rem] uppercase tracking-wider abyss-mono', WAVE_TAG_TONE[it.tag] || 'text-(--ui-text-tertiary)'), children: it.tag }),
                jsxs('div', { className: 'min-w-0 flex-1', children: [
                  jsx('div', { className: 'truncate text-xs text-(--ui-text-secondary) abyss-mono', children: it.text }),
                  it.sub && jsx('div', { className: 'truncate text-[0.65rem] text-(--ui-text-quaternary) abyss-mono', children: it.sub })
                ]}),
                jsx('span', { className: 'shrink-0 text-[0.65rem] text-(--ui-text-quaternary) abyss-mono tabular-nums', children: relativeTime(it.ts) })
              ]
            }))
      })
    ]
  })
}

function AbyssDashboard({ ctx }) {
  const [activeTab, setActiveTab] = useState('activity')

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

  const tabs = [
    { value: 'activity', label: 'activity' },
    { value: 'calendar', label: 'calendar' },
    { value: 'search', label: 'search' },
    { value: 'tracing', label: 'trace' },
    { value: 'brain', label: 'brain' },
    { value: 'signals', label: 'watch' },
    { value: 'health', label: 'health' },
    { value: 'wave', label: 'wave' }
  ]

  return jsxs('div', {
    className: 'flex h-full flex-col bg-background text-foreground',
    children: [
      jsx(Masthead, {}),
      jsx(StatusStrip, { ctx }),
      jsx(Separator, {}),
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
            children: activeTab === 'activity' ? jsx(ActivityFeed, { ctx })
              : activeTab === 'calendar' ? jsx(CalendarView, { ctx })
              : activeTab === 'search' ? jsx(GlobalSearch, { ctx })
              : activeTab === 'tracing' ? jsx(TracingView, { ctx })
              : activeTab === 'brain' ? jsx(BrainGraph, { ctx })
              : activeTab === 'signals' ? jsx(SignalsIncidentsView, { ctx })
              : activeTab === 'wave' ? jsx(WaveView, { ctx })
              : jsx(HealthView, { ctx })
          })
        ]
      })
    ]
  })
}

function AbyssStatusChip({ ctx }) {
  const [status, setStatus] = useState(null)

  const refresh = useCallback(() => {
    if (!ctx) return
    ctx.rest('/status', { method: 'GET', timeoutMs: 5000 })
      .then(d => setStatus(d && typeof d === 'object' ? d : null))
      .catch(() => setStatus(null))
  }, [ctx])

  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 30000)
    return () => clearInterval(t)
  }, [refresh])

  const open = status?.signals_open ?? 0
  const score = status?.score
  const level = status?.level || ''
  const tone = level === 'critical' ? 'text-(--ui-red)'
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
        children: [
          jsx('span', {
            className: 'inline-block h-1.5 w-1.5 rounded-full',
            style: {
              backgroundColor: level === 'critical' ? 'var(--ui-red)'
                : level === 'degraded' || level === 'fair' ? 'var(--ui-yellow)'
                : 'var(--ui-green)'
            },
            children: ''
          }),
          score !== null && score !== undefined ? `${score}` : open
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
        signals: 'Signals & Incidents',
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

    // Command palette entry
    ctx.register({
      id: 'abyss-palette-command',
      area: 'palette',
      data: { label: 'Open Abyss Dashboard', icon: 'eye' },
      onSelect: () => host.navigate('/abyss')
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
