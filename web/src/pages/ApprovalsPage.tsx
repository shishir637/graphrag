import { useState } from 'react'
import { ago } from '../api'
import { Loading, useLoad } from '../ui'
import { Requests, type Waiting } from './Home'

interface Done { id: number | string; type: string; where: string; kind: string; key: string; asked_by: string; asked: string; decided_by: string; decided: string; reason: string }
const KIND: Record<string, string> = { metric: 'metric', rule: 'rule', synonym: 'word', file: 'file' }
const name = (e: string) => e.includes('@') ? e.split('@')[0].split(/[._]/).map((x) => x[0]?.toUpperCase() + x.slice(1)).join(' ') : e

/* People ask Claude to add their own rules, words, metrics or files to their team or the company.
   Team admins decide for their teams, company admins for everything. */
export default function ApprovalsPage({ onChange }: { onChange: () => void }) {
  const [tab, setTab] = useState<'waiting' | 'approved' | 'declined'>('waiting')
  const d = useLoad<{ items: any[] }>(`/approvals?status=${tab}`, [tab])
  return <>
    <div className="row" style={{ justifyContent: 'space-between', marginBottom: 8 }}>
      <h1 style={{ margin: 0 }}>Approvals</h1>
      <div className="seg">{(['waiting', 'approved', 'declined'] as const).map((t) => <button key={t} className={tab === t ? 'on' : ''} onClick={() => setTab(t)}>
        {t === 'waiting' ? 'Waiting' : t === 'approved' ? 'Approved' : 'Declined'}</button>)}</div>
    </div>
    <p className="small muted" style={{ marginTop: 0 }}>People ask Claude to share their own rules, words, metrics or files with their team or the company. Nothing reaches others until an admin approves it here.</p>
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
