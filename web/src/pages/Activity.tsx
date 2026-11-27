import { useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { can, type Event } from '../api'
import type { Session } from '../App'
import { Badge, Empty, ErrorBox, Icon, Loading, PageHead, useLoad } from '../ui'

export default function Activity({ s }: { s: Session }) {
  const [params, setParams] = useSearchParams()
  const type = params.get('type') || '', actor = params.get('actor') || ''
  const [q, setQ] = useState(params.get('q') || '')
  const qs = new URLSearchParams({ ...(type && { type }), ...(actor && { actor }), ...(params.get('q') && { q: params.get('q')! }), limit: '300' })
  const { data, error, reload } = useLoad<{ events: Event[] }>(`/events?${qs}`, [qs.toString()])
  const set = (k: string, v: string) => { const p = new URLSearchParams(params); v ? p.set(k, v) : p.delete(k); setParams(p) }
  return <div className="page">
    <PageHead title="Activity" sub="Every agent call and every change made in Context Layer: who, what, when, and whether it worked.">
      {can(s.user, 'owner', 'admin') && <a className="btn" href={`/api/v1/events.csv${type ? `?type=${type}` : ''}`}><Icon name="download" size={16} />Export CSV</a>}
      <button className="btn" onClick={reload}><Icon name="refresh" size={16} />Refresh</button>
    </PageHead>
    <div className="row" style={{ marginBottom: 14 }}>
      <div className="seg">{[['', 'Everything'], ['agent', 'Agent calls'], ['user', 'Admin actions']].map(([v, l]) => <button key={v} className={type === v ? 'on' : ''} onClick={() => set('type', v)}>{l}</button>)}</div>
      <form onSubmit={(e) => { e.preventDefault(); set('q', q) }} className="row"><input type="search" placeholder="Search questions, files, actions" value={q} onChange={(e) => setQ(e.target.value)} style={{ width: 280 }} aria-label="Search activity" /></form>
      {actor && <span className="badge brand">{actor} <a href="#" onClick={(e) => { e.preventDefault(); set('actor', '') }} aria-label="Clear">×</a></span>}
    </div>
    {error ? <ErrorBox error={error} retry={reload} /> : !data ? <Loading /> : <div className="card">{data.events.length ?
      <div className="tablewrap"><table className="t"><thead><tr><th>When</th><th>Who</th><th>What</th><th>Details</th><th>Result</th><th>Time</th></tr></thead>
        <tbody>{data.events.map((e) => { const d = e.detail as any
          return <tr key={e.id}><td className="small muted" style={{ whiteSpace: 'nowrap' }}>{new Date(e.time).toLocaleString()}</td>
            <td><Badge kind={e.actor_type === 'agent' ? 'brand' : ''}>{e.actor_type === 'agent' ? 'agent' : 'admin'}</Badge>{' '}
              <a href="#" onClick={(ev) => { ev.preventDefault(); set('actor', e.actor) }}><b>{e.actor}</b></a></td>
            <td className="mono">{e.action}</td>
            <td className="small" style={{ maxWidth: 420 }}>{e.target}{d.summary && <div className="tiny muted">{String(d.summary)}</div>}{d.error && <div className="tiny" style={{ color: 'var(--bad)' }}>{String(d.error)}</div>}
              {d.groups && <div className="tiny muted">groups: {(d.groups as string[]).join(', ')}</div>}{d.who && <div className="tiny muted">{String(d.who)}</div>}</td>
            <td>{e.ok ? <Badge kind="ok">ok</Badge> : <Badge kind="bad">failed</Badge>}</td>
            <td className="small muted">{e.seconds != null ? `${e.seconds.toFixed(2)} s` : ''}</td></tr> })}</tbody></table></div>
      : <Empty icon="activity" title="Nothing here yet">Agent calls and admin actions appear here as they happen.</Empty>}</div>}
  </div>
}
