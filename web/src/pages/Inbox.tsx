import { useState } from 'react'
import { api, can } from '../api'
import type { Session } from '../App'
import { Badge, Empty, ErrorBox, Loading, PageHead, Spinner, useLoad, useToast } from '../ui'

interface Item {
  type: 'fact' | 'link' | 'definition' | 'ontology'; id: string; title: string; reason?: string; quote?: string
  document?: string; value?: string; confidence?: number; score?: number; source?: string
}
const TYPE_LABEL: Record<Item['type'], string> = { fact: 'Fact from a document', link: 'Name matched to data', definition: 'Definition', ontology: 'Graph type' }

export default function Inbox({ s }: { s: Session }) {
  const { data, error, reload } = useLoad<{ items: Item[] }>('/inbox')
  const [busy, setBusy] = useState<string | null>(null)
  const [editing, setEditing] = useState<Record<string, string>>({})
  const toast = useToast()
  if (!can(s.user, 'owner', 'admin')) return <div className="page"><Empty icon="lock" title="For owners and admins">The inbox is where owners and admins approve what Context Layer was unsure about.</Empty></div>
  if (error) return <div className="page"><ErrorBox error={error} retry={reload} /></div>
  if (!data) return <div className="page"><Loading /></div>
  const act = async (it: Item, action: 'approve' | 'reject' | 'edit') => {
    const key = `${it.type}/${it.id}`
    setBusy(key)
    try {
      await api(`/inbox/${it.type}/${encodeURIComponent(it.id)}`, { json: { action, value: action === 'edit' ? editing[key] : undefined } })
      toast(action === 'reject' ? 'Rejected' : 'Approved: agents can use it'); reload()
    } catch (e: any) { toast(e.message, true) } finally { setBusy(null) }
  }
  return <div className="page">
    <PageHead title="Inbox" sub="What Context Layer was not sure about: facts read from documents, names matched to your data, drafted metrics and graph types. Agents only use what is approved." />
    {data.items.length === 0 ? <div className="card"><Empty icon="check" title="Nothing waiting">Everything read from your sources passed its checks.</Empty></div>
      : <div className="stack">{data.items.map((it) => {
        const key = `${it.type}/${it.id}`
        return <div className="card" key={key}><div className="bd stack">
          <div className="between"><b>{it.title}</b><Badge kind="warn">{TYPE_LABEL[it.type]}</Badge></div>
          {it.reason && <div className="small muted">Why: {it.reason}</div>}
          {it.quote && <blockquote className="quote">“{it.quote}”{it.document && <div className="tiny muted">from {it.document}</div>}</blockquote>}
          <div className="row" style={{ flexWrap: 'wrap' }}>
            <button className="btn primary sm" disabled={busy === key} onClick={() => act(it, 'approve')}>{busy === key ? <Spinner /> : 'Approve'}</button>
            <button className="btn sm" disabled={busy === key} onClick={() => act(it, 'reject')}>Reject</button>
            {(it.type === 'fact' || it.type === 'link') && <>
              <label className="vh" htmlFor={`e-${key}`}>Corrected value</label>
              <input id={`e-${key}`} type="text" style={{ maxWidth: 220 }} placeholder={it.type === 'link' ? 'Right row key' : 'Corrected value'}
                value={editing[key] ?? ''} onChange={(e) => setEditing({ ...editing, [key]: e.target.value })} />
              <button className="btn sm" disabled={!editing[key] || busy === key} onClick={() => act(it, 'edit')}>Save correction</button></>}
          </div></div></div>
      })}</div>}
  </div>
}
