import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from './api'
import { Badge, Copy, CopyBlock, Icon, Modal, Spinner, useToast } from './ui'

interface Step {
  id: string; title: string; body: string; done: boolean; to: string; info?: boolean; sample?: boolean
  running?: boolean; count?: number; progress?: { passages: number; entities: number; metrics: number }
  mcp_url?: string; snippets?: { claude_web?: string; chatgpt?: string; claude_code?: string; cursor?: string; scopes?: Record<string, string> }
}
export interface GuideData { role: string; show: boolean; dismissed: boolean; done: number; total: number; steps: Step[] }

/** Getting started: opens by itself on a person's first sign-in to a workspace; reopen from the menu. */
export function Guide({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [g, setG] = useState<GuideData | null>(null)
  const [busy, setBusy] = useState(false)
  const [expanded, setExpanded] = useState<string | null>(null)
  const nav = useNavigate()
  const toast = useToast()
  const load = () => api<GuideData>('/guide').then((d) => {
    setG(d); setExpanded((e) => e ?? d.steps.find((s) => !s.done)?.id ?? null)
  }).catch(() => {})
  useEffect(() => { if (open) load() }, [open])
  useEffect(() => {                                 // follow a running build
    if (!open || !g?.steps.some((s) => s.running)) return
    const t = window.setInterval(load, 4000); return () => window.clearInterval(t)
  }, [open, g])
  if (!open) return null
  const close = async (dismiss: boolean) => {
    if (dismiss) await api('/guide/dismiss', { method: 'POST' }).catch(() => {})
    onClose()
  }
  const sample = async () => {
    setBusy(true)
    try { await api('/guide/sample', { method: 'POST' }); toast('Sample data loaded'); load() }
    catch (e: any) { toast(e.message, true) } finally { setBusy(false) }
  }
  const go = (to: string) => { onClose(); nav(to) }
  return <Modal title="Getting started" onClose={() => close(false)}
    footer={<><button className="btn ghost" onClick={() => close(true)}>Don't show again</button>
      <button className="btn primary" onClick={() => close(false)}>Close</button></>}>
    {!g ? <div className="row muted"><Spinner /> Loading…</div> : <div className="stack">
      <p className="muted small" style={{ margin: 0 }}>{g.done} of {g.total} done. Steps tick themselves when the work is really done.</p>
      <ol className="guide">{g.steps.map((s, i) => <li key={s.id} className={s.done ? 'done' : ''}>
        <button className="guide-hd" aria-expanded={expanded === s.id} aria-controls={`gs-${s.id}`}
          onClick={() => setExpanded(expanded === s.id ? null : s.id)}>
          <span className={`tick${s.done ? ' done' : ''}`} aria-hidden="true">{s.done ? <Icon name="ok" size={13} /> : <span className="n">{i + 1}</span>}</span>
          <b>{s.title}</b>
          {s.running && <Badge kind="brand"><Spinner /> building</Badge>}
          {!!s.count && <Badge kind="warn">{s.count} waiting</Badge>}
          <span className="vh">{s.done ? '(done)' : '(to do)'}</span>
        </button>
        {expanded === s.id && <div id={`gs-${s.id}`} className="guide-bd">
          <p>{s.body}</p>
          {s.progress && <p className="small muted">{s.progress.passages.toLocaleString()} passages · {s.progress.entities.toLocaleString()} graph entities · {s.progress.metrics} metrics</p>}
          {s.mcp_url && <><div className="small muted">Your MCP link</div><Copy value={s.mcp_url} /></>}
          {s.snippets?.claude_web && <p className="small">{s.snippets.claude_web}</p>}
          {s.snippets?.chatgpt && <p className="small">{s.snippets.chatgpt}</p>}
          {s.snippets?.claude_code && <><div className="small muted">Claude Code (with a key)</div><CopyBlock value={s.snippets.claude_code} /></>}
          {s.snippets?.cursor && <><div className="small muted">Cursor: .cursor/mcp.json</div><CopyBlock value={s.snippets.cursor} /></>}
          {s.snippets?.scopes && <ul className="small">{Object.entries(s.snippets.scopes).map(([k, v]) => <li key={k}><b>{k}</b> key: {v}</li>)}</ul>}
          <div className="row">
            {!s.info && <button className="btn sm" onClick={() => go(s.to)}>{s.done ? 'Open' : 'Start'}</button>}
            {s.sample && <button className="btn sm" disabled={busy} onClick={sample}>{busy ? <Spinner /> : 'Try sample data'}</button>}
            {s.id === 'connect' && g.role !== 'viewer' && <button className="btn sm" onClick={() => go('/agents')}><Icon name="key" size={14} />Create a key</button>}
          </div>
        </div>}
      </li>)}</ol>
    </div>}
  </Modal>
}

/** Opens the guide on first sign-in (not dismissed and something left to do). */
export function useGuideAutoOpen(): [boolean, (v: boolean) => void] {
  const [open, setOpen] = useState(false)
  useEffect(() => { api<GuideData>('/guide').then((d) => { if (d.show) setOpen(true) }).catch(() => {}) }, [])
  return [open, setOpen]
}
