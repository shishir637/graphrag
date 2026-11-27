import { useState } from 'react'
import { ago, api } from '../api'
import type { Session } from '../App'
import { askConfirm, Badge, Copy, Empty, ErrorBox, Loading, Modal, PageHead, useLoad, useToast } from '../ui'

interface Layer { id: string; name: string; kind: string; owner: string | null; items: number; last_change: { by_user: string; at: string } | null; shared_with: string[]; link: string | null; in_use_by: number }
interface Change { id: number; layer_id: string; layer_name: string; kind: string; key: string; action: string; before: any; after: any; by_user: string; app: string; at: string; note: string; undone_by: number | null }
interface Item { kind: string; key: string; removed: boolean; updated: string; by: string; body: any }

const KIND: Record<string, string> = { metric: 'Metric', rule: 'Business rule', synonym: 'Synonym', entity: 'Table', join: 'Join', verified: 'Example', value_list: 'Value list', lock: 'Lock', entity_type: 'Graph type', relation_type: 'Graph link type' }

export default function Layers(_: { s: Session }) {
  const ov = useLoad<{ layers: Layer[]; clashes: { metric: string; layers: string[] }[]; locked: string[] }>('/layers')
  const [layer, setLayer] = useState(''); const [kind, setKind] = useState('')
  const hist = useLoad<{ changes: Change[] }>(`/definitions/history?limit=150${layer ? `&layer=${encodeURIComponent(layer)}` : ''}${kind ? `&kind=${kind}` : ''}`, [layer, kind])
  const [open, setOpen] = useState<Layer | null>(null); const [diff, setDiff] = useState<Change | null>(null); const toast = useToast()
  const [lockName, setLockName] = useState('')
  const undo = async (c: Change) => { if (!await askConfirm(`Undo this change to ${(KIND[c.kind] || c.kind).toLowerCase()} ${c.key.split('>')[0]} in ${c.layer_name}? ${c.before == null ? 'It is removed again.' : c.after == null ? 'It comes back.' : 'The previous version comes back.'}`, 'Undo')) return
    try { await api(`/definitions/history/${c.id}/undo`, { method: 'POST' }); hist.reload(); ov.reload(); toast('Undone') } catch (e: any) { toast(e.message, true) } }
  const lock = async (metric: string, locked: boolean) => { try { await api('/definitions/locks', { json: { metric, locked } }); ov.reload(); toast(locked ? `${metric} locked` : `${metric} unlocked`) } catch (e: any) { toast(e.message, true) } }
  if (ov.error) return <div className="page"><PageHead title="Layers" /><ErrorBox error={ov.error} retry={ov.reload} /></div>
  return <div className="page">
    <PageHead title="Layers" sub={<>The company layer everyone reads, and each person's own layer of changes. People change their layer from Claude or ChatGPT (always after a yes), share it, and publishers move changes into the company layer. Every change is recorded and can be undone here.</>} />
    {!ov.data ? <Loading /> : <>
      <div className="card" style={{ marginBottom: 16 }}><div className="hd"><h2>Layers</h2><span className="muted small">{ov.data.layers.length} in total</span></div>
        <div className="tablewrap"><table className="t"><thead><tr><th>Layer</th><th>Changes in it</th><th>Shared with</th><th>Used by</th><th>Last change</th><th /></tr></thead>
          <tbody>{ov.data.layers.map((l) => <tr key={l.id}><td><b>{l.name}</b>{l.kind === 'company' ? <> <Badge kind="brand">company</Badge></> : <div className="tiny muted">{l.owner}</div>}</td>
            <td className="small">{l.items}</td><td className="small">{l.shared_with.length ? l.shared_with.join(', ') : <span className="muted">not shared</span>}</td>
            <td className="small">{l.kind === 'company' ? 'everyone' : `${l.in_use_by} chose to use it`}</td>
            <td className="small muted">{l.last_change ? <>{ago(l.last_change.at)} by {l.last_change.by_user}</> : 'never'}</td>
            <td style={{ textAlign: 'right' }}><button className="btn sm" onClick={() => setOpen(l)}>Open</button> <a className="btn sm" href={`/api/v1/definitions/export?layer=${encodeURIComponent(l.id)}`}>YAML</a></td></tr>)}</tbody></table></div></div>
      <div className="grid2" style={{ marginBottom: 16 }}>
        <div className="card"><div className="hd"><h2>Clashes</h2><span className="muted small">Metrics defined in more than one layer</span></div>
          <div className="bd">{ov.data.clashes.length ? ov.data.clashes.map((c) => <div key={c.metric} className="small" style={{ marginBottom: 6 }}><b>{c.metric}</b>: {c.layers.join(', ')}</div>)
            : <span className="muted small">None: no metric is defined differently by different people.</span>}</div></div>
        <div className="card"><div className="hd"><h2>Locked metrics</h2><span className="muted small">Only publishers and admins can change these, in the company layer</span></div>
          <div className="bd">{ov.data.locked.map((m) => <span key={m} style={{ marginRight: 8 }}><Badge kind="warn">{m}</Badge> <button className="btn ghost sm" onClick={() => lock(m, false)}>unlock</button></span>)}
            {!ov.data.locked.length && <div className="muted small" style={{ marginBottom: 8 }}>None locked.</div>}
            <div className="row" style={{ gap: 8, marginTop: 8 }}><input type="text" placeholder="metric name, e.g. net_revenue" value={lockName} onChange={(e) => setLockName(e.target.value)} aria-label="Metric to lock" />
              <button className="btn sm" disabled={!lockName.trim()} onClick={() => { lock(lockName.trim(), true); setLockName('') }}>Lock</button></div></div></div>
      </div></>}
    <div className="card"><div className="hd"><h2>History</h2>
      <div className="row" style={{ gap: 8 }}><select value={layer} onChange={(e) => setLayer(e.target.value)} aria-label="Layer" style={{ width: 180 }}><option value="">All layers</option>{(ov.data?.layers || []).map((l) => <option key={l.id} value={l.id}>{l.name}</option>)}</select>
        <select value={kind} onChange={(e) => setKind(e.target.value)} aria-label="Kind" style={{ width: 150 }}><option value="">Everything</option>{['metric', 'rule', 'synonym', 'entity', 'lock'].map((k) => <option key={k} value={k}>{KIND[k]}</option>)}</select></div></div>
      {hist.error ? <div className="bd"><ErrorBox error={hist.error} /></div> : !hist.data ? <div className="bd"><Loading /></div> : !hist.data.changes.length ? <div className="bd"><Empty icon="activity" title="No changes yet" /></div> :
        <div className="tablewrap"><table className="t"><thead><tr><th>When</th><th>Who</th><th>Layer</th><th>What</th><th>Change</th><th /></tr></thead>
          <tbody>{hist.data.changes.map((c) => <tr key={c.id} className={c.undone_by ? 'dim' : ''}><td className="small muted">{ago(c.at)}</td>
            <td className="small">{c.by_user}<div className="tiny muted">{c.app}</div></td><td className="small">{c.layer_name}</td>
            <td className="small"><span className="muted">{KIND[c.kind] || c.kind}</span> <b>{c.key.split('>')[0]}</b></td>
            <td className="small">{c.action}{c.undone_by ? ' (undone)' : ''}{c.note ? <div className="tiny muted">{c.note}</div> : null}</td>
            <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}><button className="btn sm" onClick={() => setDiff(c)}>Before / after</button> {!c.undone_by && c.action !== 'undo' && <button className="btn sm" onClick={() => undo(c)}>Undo</button>}</td></tr>)}</tbody></table></div>}</div>
    {open && <LayerModal l={open} onClose={() => setOpen(null)} onChanged={() => { ov.reload(); setOpen(null) }} />}
    {diff && <Modal title={`${KIND[diff.kind] || diff.kind} ${diff.key.split('>')[0]}`} onClose={() => setDiff(null)}>
      <div className="grid2"><div><div className="small muted">Before</div><pre className="code" style={{ whiteSpace: 'pre-wrap' }}>{diff.before ? JSON.stringify(diff.before, null, 1) : '(did not exist)'}</pre></div>
        <div><div className="small muted">After</div><pre className="code" style={{ whiteSpace: 'pre-wrap' }}>{diff.after ? JSON.stringify(diff.after, null, 1) : '(removed)'}</pre></div></div>
      <div className="small muted">{diff.by_user} · {diff.app} · {diff.at}</div></Modal>}
  </div>
}

function LayerModal({ l, onClose, onChanged }: { l: Layer; onClose: () => void; onChanged: () => void }) {
  const items = useLoad<{ items: Item[] }>(`/layers/${encodeURIComponent(l.id)}/items`); const toast = useToast()
  const shown = (items.data?.items || []).filter((i) => !['entity', 'join', 'value_list', 'verified'].includes(i.kind) || l.kind !== 'company')
  return <Modal title={l.name} onClose={onClose} footer={<>{l.kind !== 'company' && l.shared_with.length > 0 && <button className="btn" onClick={async () => { if (!await askConfirm(`Stop sharing ${l.name} with everyone it is shared with?`, 'Stop sharing')) return; try { await api(`/layers/${encodeURIComponent(l.id)}/revoke`, { method: 'POST' }); toast('Sharing stopped'); onChanged() } catch (e: any) { toast(e.message, true) } }}>Stop all sharing</button>}<button className="btn" onClick={onClose}>Close</button></>}>
    {l.link && <div style={{ marginBottom: 12 }}><div className="small muted">Its own MCP link</div><Copy value={l.link} /></div>}
    {items.error ? <ErrorBox error={items.error} /> : !items.data ? <Loading /> : !shown.length ? <div className="muted small">Nothing in this layer yet.</div> :
      <div className="tablewrap"><table className="t"><thead><tr><th>What</th><th>Definition</th><th>Changed</th></tr></thead>
        <tbody>{shown.map((i) => <tr key={i.kind + i.key}><td className="small"><span className="muted">{KIND[i.kind] || i.kind}</span><br /><b>{i.key.split('>')[0]}</b></td>
          <td className="small">{i.removed ? <span className="muted">hidden for this layer</span> : i.kind === 'metric' ? <code>{i.body.expression || `${i.body.numerator} / ${i.body.denominator}`}{i.body.filters?.length ? ` where ${i.body.filters.join(' and ')}` : ''}</code>
            : i.kind === 'rule' ? i.body.text : i.kind === 'synonym' ? `${i.body.term} means ${i.body.target}` : <code>{JSON.stringify(i.body).slice(0, 120)}</code>}</td>
          <td className="small muted">{ago(i.updated)}<div className="tiny">{i.by}</div></td></tr>)}</tbody></table></div>}
  </Modal>
}
