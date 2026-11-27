import { useState } from 'react'
import { ago } from '../api'
import { Empty, ErrorBox, Loading, PageHead, useLoad } from '../ui'
import { Requests } from './Home'

interface Done { id: number; type: string; where: string; kind: string; key: string; summary: string; asked_by: string; asked: string; decided_by: string; decided: string; reason: string }
const KIND: Record<string, string> = { metric: 'Metric', rule: 'Rule', synonym: 'Word' }

export default function Approvals() {
  const [tab, setTab] = useState<'waiting' | 'approved' | 'declined'>('waiting')
  const d = useLoad<{ items: any[] }>(`/approvals?status=${tab}`, [tab])
  return <div className="page narrow">
    <PageHead title="Approvals" sub="Changes people asked for, for a team or everyone. Nothing reaches others until an admin approves it." />
    <div className="tabs">{(['waiting', 'approved', 'declined'] as const).map((t) =>
      <button key={t} className={tab === t ? 'on' : ''} onClick={() => setTab(t)}>{t[0].toUpperCase() + t.slice(1)}</button>)}</div>
    <div className="card">
      {d.error ? <div className="bd"><ErrorBox error={d.error} retry={d.reload} /></div> : !d.data ? <div className="bd"><Loading /></div> :
        tab === 'waiting' ? (d.data.items.length ? <Requests items={d.data.items} onDone={d.reload} /> : <div className="bd"><Empty icon="check" title="Nothing waits for you" /></div>) :
          !d.data.items.length ? <div className="bd"><Empty icon="activity" title={`Nothing ${tab} yet`} /></div> :
            <div className="tablewrap"><table className="t"><thead><tr><th>Change</th><th>Asked by</th><th>Decided</th><th>Reason</th></tr></thead>
              <tbody>{(d.data.items as Done[]).map((x) => <tr key={x.id}>
                <td className="small"><span className="muted">{KIND[x.kind] || x.kind}</span> <b>{x.key.split('>')[0]}</b><div className="tiny muted">{x.where}</div></td>
                <td className="small">{x.asked_by}<div className="tiny muted">{ago(x.asked)}</div></td>
                <td className="small">{x.decided_by}<div className="tiny muted">{ago(x.decided)}</div></td>
                <td className="small">{x.reason || <span className="muted">none</span>}</td></tr>)}</tbody></table></div>}
    </div>
  </div>
}
