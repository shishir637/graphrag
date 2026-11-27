import { useState } from 'react'
import { Link } from 'react-router-dom'
import { ago, api } from '../api'
import type { Session } from '../App'
import { Copy, ErrorBox, Loading, askConfirm, useLoad, useToast } from '../ui'

export interface Waiting { body?: any; id: number | string; type: string; where: string; kind?: string; key: string; summary: string; asked_by: string; asked?: string; effect?: string; can_decide: boolean; review?: string }
export interface HomeData {
  user: { name: string; email: string; role: string; title: string; teams: string[] }
  organisation: string
  links: { name: string; kind: string; team?: string; url: string }[]
  reach: { name: string; kind: string }[]
  shared_with_me: { layer: string; name: string; url: string }[]
  my_changes: { change_id: number; item: string; action: string; at: string; app: string; layer: string; undone: boolean }[]
  waiting_for_me: Waiting[]
  manages: string[]
  company_admin: boolean
}

const NOUN: Record<string, string> = { metric: 'metric', rule: 'rule', synonym: 'word' }
export const teamSlug = (t: string) => t.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 60) || 'team'
const short = (e: string) => e.includes('@') ? e.split('@')[0].split(/[._]/).map((x) => x[0]?.toUpperCase() + x.slice(1)).join(' ') : e
const where = (w: string) => w === 'the company layer' ? 'everyone' : w.replace(/ layer$/, '')

/* one sentence per request: "Priya wants to add the rule 'X' to the Data Engineering team layer" */
export function sentence(w: Waiting) {
  if (w.kind === 'file') return <><b>{short(w.asked_by)}</b> wants to share the file <b>{w.key}</b> with {w.where}</>
  if (w.type === 'metric draft') return <>New metric found in the data: <b>{w.key}</b>{w.summary && w.summary !== w.key ? <span className="muted"> ({w.summary})</span> : null}</>
  if (!w.can_decide) return <><b>{w.key}</b>: {w.summary}</>
  let b: any = w.body ?? null; if (typeof b === 'string') { try { b = JSON.parse(b) } catch { b = null } }
  const what = !b ? <>remove the {NOUN[w.kind || ''] || w.kind} <b>{w.key.split('>')[0]}</b></>
    : w.kind === 'rule' ? <>add the rule <b>“{b.text}”</b></>
    : w.kind === 'synonym' ? <>make <b>“{b.term}”</b> mean <b>{b.target}</b></>
    : <>set the metric <b>{w.key}</b>{b.description ? <> ({b.description})</> : null}</>
  return <><b>{short(w.asked_by)}</b> wants to {what} for {where(w.where)}</>
}

export function Requests({ items, onDone }: { items: Waiting[]; onDone: () => void }) {
  const toast = useToast(); const [busy, setBusy] = useState('')
  const decide = async (w: Waiting, approve: boolean) => {
    setBusy(String(w.id))
    try { const path = String(w.id).startsWith('file:') ? `/approvals/file/${String(w.id).slice(5)}` : `/approvals/${w.id}`
      await api(path, { json: { approve } }); toast(approve ? 'Approved: it applies now' : 'Declined'); onDone() }
    catch (e: any) { toast(e.message, true) } finally { setBusy('') }
  }
  const approveMetric = async (w: Waiting) => {
    setBusy(String(w.id))
    try { await api(`/metrics/${encodeURIComponent(w.key)}/status`, { method: 'PUT', json: { status: 'approved' } }); toast('Metric approved: Claude can use it now'); onDone() }
    catch (e: any) { toast(e.message, true) } finally { setBusy('') }
  }
  items = items.filter((w) => w.type !== 'graph types')        // the graph page is not in the app for now
  return <div className="list">{items.map((w) => <div key={String(w.id)} className="li">
    <div className="small" style={{ minWidth: 0 }}>{sentence(w)}{w.asked && <span className="muted"> · {ago(w.asked)}</span>}</div>
    <div className="row" style={{ flex: 'none' }}>{w.can_decide ? <>
      <button className="btn sm" disabled={busy === String(w.id)} onClick={() => decide(w, false)}>Decline</button>
      <button className="btn sm primary" disabled={busy === String(w.id)} onClick={() => decide(w, true)}>Approve</button></>
      : w.type === 'metric draft' ? <button className="btn sm primary" disabled={busy === String(w.id)} onClick={() => approveMetric(w)}>Approve metric</button> : null}</div>
  </div>)}</div>
}

export default function Home({ s }: { s: Session }) {
  const h = useLoad<HomeData>('/home'); const toast = useToast()
  if (h.error) return <div className="page narrow"><ErrorBox error={h.error} retry={h.reload} /></div>
  if (!h.data) return <div className="page narrow"><Loading /></div>
  const d = h.data
  const link = d.links[d.links.length - 1]           // the deepest team link also brings in the company and the teams above
  const changes = d.my_changes.filter((c) => !c.undone && c.action !== 'undo').slice(0, 5)
  const undo = async (id: number) => {
    if (!(await askConfirm('Undo this change? Claude stops using it straight away.', 'Undo'))) return
    try { await api(`/home/changes/${id}/undo`, { method: 'POST' }); h.reload() } catch (e: any) { toast(e.message, true) }
  }
  const signOutAll = async () => {
    if (!(await askConfirm('Sign out in every browser, including this one? Claude stays connected.', 'Sign out everywhere'))) return
    await api('/logout-everywhere', { method: 'POST' }).catch(() => {}); location.reload()
  }
  return <div className="page narrow">
    <h1 style={{ fontSize: 22, margin: '4px 0 2px' }}>Hello, {d.user.name.split(' ')[0]}</h1>
    <p className="muted small" style={{ margin: '0 0 20px' }}>{[d.user.title, d.user.teams.join(', '), s.organisation || d.organisation].filter(Boolean).join(' · ')}</p>

    {d.waiting_for_me.length > 0 && <section className="sec">
      <h2>Needs your approval <Link className="small" style={{ fontWeight: 400, marginLeft: 8 }} to="/approvals">history</Link></h2>
      <div className="card"><Requests items={d.waiting_for_me} onDone={h.reload} /></div></section>}

    <section className="sec"><h2>Your link for Claude or ChatGPT</h2>
      <div className="card"><div className="bd stack">
        <Copy value={link.url} label="Copy" />
        <div className="small muted">Add it as a connector and sign in as {d.user.email}. It gives Claude {link.kind === 'team' ? `the company's knowledge plus ${link.team}'s rules and words` : "the company's knowledge"}.
          Tell Claude when something should be counted differently: the change is yours until an admin approves it for others.</div>
      </div></div></section>

    {changes.length > 0 && <section className="sec"><h2>Your changes</h2>
      <div className="card"><div className="list">{changes.map((c) => { const [k, ...r] = c.item.split(' ')
        return <div key={c.change_id} className="li"><div className="small">{c.action} {NOUN[k] || k} <b>{r.join(' ').split('>')[0]}</b> <span className="muted">· {ago(c.at)}</span></div>
          <button className="btn ghost sm" onClick={() => undo(c.change_id)}>Undo</button></div> })}</div></div></section>}

    <p className="small muted" style={{ marginTop: 28 }}><a href="/account">Password and connected apps</a> · <a href="#" onClick={(e) => { e.preventDefault(); signOutAll() }}>Sign out everywhere</a></p>
  </div>
}
