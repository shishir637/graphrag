import { useState } from 'react'
import { ago, api } from '../api'
import { Loading, useLoad, useToast } from '../ui'
import { Requests, type Waiting } from './Home'

interface Done { id: number | string; type: string; where: string; kind: string; key: string; asked_by: string; asked: string; decided_by: string; decided: string; reason: string }
const KIND: Record<string, string> = { metric: 'metric', rule: 'rule', synonym: 'word', file: 'file' }
const name = (e: string) => e.includes('@') ? e.split('@')[0].split(/[._]/).map((x) => x[0]?.toUpperCase() + x.slice(1)).join(' ') : e

/* People ask Claude to add their own rules, words, metrics or files to their team or the company.
   Team admins decide for their teams, company admins for everything. */
export default function ApprovalsPage({ onChange }: { onChange: () => void }) {
  const [tab, setTab] = useState<'waiting' | 'approved' | 'declined'>('waiting')
  const d = useLoad<{ items: any[]; metric_checks?: Check[] }>(`/approvals?status=${tab}`, [tab])
  return <>
    <div className="row" style={{ justifyContent: 'space-between', marginBottom: 8 }}>
      <h1 style={{ margin: 0 }}>Approvals</h1>
      <div className="seg">{(['waiting', 'approved', 'declined'] as const).map((t) => <button key={t} className={tab === t ? 'on' : ''} onClick={() => setTab(t)}>
        {t === 'waiting' ? 'Waiting' : t === 'approved' ? 'Approved' : 'Declined'}</button>)}</div>
    </div>
    <p className="small muted" style={{ marginTop: 0 }}>People ask Claude to share their own rules, words, metrics or files with their team or the company. Nothing reaches others until an admin approves it here.</p>
    {d.data?.metric_checks && d.data.metric_checks.length > 0 && <MetricChecks items={d.data.metric_checks} onDone={() => { d.reload(); onChange() }} />}
    {d.error ? <div className="callout bad">{d.error}</div> : !d.data ? <Loading /> :
      tab === 'waiting' ? (d.data.items.length ? <div className="card"><Requests items={d.data.items as Waiting[]} onDone={() => { d.reload(); onChange() }} /></div>
        : <div className="card"><div className="bd small muted">Nothing waits for you.</div></div>) :
        <div className="card"><table className="t plain">
          <thead><tr><th>What</th><th>For</th><th>Asked by</th><th>{tab === 'approved' ? 'Approved' : 'Declined'} by</th><th>Reason</th></tr></thead>
          <tbody>{(d.data.items as Done[]).map((x) => <tr key={String(x.id)}>
            <td className="small"><span className="muted">{KIND[x.kind] || x.kind}</span> <b>{x.key.split('>')[0]}</b></td>
            <td className="small">{x.where.replace(/^the /, '').replace(/ layer$/, '')}</td>
            <td className="small">{name(x.asked_by)}<div className="tiny muted">{ago(x.asked)}</div></td>
            <td className="small">{name(x.decided_by || '')}<div className="tiny muted">{x.decided ? ago(x.decided) : ''}</div></td>
            <td className="small">{x.reason || <span className="muted">none</span>}</td></tr>)}
            {!d.data.items.length && <tr><td colSpan={5} className="muted small" style={{ padding: 18 }}>Nothing yet.</td></tr>}</tbody></table></div>}
  </>
}

interface Check { metric: string; label: string; why: string; error: string; since: string }

/* Metrics the nightly check took out: they stopped running on the current data (e.g. a renamed column). Agents
   do not use them until they work again (checked every night, or now) or an admin approves them anyway. */
function MetricChecks({ items, onDone }: { items: Check[]; onDone: () => void }) {
  const toast = useToast()
  const again = async () => {
    try { await api('/metric-check/run', { method: 'POST' }); toast('Checking the metrics again. This takes a moment.'); setTimeout(onDone, 4000) } catch (e: any) { toast(e.message, true) }
  }
  const approve = async (c: Check) => {
    try { await api(`/metrics/${encodeURIComponent(c.metric)}/status`, { method: 'PUT', json: { status: 'approved' } }); toast(`${c.label} is back in use.`); onDone() } catch (e: any) { toast(e.message, true) }
  }
  return <div className="card" style={{ marginBottom: 16 }}><div className="hd"><div><h2>Metrics taken out by the nightly check</h2>
    <div className="tiny muted">Claude does not use these until they run again. Fix the definition or the data, then check again.</div></div>
    <button className="btn sm" onClick={again}>Check again now</button></div>
    <table className="t plain"><tbody>{items.map((c) => <tr key={c.metric}>
      <td className="small"><b>{c.label}</b><div className="tiny muted">{c.metric}</div></td>
      <td className="small">{c.why}<div className="tiny muted" title={c.error}>{c.error.slice(0, 140)}</div></td>
      <td className="small muted">{ago(c.since)}</td>
      <td style={{ textAlign: 'right' }}><button className="btn ghost sm" onClick={() => approve(c)}>Approve anyway</button></td></tr>)}</tbody></table></div>
}
