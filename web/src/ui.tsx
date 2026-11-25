import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react'
import { api, type Job } from './api'

/* ---------------- icons (inline, 18px, stroke) ---------------- */
const P: Record<string, ReactNode> = {
  home: <path d="M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z" />,
  file: <><path d="M14 3H6a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8z" /><path d="M14 3v5h5M8 13h8M8 17h6" /></>,
  table: <><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M3 10h18M9 4v16" /></>,
  metric: <path d="M4 20V10M10 20V4M16 20v-7M22 20H2" />,
  graph: <><circle cx="6" cy="6" r="2.5" /><circle cx="18" cy="8" r="2.5" /><circle cx="9" cy="18" r="2.5" /><path d="M8 7.5 15.6 8M7 8.3 8.5 15.6M16.6 10l-6 6.3" /></>,
  agent: <><rect x="4" y="7" width="16" height="12" rx="3" /><path d="M12 3v4M9 13h.01M15 13h.01M9 16.5h6" /></>,
  play: <><circle cx="12" cy="12" r="9" /><path d="m10 8.5 5.5 3.5-5.5 3.5z" /></>,
  activity: <path d="M3 12h4l3-8 4 16 3-8h4" />,
  check: <><path d="M9 12.5 11 14.5 15.5 10" /><circle cx="12" cy="12" r="9" /></>,
  lock: <><rect x="5" y="10.5" width="14" height="10" rx="2" /><path d="M8 10.5V7.5a4 4 0 0 1 8 0v3" /></>,
  gear: <><circle cx="12" cy="12" r="3" /><path d="M12 2.5v3M12 18.5v3M4.2 6l2.2 1.6M17.6 16.4l2.2 1.6M2.5 12h3M18.5 12h3M4.2 18l2.2-1.6M17.6 7.6 19.8 6" /></>,
  copy: <><rect x="9" y="9" width="11" height="11" rx="2" /><path d="M5 15V5a1 1 0 0 1 1-1h10" /></>,
  plus: <path d="M12 5v14M5 12h14" />,
  upload: <><path d="M12 15V4M7 9l5-5 5 5" /><path d="M5 15v4a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-4" /></>,
  refresh: <><path d="M20 11a8 8 0 0 0-14.9-3.5M4 4v4h4M4 13a8 8 0 0 0 14.9 3.5M20 20v-4h-4" /></>,
  menu: <path d="M4 7h16M4 12h16M4 17h16" />,
  out: <><path d="M15 4h3a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1h-3" /><path d="M10 16l-4-4 4-4M6 12h10" /></>,
  ok: <path d="m5 12.5 4.5 4.5L19 7.5" />,
  x: <path d="M6 6l12 12M18 6 6 18" />,
  download: <><path d="M12 4v11M7 10l5 5 5-5" /><path d="M5 19h14" /></>,
  search: <><circle cx="11" cy="11" r="6.5" /><path d="m20 20-4.2-4.2" /></>,
  key: <><circle cx="8" cy="14" r="4" /><path d="m11 11 9-9M17 5l2 2M15 7l2 2" /></>,
}
export function Icon({ name, size = 18 }: { name: keyof typeof P | string; size?: number }) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{P[name]}</svg>
}

/* ---------------- toast ---------------- */
type ToastFn = (msg: string, bad?: boolean) => void
const ToastCtx = createContext<ToastFn>(() => {})
export const useToast = () => useContext(ToastCtx)
export function ToastProvider({ children }: { children: ReactNode }) {
  const [t, setT] = useState<{ msg: string; bad: boolean } | null>(null)
  const timer = useRef<number>(0)
  const show = useCallback<ToastFn>((msg, bad = false) => {
    setT({ msg, bad }); window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => setT(null), bad ? 6500 : 2600)
  }, [])
  return <ToastCtx.Provider value={show}>{children}{t && <div className={`toast${t.bad ? ' bad' : ''}`} role="status">{t.msg}</div>}</ToastCtx.Provider>
}

/* ---------------- small components ---------------- */
export function PageHead({ title, sub, children }: { title: string; sub?: ReactNode; children?: ReactNode }) {
  return <div className="pagehead"><div><h1>{title}</h1>{sub && <p>{sub}</p>}</div>{children && <div className="row">{children}</div>}</div>
}

export function Badge({ kind = '', children, dot }: { kind?: '' | 'ok' | 'warn' | 'bad' | 'brand'; children: ReactNode; dot?: boolean }) {
  return <span className={`badge ${kind}`}>{dot && <span className="dot" />}{children}</span>
}

export function Empty({ icon, title, children, action }: { icon: string; title: string; children?: ReactNode; action?: ReactNode }) {
  return <div className="empty"><div className="ic"><Icon name={icon} size={22} /></div><h3>{title}</h3>{children && <p>{children}</p>}{action}</div>
}

export function Copy({ value, secret, label = 'Copy' }: { value: string; secret?: boolean; label?: string }) {
  const toast = useToast()
  return <div className="copy"><code className={secret ? 'secret' : ''}>{value}</code>
    <button className="btn" onClick={async () => { try { await navigator.clipboard.writeText(value); toast('Copied') } catch { toast('Copy failed: select the text and copy it', true) } }}>
      <Icon name="copy" size={15} />{label}</button></div>
}

export function CopyBlock({ value }: { value: string }) {
  const toast = useToast()
  return <div style={{ position: 'relative' }}><pre className="code">{value}</pre>
    <button className="btn sm" style={{ position: 'absolute', top: 8, right: 8 }}
      onClick={async () => { try { await navigator.clipboard.writeText(value); toast('Copied') } catch { toast('Copy failed', true) } }}><Icon name="copy" size={14} />Copy</button></div>
}

export function Modal({ title, onClose, children, footer }: { title: string; onClose: () => void; children: ReactNode; footer?: ReactNode }) {
  useEffect(() => {
    const k = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', k); return () => window.removeEventListener('keydown', k)
  }, [onClose])
  return <div className="modal-bg" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose() }}>
    <div className="modal" role="dialog" aria-modal="true" aria-label={title}>
      <div className="hd between"><h2>{title}</h2><button className="btn ghost sm" onClick={onClose} aria-label="Close"><Icon name="x" size={16} /></button></div>
      <div className="bd">{children}</div>{footer && <div className="ft">{footer}</div>}
    </div></div>
}

export function Spinner() { return <span className="spinner" aria-label="Loading" /> }

export function Loading() { return <div className="card"><div className="bd row muted"><Spinner /> Loading…</div></div> }

export function ErrorBox({ error, retry }: { error: string; retry?: () => void }) {
  return <div className="callout bad between"><span>{error}</span>{retry && <button className="btn sm" onClick={retry}>Try again</button>}</div>
}

/* ---------------- data loading hook ---------------- */
export function useLoad<T>(path: string | null, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [n, setN] = useState(0)
  useEffect(() => {
    if (!path) return
    let alive = true
    setError(null)
    api<T>(path).then((d) => { if (alive) setData(d) }).catch((e) => { if (alive) setError(e.message) })
    return () => { alive = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, n, ...deps])
  return { data, error, reload: () => setN((x) => x + 1) }
}

/* ---------------- background jobs ---------------- */
const active = (j: Job) => j.status === 'queued' || j.status === 'running'
export function useJob(onDone?: (j: Job) => void) {
  const [job, setJob] = useState<Job | null>(null)
  const toast = useToast()
  const doneRef = useRef(onDone); doneRef.current = onDone
  useEffect(() => {
    if (!job || !active(job)) return
    const t = window.setInterval(async () => {
      try {
        const { job: j } = await api<{ job: Job }>(`/jobs/${job.id}`)
        setJob(j)
        if (!active(j)) {
          j.status === 'done' ? toast(`${j.label}: done`) : toast(`${j.label} failed: ${j.error}`, true)
          doneRef.current?.(j)
        }
      } catch { /* keep polling */ }
    }, 1200)
    return () => window.clearInterval(t)
  }, [job, toast])
  const start = async (kind: string) => {
    try { const { job: j } = await api<{ job: Job }>(`/jobs/${kind}`, { method: 'POST' }); setJob(j) } catch (e: any) { toast(e.message, true) }
  }
  return { job, start, running: !!job && active(job), attach: setJob }
}

export function JobPanel({ job }: { job: Job | null }) {
  if (!job) return null
  return <div className={`callout ${job.status === 'failed' ? 'bad' : ''}`} style={{ marginBottom: 16 }}>
    <div className="row">{active(job) ? <Spinner /> : job.status === 'done' ? <Badge kind="ok">done</Badge> : <Badge kind="bad">failed</Badge>}
      <b>{job.label}</b>{job.status === 'queued' && <span className="muted small">Waiting for this workspace's current build to finish.</span>}{job.status === 'running' && <span className="muted small">The first run downloads the local search models (about 5 GB) and can take several minutes.</span>}</div>
    {job.error && <div className="small" style={{ marginTop: 6 }}>{job.error}</div>}
    {job.log.length > 0 && <pre className="joblog">{job.log.slice(-12).join('\n')}</pre>}
  </div>
}

export function GroupChips({ groups }: { groups: string[] }) {
  return <span className="row" style={{ gap: 4 }}>{groups.map((g) => <Badge key={g} kind={g === 'public' ? '' : 'warn'}>{g}</Badge>)}</span>
}

export const CAP_LABEL: Record<string, string> = { documents: 'Documents', data: 'Metrics', graph: 'Knowledge graph', upload: 'Add files' }
