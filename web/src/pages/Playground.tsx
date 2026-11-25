import { useState, type FormEvent } from 'react'
import { api, type Agent, type Answer } from '../api'
import { Badge, CAP_LABEL, Icon, PageHead, Spinner, useLoad } from '../ui'

const MODES = [['auto', 'Auto'], ['documents', 'Documents'], ['metrics', 'Metrics'], ['graph', 'Connections'], ['overview', 'Overview']] as const
const ROUTE_TEXT: Record<string, string> = {
  documents: 'Passages from document search', data: 'Computed from a reviewed metric', connections: 'Passages plus knowledge-graph links',
  overview: 'Knowledge-graph communities (big picture)',
}

export default function Playground() {
  const { data } = useLoad<{ agents: Agent[]; groups: string[] }>('/agents')
  const [q, setQ] = useState(''); const [who, setWho] = useState('groups'); const [groups, setGroups] = useState<string[]>(['public'])
  const [mode, setMode] = useState('auto'); const [busy, setBusy] = useState(false); const [ans, setAns] = useState<Answer | null>(null)
  const [err, setErr] = useState('')
  const active = (data?.agents || []).filter((a) => !a.revoked)
  const ask = async (e?: FormEvent) => {
    e?.preventDefault(); if (!q.trim()) return
    setBusy(true); setAns(null); setErr('')
    try { setAns(await api<Answer>('/playground', { json: who === 'groups' ? { question: q, groups, mode } : { question: q, agent_id: who, mode } })) }
    catch (x: any) { setErr(x.message) } finally { setBusy(false) }
  }
  const agent = active.find((a) => a.id === who)
  return <div className="page">
    <PageHead title="Playground" sub="Ask exactly as an agent would and see the evidence it receives. Context Layer finds the evidence; the agent writes the answer." />
    <form className="card" onSubmit={ask}><div className="bd stack">
      <textarea value={q} onChange={(e) => setQ(e.target.value)} placeholder="e.g. What was net revenue by region last quarter?  ·  Who approves leave over 10 days?" rows={2} aria-label="Question"
        onKeyDown={(e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) ask() }} />
      <div className="between" style={{ alignItems: 'flex-end' }}>
        <div className="row" style={{ alignItems: 'flex-end', gap: 16 }}>
          <label className="f" style={{ minWidth: 240 }}>Ask as<select value={who} onChange={(e) => setWho(e.target.value)}>
            <option value="groups">Groups I pick (all capabilities)</option>{active.map((a) => <option key={a.id} value={a.id}>Agent: {a.name}</option>)}</select></label>
          {who === 'groups' ? <div><div className="small" style={{ fontWeight: 600, marginBottom: 6 }}>Groups</div><div className="checks">{(data?.groups || ['public']).map((g) =>
            <label key={g} className="check"><input type="checkbox" checked={groups.includes(g)} onChange={(e) => setGroups(e.target.checked ? [...groups, g] : groups.filter((x) => x !== g))} />{g}</label>)}</div></div>
            : agent && <div className="small muted">Groups: {agent.groups.join(', ') || '—'} · May use: {agent.capabilities.map((c) => CAP_LABEL[c]).join(', ')}</div>}
        </div>
        <div className="row"><div className="seg" role="group" aria-label="Source">{MODES.map(([v, l]) => <button type="button" key={v} className={mode === v ? 'on' : ''} onClick={() => setMode(v)}>{l}</button>)}</div>
          <button className="btn primary" disabled={busy || !q.trim() || (who === 'groups' && !groups.length)}>{busy ? <Spinner /> : <Icon name="play" size={16} />}Ask</button></div>
      </div></div></form>
    {err && <div className="callout bad" style={{ marginTop: 16 }}>{err}</div>}
    {busy && <div className="card" style={{ marginTop: 16 }}><div className="bd row muted"><Spinner /> Routing the question and gathering evidence…</div></div>}
    {ans && <div className="grid g21" style={{ marginTop: 16 }}>
      <div className="stack">
        <div className="card"><div className="hd"><div className="row"><Badge kind="brand">{ans.route}</Badge><span className="small muted">{ROUTE_TEXT[ans.route] || ''}{ans.route_reason && ` · ${ans.route_reason}`}</span></div>
          {ans.found === false ? <Badge kind="warn">nothing found</Badge> : <Badge kind="ok">evidence found</Badge>}</div>
          <div className="bd">{ans.answer ? <div className="answer">{ans.answer}</div> : <div className="muted small">{ans.guidance}</div>}
            {ans.data?.not_answerable && <div className="callout warn" style={{ marginTop: 12 }}>{ans.data.not_answerable}</div>}
            {ans.notes.map((n, i) => <div key={i} className="callout warn" style={{ marginTop: 12 }}>{n}</div>)}</div></div>
        {ans.data && <div className="card"><div className="hd"><h2>How the number was computed</h2>{ans.data.row_count != null && <span className="muted small">{ans.data.row_count} rows</span>}</div><div className="bd stack small">
          {ans.data.assumptions?.length ? <div><b>Assumptions</b><ul style={{ margin: '4px 0 0', paddingLeft: 18 }}>{ans.data.assumptions.map((a, i) => <li key={i}>{a}</li>)}</ul></div> : null}
          {ans.data.rows?.length ? <div className="tablewrap"><table className="t"><thead><tr>{Object.keys(ans.data.rows[0]).map((k) => <th key={k}>{k}</th>)}</tr></thead>
            <tbody>{ans.data.rows.slice(0, 20).map((r, i) => <tr key={i}>{Object.values(r).map((v, j) => <td key={j}>{typeof v === 'number' ? v.toLocaleString() : String(v ?? '')}</td>)}</tr>)}</tbody></table></div> : null}
          {ans.data.sql && <details><summary className="muted">Generated SQL (from the reviewed definition)</summary><pre className="code" style={{ marginTop: 8 }}>{ans.data.sql}</pre></details>}
          {ans.data.value_corrections?.map((w, i) => <div key={i} className="muted">value corrected: {w}</div>)}</div></div>}
        {ans.passages.length > 0 && <div className="card"><div className="hd"><h2>Passages the agent receives</h2><span className="muted small">{ans.passages.length}</span></div>
          <div className="bd stack">{ans.passages.map((p, i) => <details key={p.id} className="src" open={i === 0}>
            <summary className="between"><span><b>{p.source}</b>{p.section && <span className="muted"> · {p.section}</span>}{p.pages.length > 0 && <span className="muted"> · p. {p.pages.join(', ')}</span>}</span>
              <span className="row">{p.included_for && <Badge>for {p.included_for}</Badge>}{p.score != null && <span className="tiny muted">score {p.score.toFixed(2)}</span>}</span></summary>
            {p.highlights && p.highlights.length > 0 && <div className="small" style={{ margin: '8px 0', borderLeft: '3px solid var(--brand)', paddingLeft: 10 }}>{p.highlights.map((h, j) => <div key={j}>{h}</div>)}</div>}
            <p className="small" style={{ whiteSpace: 'pre-wrap', margin: '8px 0 0' }}>{p.text}</p><div className="tiny muted mono">{p.id}</div></details>)}</div></div>}
        {ans.overview && ans.overview.length > 0 && <div className="card"><div className="hd"><h2>Communities in the knowledge graph</h2></div><div className="bd stack small">
          {ans.overview.map((c) => <div key={c.community}><b>{c.title}</b> <span className="muted">· {c.size} entities</span>
            <div className="row" style={{ marginTop: 4 }}>{c.main_entities.slice(0, 8).map((e, i) => <Badge key={i}>{e.name}</Badge>)}</div>
            {c.key_links.slice(0, 4).map((l, i) => <div key={i} className="muted">{l.source} — {l.target}: {l.evidence}</div>)}</div>)}</div></div>}
      </div>
      <div className="stack">
        <div className="card"><div className="hd"><h2>Trace</h2></div><div className="bd small stack">
          <div className="between"><span className="muted">Asked</span><span>{ans.asked}</span></div>
          {Object.entries(ans.timings_s).map(([k, v]) => <div key={k} className="between"><span className="muted">{k.replace(/_/g, ' ')}</span><span>{v.toFixed(2)} s</span></div>)}</div></div>
        {ans.graph && <div className="card"><div className="hd"><h2>Graph links</h2></div><div className="bd small stack">
          <div className="row">{ans.graph.entities.map((e, i) => <Badge key={i} kind="brand">{e.name}</Badge>)}{ans.graph.bridges?.slice(0, 6).map((e, i) => <Badge key={`b${i}`}>{e.name}</Badge>)}</div>
          {ans.graph.relations.slice(0, 15).map((r, i) => <div key={i}><b>{r.source}</b> <span className="muted">{r.type.toLowerCase().replace('_', ' ')}</span> <b>{r.target}</b>{r.evidence && <div className="tiny muted">{r.evidence}</div>}</div>)}</div></div>}
      </div>
    </div>}
  </div>
}
