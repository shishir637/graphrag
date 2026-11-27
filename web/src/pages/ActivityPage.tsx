import { useState } from 'react'
import { ago } from '../api'
import { Loading, useLoad } from '../ui'
import type { Waiting } from './Home'

interface Ev { id: number; time: string; who: string; app: string; what: string; ok: boolean }
const KINDS = [['', 'Everything'], ['questions', 'Claude use'], ['changes', 'Changes'], ['signins', 'Sign-ins']] as const

/* the same thing done several times in a row shows once, with a count */
function collapse(ev: Ev[]) {
  const out: { e: Ev; n: number }[] = []
  for (const e of ev) { const last = out[out.length - 1]; if (last && last.e.who === e.who && last.e.what === e.what) last.n++; else out.push({ e, n: 1 }) }
  return out
}

/* What needs a decision, then what happened, in plain words. */
export default function ActivityPage(_: { onChange?: () => void }) {
  const [kind, setKind] = useState(''); const [q, setQ] = useState('')
  const d = useLoad<{ events: Ev[]; waiting: Waiting[] }>(`/activity?kind=${kind}&q=${encodeURIComponent(q)}`, [kind, q])
  return <>
    {d.data && d.data.waiting.filter((w) => w.can_decide).length > 0 && <div className="callout" style={{ marginBottom: 16 }}>{d.data.waiting.filter((w) => w.can_decide).length} waiting for your approval. <a href="#approvals">Open Approvals</a></div>}
    <div className="row" style={{ justifyContent: 'space-between', marginBottom: 12 }}>
      <h1 style={{ margin: 0 }}>Activity</h1>
      <div className="row"><input type="search" placeholder="Search" value={q} onChange={(e) => setQ(e.target.value)} style={{ width: 180 }} aria-label="Search activity" />
        <div className="seg">{KINDS.map(([k, l]) => <button key={k} className={kind === k ? 'on' : ''} onClick={() => setKind(k)}>{l}</button>)}</div>
        <a className="btn sm" href="/api/v1/events.csv">Export</a></div>
    </div>
    {d.error ? <div className="callout bad">{d.error}</div> : !d.data ? <Loading /> :
      <div className="card"><div className="list tight">{collapse(d.data.events).map(({ e, n }) => <div key={e.id} className="li">
        <div className="small" style={{ minWidth: 0 }}><b>{e.who}</b> {e.what}{n > 1 && <span className="muted"> ×{n}</span>}{!e.ok && <span className="bad-text"> (failed)</span>}</div>
        <div className="tiny muted" style={{ flex: 'none' }}>{ago(e.time)} · {e.app}</div></div>)}
        {!d.data.events.length && <div className="li small muted">Nothing yet.</div>}</div></div>}
  </>
}
