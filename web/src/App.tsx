/* Context Layer web app, v0.7: one page for now. Admins add users, set up teams and team admins; team admins add
   and remove people in their teams; everyone else sees their team and their link. (The older admin pages are
   still in src/pages but not shown.) */
import { useEffect, useState, type FormEvent } from 'react'
import { api, setMcpUrl, setUnauthorizedHandler, type User } from './api'
import { Icon, Spinner, ToastProvider } from './ui'
import Shell from './Shell'

export interface Session { user: User; organisation: string; version: string }

function AuthCard({ title, sub, children }: { title: string; sub: string; children: React.ReactNode }) {
  return <div className="auth"><div className="card">
    <div className="brand" style={{ padding: '0 0 18px' }}><div className="mark"><Icon name="menu" size={17} /></div><div><b>Context Layer</b><span>Governed company knowledge for AI agents</span></div></div>
    <h1 style={{ fontSize: 20 }}>{title}</h1><p className="muted" style={{ margin: '6px 0 18px' }}>{sub}</p>{children}</div></div>
}

function Setup({ onDone }: { onDone: () => void }) {
  const [f, setF] = useState({ organisation: '', name: '', email: '', password: '' })
  const [err, setErr] = useState(''); const [busy, setBusy] = useState(false)
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setBusy(true); setErr('')
    try { await api('/setup', { json: f }); onDone() } catch (x: any) { setErr(x.message) } finally { setBusy(false) }
  }
  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement>) => setF({ ...f, [k]: e.target.value })
  return <AuthCard title="Set up Context Layer" sub="Create the owner account for this installation. You can invite your team afterwards.">
    <form className="stack" onSubmit={submit}>
      <label className="f">Organisation<input type="text" value={f.organisation} onChange={set('organisation')} placeholder="Acme Pvt Ltd" /></label>
      <label className="f">Your name<input type="text" required value={f.name} onChange={set('name')} autoComplete="name" /></label>
      <label className="f">Work email<input type="email" required value={f.email} onChange={set('email')} autoComplete="email" /></label>
      <label className="f">Password<span className="h">At least 15 characters. A short sentence you will remember works well.</span><input type="password" required minLength={15} value={f.password} onChange={set('password')} autoComplete="new-password" /></label>
      {err && <div className="callout bad">{err}</div>}
      <button className="btn primary" style={{ width: '100%', justifyContent: 'center' }} disabled={busy}>{busy ? <Spinner /> : 'Create owner account'}</button>
    </form></AuthCard>
}

function Login({ onDone, org }: { onDone: () => void; org: string }) {
  const [email, setEmail] = useState(''); const [password, setPassword] = useState('')
  const [err, setErr] = useState(''); const [busy, setBusy] = useState(false)
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setBusy(true); setErr('')
    try { await api('/login', { json: { email, password } }); onDone() } catch (x: any) { setErr(x.message) } finally { setBusy(false) }
  }
  return <AuthCard title="Sign in" sub={org ? `to ${org}` : 'to your Context Layer'}>
    <form className="stack" onSubmit={submit}>
      <label className="f">Email<input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="username" autoFocus /></label>
      <label className="f">Password<input type="password" required value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" /></label>
      {err && <div className="callout bad">{err}</div>}
      <button className="btn primary" style={{ width: '100%', justifyContent: 'center' }} disabled={busy}>{busy ? <Spinner /> : 'Sign in'}</button>
      <p className="tiny muted" style={{ textAlign: 'center' }}>Forgot your password? Ask an owner of this installation to reset your account.</p>
    </form></AuthCard>
}

export default function App() {
  const [state, setState] = useState<{ phase: 'loading' | 'setup' | 'login' | 'in' | 'guest'; s?: Session; org?: string }>({ phase: 'loading' })
  const load = async () => {
    try {
      const st = await api<{ needs_setup: boolean; organisation: string; guest_chat: boolean }>('/setup')
      if (st.needs_setup) return setState({ phase: 'setup' })
      try {
        const me = await api<Session>('/me'); setState({ phase: 'in', s: me })
        api<{ mcp: { public_url: string | null } }>('/settings').then((x) => { if (x.mcp.public_url) setMcpUrl(x.mcp.public_url) }).catch(() => {})
      } catch { setState({ phase: 'login', org: st.organisation }) }
    } catch { setState({ phase: 'login' }) }
  }
  useEffect(() => { setUnauthorizedHandler(() => setState((s) => (s.phase === 'in' ? { phase: 'login', org: s.s?.organisation } : s))); load() }, [])
  const logout = async () => { await api('/logout', { method: 'POST' }).catch(() => {}); setState({ phase: 'login', org: state.s?.organisation }) }
  return <ToastProvider>
    {state.phase === 'loading' && <div className="auth"><Spinner /></div>}
    {state.phase === 'setup' && <Setup onDone={load} />}
    {state.phase === 'login' && <Login onDone={load} org={state.org || ''} />}
    {state.phase === 'in' && state.s && <Shell s={state.s} onLogout={logout} />}
  </ToastProvider>
}
