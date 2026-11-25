import { useState } from 'react'
import { api, can } from '../api'
import type { Session } from '../App'
import { Badge, Empty, ErrorBox, Loading, PageHead, useLoad, useToast } from '../ui'

interface Metric { name: string; label: string; description: string; type: string; format: string; owner: string; status: string; expression: string; filters: string[] | null }
interface Dim { name: string; label: string; description: string }

export default function Metrics({ s }: { s: Session }) {
  const { data, error, reload } = useLoad<{ metrics: Metric[]; dimensions: Dim[]; reviewed_in: string }>('/metrics')
  const [q, setQ] = useState(''); const [tab, setTab] = useState<'metrics' | 'dimensions'>('metrics'); const toast = useToast()
  const admin = can(s.user, 'owner', 'admin')
  const setStatus = async (name: string, status: string) => {
    try { await api(`/metrics/${name}/status`, { method: 'PUT', json: { status } }); toast(status === 'approved' ? 'Approved: agents can use it after Prepare data' : 'Set back to draft'); reload() } catch (e: any) { toast(e.message, true) }
  }
  if (error) return <div className="page"><ErrorBox error={error} retry={reload} /></div>
  if (!data) return <div className="page"><Loading /></div>
  const ql = q.toLowerCase()
  const ms = data.metrics.filter((m) => !q || `${m.name} ${m.label} ${m.description}`.toLowerCase().includes(ql))
  const ds = data.dimensions.filter((d) => !q || `${d.name} ${d.label} ${d.description}`.toLowerCase().includes(ql))
  return <div className="page">
    <PageHead title="Metrics" sub="Every business number, defined once. Agents ask for these by name; Context Layer writes the query from the definition, so the AI never invents SQL or a formula." />
    <div className="tabs"><button className={tab === 'metrics' ? 'on' : ''} onClick={() => setTab('metrics')}>Metrics ({data.metrics.length})</button>
      <button className={tab === 'dimensions' ? 'on' : ''} onClick={() => setTab('dimensions')}>Break down by ({data.dimensions.length})</button></div>
    <input type="search" placeholder="Search" value={q} onChange={(e) => setQ(e.target.value)} style={{ maxWidth: 320, marginBottom: 14 }} aria-label="Search metrics" />
    {tab === 'metrics' ? <div className="card">{ms.length ? <div className="tablewrap"><table className="t"><thead><tr><th>Metric</th><th>Definition</th><th>Owner</th><th>Status</th></tr></thead>
      <tbody>{ms.map((m) => <tr key={m.name}><td style={{ minWidth: 200 }}><b>{m.label}</b><div className="tiny mono muted">{m.name}</div></td>
        <td><div>{m.description}</div><div className="tiny mono muted" style={{ marginTop: 4 }}>{m.expression}{m.filters?.length ? ` where ${m.filters.join(' and ')}` : ''}</div></td>
        <td>{m.owner}</td><td><Badge kind={m.status === 'approved' ? 'ok' : 'warn'}>{m.status}</Badge>{admin && <div style={{ marginTop: 6 }}><button className="btn sm" onClick={() => setStatus(m.name, m.status === 'approved' ? 'draft' : 'approved')}>{m.status === 'approved' ? 'Unapprove' : 'Approve'}</button></div>}</td></tr>)}</tbody></table></div>
      : <Empty icon="metric" title="No metrics">Metric definitions live in <code>semantic_layer/metrics.yaml</code>.</Empty>}</div>
      : <div className="card"><div className="tablewrap"><table className="t"><thead><tr><th>Dimension</th><th>Description</th></tr></thead>
        <tbody>{ds.map((d) => <tr key={d.name}><td><b>{d.label}</b><div className="tiny mono muted">{d.name}</div></td><td>{d.description}</td></tr>)}</tbody></table></div></div>}
    <div className="callout" style={{ marginTop: 16 }}>New tables get draft definitions drafted from the data profile (no AI model). Agents see only approved metrics: review the formula, then approve. Definitions live in <code>{data.reviewed_in}</code>, where tests check every formula.</div>
  </div>
}
