import { useEffect, useMemo, useState } from 'react'
import { ago, can, type Job } from '../api'
import type { Session } from '../App'
import { Badge, Empty, ErrorBox, Icon, JobPanel, Loading, PageHead, useJob, useLoad } from '../ui'

interface Ent { id: string; name: string; type: string; degree: number; description: string; community: number | null; passages: number }
interface Rel { source: string; target: string; type: string; weight: number; description: string }
interface G { overview: null | { nodes: number; edges: number; entities: number; relations: number; built: string; entities_by_type: Record<string, number>; relations_by_type: Record<string, number> }
  entities: Ent[]; relations: Rel[]; total: number }

const COLORS = ['#4338ca', '#0f766e', '#b45309', '#be185d', '#0369a1', '#4d7c0f', '#7c3aed', '#c2410c', '#475569', '#15803d']

function layout(nodes: Ent[], edges: Rel[], W: number, H: number) {
  let seed = 11; const rnd = () => ((seed = (seed * 16807) % 2147483647) / 2147483647)
  const P: Record<string, { x: number; y: number; dx: number; dy: number }> = {}
  nodes.forEach((n) => { P[n.id] = { x: W / 2 + (rnd() - 0.5) * W * 0.8, y: H / 2 + (rnd() - 0.5) * H * 0.8, dx: 0, dy: 0 } })
  const k = Math.sqrt((W * H) / Math.max(1, nodes.length)) * 0.8
  let t = W / 8
  for (let it = 0; it < 220; it++) {
    for (const n of nodes) { P[n.id].dx = 0; P[n.id].dy = 0 }
    for (let i = 0; i < nodes.length; i++) for (let j = i + 1; j < nodes.length; j++) {
      const a = P[nodes[i].id], b = P[nodes[j].id]; let dx = a.x - b.x, dy = a.y - b.y
      const d = Math.max(0.01, Math.hypot(dx, dy)), f = (k * k) / d; dx = (dx / d) * f; dy = (dy / d) * f
      a.dx += dx; a.dy += dy; b.dx -= dx; b.dy -= dy
    }
    for (const e of edges) {
      const a = P[e.source], b = P[e.target]; if (!a || !b) continue
      const dx = a.x - b.x, dy = a.y - b.y, d = Math.max(0.01, Math.hypot(dx, dy)), f = (d * d) / k
      a.dx -= (dx / d) * f; a.dy -= (dy / d) * f; b.dx += (dx / d) * f; b.dy += (dy / d) * f
    }
    for (const n of nodes) {
      const p = P[n.id]; p.dx += (W / 2 - p.x) * 0.03; p.dy += (H / 2 - p.y) * 0.03
      const d = Math.max(0.01, Math.hypot(p.dx, p.dy)); p.x += (p.dx / d) * Math.min(d, t); p.y += (p.dy / d) * Math.min(d, t)
    }
    t *= 0.985
  }
  const xs = nodes.map((n) => P[n.id].x), ys = nodes.map((n) => P[n.id].y)
  const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)]
  for (const n of nodes) { const p = P[n.id]; p.x = x1 > x0 ? 80 + ((p.x - x0) / (x1 - x0)) * (W - 160) : W / 2; p.y = y1 > y0 ? 30 + ((p.y - y0) / (y1 - y0)) * (H - 60) : H / 2 }
  return P
}

export default function Graph({ s }: { s: Session }) {
  const [q, setQ] = useState(''); const [query, setQuery] = useState('')
  const { data, error, reload } = useLoad<G>(`/graph?q=${encodeURIComponent(query)}`, [query])
  const running = useLoad<{ running: Job | null }>('/jobs')
  const job = useJob(() => reload())
  const [sel, setSel] = useState<string | null>(null)
  const admin = can(s.user, 'owner', 'admin')
  useEffect(() => { if (running.data?.running?.kind === 'graph') job.attach(running.data.running) }, [running.data])  // eslint-disable-line
  const W = 1000, H = 560
  const types = useMemo(() => Array.from(new Set((data?.entities || []).map((e) => e.type))).sort(), [data])
  const color = (t: string) => COLORS[types.indexOf(t) % COLORS.length]
  const P = useMemo(() => (data && data.entities.length ? layout(data.entities, data.relations, W, H) : {}), [data])
  if (error) return <div className="page"><ErrorBox error={error} retry={reload} /></div>
  if (!data) return <div className="page"><Loading /></div>
  const byId = Object.fromEntries(data.entities.map((e) => [e.id, e]))
  const deg: Record<string, number> = {}; data.relations.forEach((r) => { deg[r.source] = (deg[r.source] || 0) + 1; deg[r.target] = (deg[r.target] || 0) + 1 })
  const near = new Set<string>(); if (sel) data.relations.forEach((r) => { if (r.source === sel) near.add(r.target); if (r.target === sel) near.add(r.source) })
  const labelled = new Set([...data.entities].sort((a, b) => (deg[b.id] || 0) - (deg[a.id] || 0)).slice(0, 22).map((e) => e.id))
  const selE = sel ? byId[sel] : null
  const ov = data.overview
  return <div className="page">
    <PageHead title="Knowledge graph" sub="People, organisations, products, places and how they connect, extracted from your documents and tables. Agents use it for questions that link facts across documents.">
      {admin && <button className="btn primary" disabled={job.running} onClick={() => job.start('graph')}><Icon name="refresh" size={16} />{ov ? 'Rebuild graph' : 'Build graph'}</button>}
    </PageHead>
    <JobPanel job={job.job} />
    {!ov ? <div className="card"><Empty icon="graph" title="The knowledge graph is not built yet" action={admin && <button className="btn primary" disabled={job.running} onClick={() => job.start('graph')}>Build the graph</button>}>
      Local models (GLiNER and spaCy) find the people, places, organisations and things in every passage and link those named together, with the sentence as evidence. Duplicates are merged and related entities grouped into communities. Needs prepared documents; no AI model or API key.</Empty></div> : <>
      <div className="stats">
        <div className="card stat"><div className="k">Entities</div><div className="v">{ov.entities.toLocaleString()}</div><div className="s">{Object.keys(ov.entities_by_type || {}).length} types</div></div>
        <div className="card stat"><div className="k">Relationships</div><div className="v">{ov.relations.toLocaleString()}</div><div className="s">{Object.keys(ov.relations_by_type || {}).length} kinds</div></div>
        <div className="card stat"><div className="k">Most common</div><div className="v" style={{ fontSize: 18, marginTop: 10 }}>{Object.keys(ov.entities_by_type || {})[0] || '—'}</div><div className="s">entity type</div></div>
        <div className="card stat"><div className="k">Built</div><div className="v" style={{ fontSize: 18, marginTop: 10 }}>{ago(ov.built)}</div><div className="s">rebuild after adding documents</div></div>
      </div>
      <div className="grid g21" style={{ marginTop: 16 }}>
        <div className="card"><div className="hd"><form className="row" style={{ flex: 1 }} onSubmit={(e) => { e.preventDefault(); setSel(null); setQuery(q) }}>
          <input type="search" placeholder="Find an entity, e.g. a person or product" value={q} onChange={(e) => setQ(e.target.value)} style={{ maxWidth: 340 }} aria-label="Find an entity" />
          <button className="btn"><Icon name="search" size={15} />Find</button>{query && <button type="button" className="btn ghost" onClick={() => { setQ(''); setQuery(''); setSel(null) }}>Show most connected</button>}</form>
          <span className="muted small">{data.entities.length} of {data.total.toLocaleString()} shown</span></div>
          <div className="bd"><div className="graph-wrap">
            {data.entities.length ? <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', height: 'auto', display: 'block' }} role="img" aria-label="Knowledge graph">
              {data.relations.map((r, i) => { const a = P[r.source], b = P[r.target]; if (!a || !b) return null; const on = sel && (r.source === sel || r.target === sel)
                return <g key={i}><line x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke={on ? 'var(--brand)' : 'var(--line)'} strokeWidth={on ? 2 : 1}><title>{r.type}</title></line>
                  {on && <text x={(a.x + b.x) / 2} y={(a.y + b.y) / 2 - 3} fontSize={10} textAnchor="middle" fill="var(--brand-ink)" style={{ paintOrder: 'stroke', stroke: 'var(--code)', strokeWidth: 3 }}>{r.type}</text>}</g> })}
              {data.entities.map((e) => { const p = P[e.id]; const r = 5 + Math.min(9, (deg[e.id] || 0) * 1.2); const dim = sel && e.id !== sel && !near.has(e.id)
                return <g key={e.id} className="gnode" opacity={dim ? 0.2 : 1} onClick={() => setSel(sel === e.id ? null : e.id)} tabIndex={0} role="button" aria-label={e.name}
                  onKeyDown={(ev) => { if (ev.key === 'Enter') setSel(sel === e.id ? null : e.id) }}>
                  <circle cx={p.x} cy={p.y} r={r} fill={color(e.type)} stroke={e.id === sel ? 'var(--ink)' : 'var(--code)'} strokeWidth={e.id === sel ? 3 : 1.5}><title>{`${e.name} (${e.type})`}</title></circle>
                  {(labelled.has(e.id) || e.id === sel || near.has(e.id)) && <text x={p.x} y={p.y - r - 4} fontSize={11} textAnchor="middle" fill="var(--ink)" style={{ paintOrder: 'stroke', stroke: 'var(--code)', strokeWidth: 3, pointerEvents: 'none' }}>{e.name.length > 24 ? e.name.slice(0, 23) + '…' : e.name}</text>}</g> })}
            </svg> : <div className="empty muted">No entity matches “{query}”.</div>}
          </div><div className="row" style={{ marginTop: 10 }}>{types.map((t) => <span key={t} className="badge"><span className="dot" style={{ color: color(t) }} />{t}</span>)}</div></div></div>
        <div className="card"><div className="hd"><h2>{selE ? selE.name : 'Details'}</h2></div><div className="bd">
          {selE ? <div className="stack small"><div className="row"><Badge kind="brand">{selE.type}</Badge><span className="muted">{selE.passages} source passage{selE.passages === 1 ? '' : 's'}</span></div>
            <p style={{ margin: 0 }}>{selE.description || <span className="muted">No description.</span>}</p><div className="sep" />
            <b>Connections</b>{data.relations.filter((r) => r.source === sel || r.target === sel).slice(0, 30).map((r, i) =>
              <div key={i}><span className="muted">{r.source === sel ? '→' : '←'}</span> <b style={{ color: 'var(--brand-ink)' }}>{r.type}</b>{' '}
                <a href="#" onClick={(ev) => { ev.preventDefault(); setSel(r.source === sel ? r.target : r.source) }}>{byId[r.source === sel ? r.target : r.source]?.name}</a>
                {r.description && <div className="tiny muted">{r.description}</div>}</div>)}</div>
            : <p className="muted small" style={{ margin: 0 }}>Click an entity to see what the graph knows about it and how it connects. The most connected entities are shown; search to explore any other.</p>}</div></div>
      </div></>}
  </div>
}
