import { Link } from 'react-router-dom'
import { ago, type Agent, type Event, type Job } from '../api'
import type { Session } from '../App'
import { Badge, ErrorBox, Icon, Loading, PageHead, useLoad } from '../ui'

interface Ov {
  status: { documents: { count: number; ready: number; pending: number; passages: number; indexed: string | null }
    data: { tables: number; metrics: number; ready: boolean }; graph: { ready: boolean; entities: number; relations: number; built: string | null }
    postgres: { ok: boolean; message: string } }
  checklist: { id: string; label: string; done: boolean; to: string; optional?: boolean }[]
  usage: { day: string; calls: number; errors: number }[]; agents: { active: number; total: number }
  top_agents: Agent[]; problems: Event[]; job: Job | null
}

export default function Overview({ s }: { s: Session }) {
  const { data, error, reload } = useLoad<Ov>('/overview')
  if (error) return <div className="page"><ErrorBox error={error} retry={reload} /></div>
  if (!data) return <div className="page"><Loading /></div>
  const st = data.status
  const required = data.checklist.filter((c) => !c.optional)
  const setupDone = required.every((c) => c.done)
  const week = data.usage.slice(-7).reduce((a, d) => a + d.calls, 0)
  const max = Math.max(1, ...data.usage.map((d) => d.calls))
  return <div className="page">
    <PageHead title={`Good to see you, ${s.user.name.split(' ')[0]}`} sub="Company knowledge your AI agents can use, with sources and access rules you control." />
    {data.job && <div className="callout" style={{ marginBottom: 16 }}><b>{data.job.label}</b> is running. <span className="muted">Started {ago(data.job.started)} by {data.job.by}.</span></div>}
    {!setupDone && <div className="card"><div className="hd"><div><h2>Get set up</h2><div className="muted small">{data.checklist.filter((c) => c.done).length} of {data.checklist.length} done</div></div></div>
      <div className="bd" style={{ paddingTop: 4, paddingBottom: 4 }}>{data.checklist.map((c) => <div key={c.id} className="check-item">
        <div className={`tick${c.done ? ' done' : ''}`}>{c.done && <Icon name="ok" size={13} />}</div>
        <div style={{ flex: 1 }}><b style={{ textDecoration: c.done ? 'line-through' : 'none', color: c.done ? 'var(--muted)' : undefined }}>{c.label}</b>{c.optional && <span className="muted small"> · optional</span>}</div>
        {!c.done && <Link className="btn sm" to={c.to}>Start</Link>}</div>)}</div></div>}
    <div className="stats" style={{ marginTop: setupDone ? 0 : 16 }}>
      <Link to="/sources/documents" className="card stat" style={{ color: 'inherit', textDecoration: 'none' }}><div className="k"><Icon name="file" size={15} />Documents</div><div className="v">{st.documents.ready}</div>
        <div className="s">{st.documents.pending ? <span style={{ color: 'var(--warn)' }}>{st.documents.pending} waiting to be prepared</span> : `${st.documents.passages.toLocaleString()} passages searchable`}</div></Link>
      <Link to="/knowledge/metrics" className="card stat" style={{ color: 'inherit', textDecoration: 'none' }}><div className="k"><Icon name="metric" size={15} />Metrics</div><div className="v">{st.data.metrics}</div>
        <div className="s">{st.data.ready ? `from ${st.data.tables} tables` : 'data not prepared'}</div></Link>
      <Link to="/knowledge/graph" className="card stat" style={{ color: 'inherit', textDecoration: 'none' }}><div className="k"><Icon name="graph" size={15} />Knowledge graph</div><div className="v">{st.graph.entities.toLocaleString()}</div>
        <div className="s">{st.graph.ready ? `entities · ${st.graph.relations.toLocaleString()} links` : 'not built'}</div></Link>
      <Link to="/agents" className="card stat" style={{ color: 'inherit', textDecoration: 'none' }}><div className="k"><Icon name="agent" size={15} />Agents</div><div className="v">{data.agents.active}</div>
        <div className="s">{week.toLocaleString()} calls this week</div></Link>
    </div>
    <div className="grid g21" style={{ marginTop: 16 }}>
      <div className="card"><div className="hd"><h2>Agent calls, last 14 days</h2><Link to="/activity" className="small">All activity</Link></div>
        <div className="bd"><div className="bars" role="img" aria-label="Agent calls per day">{data.usage.map((d) =>
          <div key={d.day} className={`b${d.calls ? '' : ' zero'}`} style={{ height: `${(d.calls / max) * 100}%` }} title={`${d.day}: ${d.calls} calls, ${d.errors} errors`} />)}</div>
          <div className="barlabels">{data.usage.map((d, i) => <span key={d.day}>{i % 2 === 0 ? new Date(d.day).toLocaleDateString(undefined, { day: 'numeric', month: 'short' }) : ''}</span>)}</div></div></div>
      <div className="card"><div className="hd"><h2>System</h2></div><div className="bd stack small">
        <div className="between"><span>AI model inside</span><span className="muted">none · agents bring their own</span></div>
        <div className="between"><span>Agent link (MCP)</span><code className="tiny">{window.location.origin}/mcp</code></div>
        <div className="between"><span>Search index</span>{st.documents.indexed ? <span className="muted">updated {ago(st.documents.indexed)}</span> : <Badge kind="warn">not built</Badge>}</div>
        <div className="between"><span>Metrics catalog</span>{st.data.ready ? <Badge kind="ok" dot>ready</Badge> : <Badge>not prepared</Badge>}</div>
        <div className="between"><span>Graph database (Cypher copy)</span>{st.postgres.ok ? <Badge kind="ok" dot>online</Badge> : <Badge dot>offline</Badge>}</div>
      </div></div>
    </div>
    <div className="grid g2" style={{ marginTop: 16 }}>
      <div className="card"><div className="hd"><h2>Most active agents</h2><Link to="/agents" className="small">Manage</Link></div>
        {data.top_agents.length ? <table className="t"><tbody>{data.top_agents.map((a) => <tr key={a.id}><td><b>{a.name}</b><div className="tiny muted">last used {ago(a.last_used)}</div></td><td style={{ textAlign: 'right' }}>{a.calls.toLocaleString()} call{a.calls === 1 ? '' : 's'}</td></tr>)}</tbody></table>
          : <div className="bd muted small">No agents connected yet. <Link to="/agents">Connect one</Link>.</div>}</div>
      <div className="card"><div className="hd"><h2>Recent problems</h2></div>
        {data.problems.length ? <table className="t"><tbody>{data.problems.map((e) => <tr key={e.id}><td><Badge kind="bad">{e.actor_type}</Badge></td><td><b>{e.actor}</b> · {e.action === 'login' ? 'sign-in failed' : e.action}<div className="tiny muted">{String((e.detail as any).error || e.target || '')}</div></td><td className="tiny muted" style={{ whiteSpace: 'nowrap' }}>{ago(e.time)}</td></tr>)}</tbody></table>
          : <div className="bd muted small">Nothing has failed recently.</div>}</div>
    </div>
  </div>
}
