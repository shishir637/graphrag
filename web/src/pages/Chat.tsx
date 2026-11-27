import { Fragment, useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { api, fmtBytes, ago, type Job } from '../api'
import type { Session } from '../App'
import { askConfirm, Badge, Icon, Modal, Spinner, useToast } from '../ui'

/* ---------------- types ---------------- */
interface Me {
  key_set: boolean; key: string | null; model: string; last_used: string | null; company_key_available: boolean
  company_model: string; can_chat: boolean; can_upload: boolean; guest?: boolean; models: [string, string][]; keys_url: string; groups: string[]; all_groups: boolean
}
interface ChatRow { id: string; title: string; updated: string }
interface Upload { ref: string; file: string; kind: string; job_id: string; bytes: number; shown?: boolean; groups?: string[] }
interface Src { id: string; source: string; section: string; pages: number[]; text: string }
interface Step { tool: string; label: string; ok: boolean; summary: string; input: Record<string, unknown> }
interface Msg {
  id: number | string; role: 'user' | 'assistant'; text: string; created?: string; uploads?: Upload[]; sources?: Src[]
  steps?: Step[]; model?: string; key?: string; usage?: { input_tokens: number; output_tokens: number }; error?: string
}

/* ---------------- tiny, safe markdown (no HTML injection: React elements only) ---------------- */
function inline(text: string, cite: (id: string) => ReactNode, key = ''): ReactNode[] {
  const out: ReactNode[] = []
  const re = /(\[[0-9a-f]{12}\])|(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*\s][^*]*\*)/g
  let last = 0, m: RegExpExecArray | null, i = 0
  while ((m = re.exec(text))) {
    const t = m[0]
    if (m.index > last) out.push(m[1] ? text.slice(last, m.index).replace(/\s+$/, '') : text.slice(last, m.index))
    if (m[1]) out.push(<Fragment key={key + i}>{cite(t.slice(1, -1))}</Fragment>)
    else if (m[2]) out.push(<code key={key + i}>{t.slice(1, -1)}</code>)
    else if (m[3]) out.push(<b key={key + i}>{t.slice(2, -2)}</b>)
    else out.push(<i key={key + i}>{t.slice(1, -1)}</i>)
    last = m.index + t.length; i++
  }
  if (last < text.length) out.push(text.slice(last))
  return out
}

function Markdown({ text, cite }: { text: string; cite: (id: string) => ReactNode }) {
  const lines = text.replace(/\r/g, '').split('\n')
  const blocks: ReactNode[] = []
  let i = 0, k = 0
  while (i < lines.length) {
    const l = lines[i]
    if (l.startsWith('```')) {
      const body: string[] = []; i++
      while (i < lines.length && !lines[i].startsWith('```')) body.push(lines[i++])
      i++; blocks.push(<pre key={k++} className="code">{body.join('\n')}</pre>); continue
    }
    if (/^\s*\|.*\|\s*$/.test(l) && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
      const row = (s: string) => s.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim())
      const head = row(l); const rows: string[][] = []; i += 2
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) rows.push(row(lines[i++]))
      blocks.push(<div key={k++} className="tablewrap"><table className="t"><thead><tr>{head.map((h, j) => <th key={j}>{inline(h, cite)}</th>)}</tr></thead>
        <tbody>{rows.map((r, j) => <tr key={j}>{r.map((c, n) => <td key={n}>{inline(c, cite)}</td>)}</tr>)}</tbody></table></div>); continue
    }
    if (/^\s*([-*]|\d+[.)])\s+/.test(l)) {
      const ordered = /^\s*\d/.test(l); const items: string[] = []
      while (i < lines.length && /^\s*([-*]|\d+[.)])\s+/.test(lines[i])) items.push(lines[i++].replace(/^\s*([-*]|\d+[.)])\s+/, ''))
      const L = ordered ? 'ol' : 'ul'
      blocks.push(<L key={k++}>{items.map((t, j) => <li key={j}>{inline(t, cite, `${k}-${j}-`)}</li>)}</L>); continue
    }
    const h = /^(#{1,4})\s+(.*)$/.exec(l)
    if (h) { blocks.push(<h3 key={k++}>{inline(h[2], cite)}</h3>); i++; continue }
    if (!l.trim()) { i++; continue }
    const para: string[] = []
    while (i < lines.length && lines[i].trim() && !/^(```|#{1,4}\s|\s*([-*]|\d+[.)])\s+|\s*\|)/.test(lines[i])) para.push(lines[i++])
    if (!para.length) para.push(lines[i++])
    blocks.push(<p key={k++}>{inline(para.join('\n'), cite)}</p>)
  }
  return <div className="md">{blocks}</div>
}

/* ---------------- key setup ---------------- */
function KeyDialog({ me, onClose, onSaved }: { me: Me; onClose: () => void; onSaved: (m: Me) => void }) {
  const toast = useToast()
  const [key, setKey] = useState(''); const [model, setModel] = useState(me.model)
  const [busy, setBusy] = useState(false); const [err, setErr] = useState('')
  const known = me.models.some(([m]) => m === model)
  const save = async (body: { api_key: string | null; model: string }, msg: string) => {
    setBusy(true); setErr('')
    try { onSaved(await api<Me>('/chat/key', { method: 'PUT', json: body })); toast(msg); onClose() }
    catch (e: any) { setErr(e.message) } finally { setBusy(false) }
  }
  return <Modal title="My Claude key" onClose={onClose} footer={<>
    {me.key_set && <button className="btn ghost danger" disabled={busy} onClick={() => save({ api_key: '', model: '' }, 'Key removed')}>Remove my key</button>}
    <span style={{ flex: 1 }} />
    <button className="btn" onClick={onClose}>Cancel</button>
    <button className="btn primary" disabled={busy || (!key.trim() && !me.key_set)} onClick={() => save({ api_key: key.trim() || null, model }, 'Saved')}>
      {busy ? <Spinner /> : key.trim() ? 'Check and save' : 'Save'}</button></>}>
    <div className="stack small">
      <p style={{ margin: 0 }}>Chat runs on <b>your own Claude API key</b>, so usage is billed to your Claude Console account. Context Layer keeps the key encrypted and never shows it again.</p>
      <ol style={{ margin: 0, paddingLeft: 18 }}>
        <li>Open <a href={me.keys_url} target="_blank" rel="noreferrer">Claude Console → API keys</a> (sign in or create an account; add credit under Billing).</li>
        <li>Click <b>Create key</b> and copy it (it starts with <code>sk-ant-</code>).</li>
        <li>Paste it here.</li>
      </ol>
      <div className="muted tiny">A Claude Pro or Max plan can't be used here: Anthropic only allows those inside its own apps. Use an API key, or add Context Layer as a connector inside Claude instead.</div>
      <label className="f">API key{me.key_set && <span className="muted"> (saved: {me.key}; leave empty to keep it)</span>}
        <input type="password" autoComplete="off" value={key} onChange={(e) => setKey(e.target.value)} placeholder={me.key_set ? 'leave empty to keep the saved key' : 'sk-ant-…'} autoFocus /></label>
      <label className="f">Model
        <select value={known ? model : '__custom'} onChange={(e) => setModel(e.target.value === '__custom' ? '' : e.target.value)}>
          {me.models.map(([m, l]) => <option key={m} value={m}>{l}</option>)}<option value="__custom">Other model name…</option></select></label>
      {!known && <label className="f">Model name<input type="text" value={model} onChange={(e) => setModel(e.target.value)} placeholder="e.g. claude-sonnet-5-5" /></label>}
      {err && <div className="callout bad">{err}</div>}
    </div></Modal>
}

/* ---------------- upload chips ---------------- */
function useJobs(ids: string[]) {
  const [jobs, setJobs] = useState<Record<string, Job>>({})
  const key = ids.join(',')
  useEffect(() => {
    if (!ids.length) return
    let alive = true
    const tick = async () => {
      const next: Record<string, Job> = {}
      for (const id of ids) { try { next[id] = (await api<{ job: Job }>(`/chat/jobs/${id}`)).job } catch { /* gone */ } }
      if (alive) setJobs((j) => ({ ...j, ...next }))
      return Object.values(next).some((j) => ['running', 'queued'].includes(j.status as string))
    }
    let t = 0
    const loop = async () => { if (await tick() && alive) t = window.setTimeout(loop, 2500) }
    loop()
    return () => { alive = false; window.clearTimeout(t) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])
  return jobs
}

function FileChip({ u, job, onRemove }: { u: Upload; job?: Job; onRemove?: () => void }) {
  const st = job?.status as string | undefined
  const label = !st ? 'adding' : st === 'done' ? 'searchable' : st === 'failed' ? 'build failed' : st === 'queued' ? 'queued' : 'building'
  return <span className="fchip" title={`${u.file} · ${fmtBytes(u.bytes)} · ${u.kind === 'table' ? 'data table (metrics)' : 'document'}`}>
    <Icon name={u.kind === 'table' ? 'table' : 'file'} size={14} /><span className="nm">{u.file}</span>
    <span className={`st ${st === 'done' ? 'ok' : st === 'failed' ? 'bad' : ''}`}>{st && st !== 'done' && st !== 'failed' ? <Spinner /> : null}{label}</span>
    {onRemove && <button type="button" className="x" onClick={onRemove} aria-label={`Remove ${u.file}`}><Icon name="x" size={12} /></button>}</span>
}

/* ---------------- one message ---------------- */
function Bubble({ m, jobs }: { m: Msg; jobs: Record<string, Job> }) {
  const [open, setOpen] = useState<string | null>(null)
  const srcById = Object.fromEntries((m.sources || []).map((s) => [s.id, s]))
  const cited = [...new Set([...m.text.matchAll(/\[([0-9a-f]{12})\]/g)].map((x) => x[1]))].filter((id) => srcById[id])
  const cite = (id: string) => {
    if (!srcById[id]) return null
    const n = cited.indexOf(id) + 1
    return <button type="button" className="cite" onClick={() => setOpen(open === id ? null : id)} title={srcById[id].source}>{n}</button>
  }
  if (m.role === 'user') return <div className="msg user"><div className="bubble">
    {m.uploads && m.uploads.length > 0 && <div className="row" style={{ marginBottom: m.text && m.text !== '(attached files)' ? 6 : 0 }}>{m.uploads.map((u) => <FileChip key={u.ref} u={u} job={jobs[u.job_id]} />)}</div>}
    {m.text && m.text !== '(attached files)' && <div style={{ whiteSpace: 'pre-wrap' }}>{m.text}</div>}
    {m.error && <div className="callout bad small" style={{ marginTop: 8 }}>{m.error}</div>}</div></div>
  const body = <Markdown text={m.text} cite={cite} />
  const shown = open ? srcById[open] : null
  return <div className="msg assistant"><div className="bubble">
    {body}
    {cited.length > 0 && <div className="sources">{cited.map((id, i) => <button type="button" key={id} className={`srcchip${open === id ? ' on' : ''}`} onClick={() => setOpen(open === id ? null : id)}>
      <span className="n">{i + 1}</span>{srcById[id].source}{srcById[id].pages.length > 0 && <span className="muted"> p.{srcById[id].pages.join(',')}</span>}</button>)}</div>}
    {shown && <div className="srcbox small"><div className="muted tiny">{shown.source}{shown.section && ` · ${shown.section}`}</div><div style={{ whiteSpace: 'pre-wrap' }}>{shown.text}</div></div>}
    {(m.steps?.length || m.model) && <details className="trace tiny muted"><summary>{m.steps?.length ? `${m.steps.length} lookup${m.steps.length > 1 ? 's' : ''}` : 'details'}{m.model && ` · ${m.model}`}{m.key === 'company' && ' · company key'}</summary>
      {m.steps?.map((s, i) => <div key={i} className="row" style={{ gap: 6 }}>{s.ok ? <Icon name="ok" size={13} /> : <Icon name="x" size={13} />}<b>{s.label}</b><span>{s.summary}</span></div>)}
      {m.usage && <div>{m.usage.input_tokens.toLocaleString()} input · {m.usage.output_tokens.toLocaleString()} output tokens</div>}</details>}
  </div></div>
}

/* ---------------- page ---------------- */
export default function Chat({ s, onSignIn }: { s: Session; onSignIn?: () => void }) {
  const toast = useToast()
  const [me, setMe] = useState<Me | null>(null)
  const [chats, setChats] = useState<ChatRow[]>([])
  const [cid, setCid] = useState<string | null>(null)
  const [msgs, setMsgs] = useState<Msg[]>([])
  const [text, setText] = useState('')
  const [pending, setPending] = useState<Upload[]>([])
  const [uploading, setUploading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [status, setStatus] = useState(''); const [live, setLive] = useState<Step[]>([])
  const [keyOpen, setKeyOpen] = useState(false); const [drag, setDrag] = useState(false)
  const endRef = useRef<HTMLDivElement>(null); const fileRef = useRef<HTMLInputElement>(null); const taRef = useRef<HTMLTextAreaElement>(null)

  const loadChats = useCallback(async () => { const r = await api<{ chats: ChatRow[] }>('/chats'); setChats(r.chats); return r.chats }, [])
  useEffect(() => {
    api<Me>('/chat/me').then((m) => { setMe(m); if (!m.can_chat && !m.guest) setKeyOpen(true) }).catch((e) => toast(e.message, true))
    loadChats().then((c) => { if (c[0]) setCid(c[0].id) }).catch(() => {})
  }, [loadChats, toast])
  useEffect(() => {
    if (!cid) { setMsgs([]); return }
    api<{ messages: Msg[] }>(`/chats/${cid}`).then((r) => setMsgs(r.messages)).catch((e) => { toast(e.message, true); setCid(null) })
  }, [cid, toast])
  useEffect(() => { endRef.current?.scrollIntoView({ block: 'end' }) }, [msgs, live, status])

  const jobIds = [...new Set([...msgs.flatMap((m) => (m.uploads || []).map((u) => u.job_id)), ...pending.map((u) => u.job_id)])].filter(Boolean)
  const jobs = useJobs(jobIds)

  const ensureChat = async () => {
    if (cid) return cid
    const { chat } = await api<{ chat: ChatRow }>('/chats', { method: 'POST' })
    setChats((c) => [chat, ...c]); setCid(chat.id); return chat.id
  }
  const addFiles = async (files: FileList | File[]) => {
    const list = [...files]; if (!list.length) return
    if (!me?.can_upload) { toast('Your admin has not allowed adding files from the chat.', true); return }
    setUploading(true)
    try {
      const id = await ensureChat()
      const fd = new FormData(); list.slice(0, 10).forEach((f) => fd.append('files', f))
      const r = await api<{ files: Upload[]; errors: string[] }>(`/chats/${id}/files`, { form: fd })
      setPending((p) => [...p, ...r.files]); r.errors.forEach((e) => toast(e, true))
    } catch (e: any) { toast(e.message, true) } finally { setUploading(false) }
  }
  const send = async () => {
    const t = text.trim(); if ((!t && !pending.length) || busy) return
    if (!me?.can_chat) { if (!me?.guest) setKeyOpen(true); return }
    setBusy(true); setStatus('Thinking'); setLive([])
    const ups = pending
    let id: string
    try { id = await ensureChat() } catch (e: any) { toast(e.message, true); setBusy(false); return }
    setMsgs((m) => [...m, { id: `tmp${Date.now()}`, role: 'user', text: t, uploads: ups }]); setText(''); setPending([])
    try {
      const r = await fetch(`/api/v1/chats/${id}/messages`, { method: 'POST', credentials: 'same-origin', headers: { 'X-CL': '1', 'Content-Type': 'application/json' }, body: JSON.stringify({ text: t, uploads: ups }) })
      if (!r.ok || !r.body) { let d = ''; try { d = (await r.json()).detail } catch { /* */ } throw new Error(d || `Request failed (${r.status})`) }
      const reader = r.body.getReader(); const dec = new TextDecoder(); let buf = ''
      for (;;) {
        const { value, done } = await reader.read(); if (done) break
        buf += dec.decode(value, { stream: true })
        let nl: number
        while ((nl = buf.indexOf('\n')) >= 0) {
          const line = buf.slice(0, nl).trim(); buf = buf.slice(nl + 1); if (!line) continue
          const ev = JSON.parse(line)
          if (ev.type === 'status') setStatus(ev.text)
          else if (ev.type === 'step') setLive((l) => [...l, ev.step])
          else if (ev.type === 'answer') setMsgs((m) => [...m, ev.message])
          else if (ev.type === 'error') {
            setMsgs((m) => m.map((x, i) => i === m.length - 1 && x.role === 'user' ? { ...x, error: ev.text } : x))
            if (/key/i.test(ev.text)) api<Me>('/chat/me').then(setMe).catch(() => {})
          }
        }
      }
    } catch (e: any) {
      setMsgs((m) => m.map((x, i) => i === m.length - 1 && x.role === 'user' ? { ...x, error: e.message } : x))
    } finally { setBusy(false); setStatus(''); setLive([]); loadChats().catch(() => {}); taRef.current?.focus() }
  }
  const newChat = () => { setCid(null); setMsgs([]); setPending([]); taRef.current?.focus() }
  const remove = async (c: ChatRow) => {
    if (!await askConfirm(`Delete "${c.title}"? This can't be undone. Files you added stay in Context Layer.`, 'Delete')) return
    try { await api(`/chats/${c.id}`, { method: 'DELETE' }); setChats((x) => x.filter((y) => y.id !== c.id)); if (cid === c.id) newChat() } catch (e: any) { toast(e.message, true) }
  }
  const first = me?.guest ? 'there' : s.user.name.split(/\s+/)[0]
  const keyLabel = !me ? '' : me.key_set ? `Your key · ${me.model}` : me.company_key_available ? `Company key · ${me.company_model}` : 'No Claude key'

  return <div className="chatpage" onDragOver={(e) => { if (e.dataTransfer.types.includes('Files')) { e.preventDefault(); setDrag(true) } }}
    onDragLeave={(e) => { if (e.currentTarget === e.target) setDrag(false) }} onDrop={(e) => { e.preventDefault(); setDrag(false); addFiles(e.dataTransfer.files) }}>
    <aside className="chatlist" aria-label="Chats">
      <button className="btn primary" style={{ width: '100%', justifyContent: 'center' }} onClick={newChat}><Icon name="plus" size={16} />New chat</button>
      <div className="items">{chats.map((c) => <div key={c.id} className={`citem${c.id === cid ? ' on' : ''}`}>
        <button type="button" className="t" onClick={() => setCid(c.id)} title={c.title}><span>{c.title}</span><span className="tiny muted">{ago(c.updated)}</span></button>
        <button type="button" className="btn ghost sm del" onClick={() => remove(c)} aria-label={`Delete ${c.title}`}><Icon name="x" size={14} /></button></div>)}
        {!chats.length && <div className="tiny muted" style={{ padding: 8 }}>No chats yet.</div>}</div>
    </aside>
    <section className="chatmain">
      <div className="chathead">
        <div className="row" style={{ minWidth: 0, flexWrap: 'nowrap' }}><button className="btn ghost sm mobonly" onClick={newChat} aria-label="New chat"><Icon name="plus" size={16} /></button>
          {chats.length > 0 && <select className="mobonly" value={cid || ''} onChange={(e) => e.target.value ? setCid(e.target.value) : newChat()} aria-label="Chats" style={{ maxWidth: 170, minWidth: 120 }}>
            <option value="">New chat</option>{chats.map((c) => <option key={c.id} value={c.id}>{c.title}</option>)}</select>}
          <b className="deskonly">{chats.find((c) => c.id === cid)?.title || 'New chat'}</b></div>
        {me && (me.guest ? <div className="row"><span className="small muted">Guest · {me.company_model} · public documents only</span>
          {onSignIn && <button className="btn sm" onClick={onSignIn}>Sign in</button>}</div>
          : <button className="btn ghost sm" onClick={() => setKeyOpen(true)} title="My Claude key"><Icon name="key" size={15} />
          {me.can_chat ? <span className="small">{keyLabel}</span> : <Badge kind="warn">Add your Claude key</Badge>}</button>)}
      </div>
      <div className="msgs">
        {!msgs.length && <div className="hello">
          <h1>Hi {first}, what do you want to know?</h1>
          <p className="muted">Answers come from {s.organisation || 'your organisation'}'s documents, metrics and knowledge graph that you have access to, with sources. Drop files here to add them.</p>
          {me && !me.can_chat && !me.guest && <button className="btn primary" onClick={() => setKeyOpen(true)}><Icon name="key" size={16} />Add your Claude API key</button>}
        </div>}
        {msgs.map((m) => <Bubble key={m.id} m={m} jobs={jobs} />)}
        {busy && <div className="msg assistant"><div className="bubble thinking"><div className="row"><Spinner /><span className="muted">{status || 'Thinking'}…</span></div>
          {live.map((st, i) => <div key={i} className="tiny muted row" style={{ gap: 6 }}>{st.ok ? <Icon name="ok" size={13} /> : <Icon name="x" size={13} />}{st.label}: {st.summary}</div>)}</div></div>}
        <div ref={endRef} />
      </div>
      <div className="composer">
        {pending.length > 0 && <div className="row" style={{ marginBottom: 8 }}>{pending.map((u) => <FileChip key={u.ref} u={u} job={jobs[u.job_id]} onRemove={() => setPending((p) => p.filter((x) => x.ref !== u.ref))} />)}
          <span className="tiny muted">Added to Context Layer for: {pending[0].groups?.join(', ') || 'your groups'}</span></div>}
        <div className="cbox">
          <button type="button" className="btn ghost sm" onClick={() => fileRef.current?.click()} disabled={uploading || !me?.can_upload} title={me?.can_upload ? 'Attach files' : 'Adding files is turned off for you'} aria-label="Attach files">
            {uploading ? <Spinner /> : <Icon name="upload" size={17} />}</button>
          <input ref={fileRef} type="file" multiple hidden onChange={(e) => { if (e.target.files) addFiles(e.target.files); e.target.value = '' }} />
          <textarea ref={taRef} rows={1} value={text} placeholder={me?.can_chat === false ? 'Add your Claude API key to start' : 'Ask about your company…'} aria-label="Message"
            onChange={(e) => { setText(e.target.value); e.target.style.height = 'auto'; e.target.style.height = Math.min(e.target.scrollHeight, 220) + 'px' }}
            onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); send() } }} />
          <button className="btn primary" onClick={send} disabled={busy || uploading || (!text.trim() && !pending.length)} aria-label="Send">{busy ? <Spinner /> : 'Send'}</button>
        </div>
        <div className="tiny muted" style={{ marginTop: 6 }}>Enter to send, Shift+Enter for a new line. Claude can make mistakes: check the sources.</div>
      </div>
      {drag && <div className="dropzone"><Icon name="upload" size={28} /><b>Drop files to add them to Context Layer</b></div>}
    </section>
    {keyOpen && me && <KeyDialog me={me} onClose={() => setKeyOpen(false)} onSaved={setMe} />}
  </div>
}
