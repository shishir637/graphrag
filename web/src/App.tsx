import { useEffect, useState, type FormEvent } from 'react'
import { BrowserRouter, Navigate, NavLink, Route, Routes, useLocation } from 'react-router-dom'
import { api, setUnauthorizedHandler, type User } from './api'
import { Icon, Spinner, ToastProvider } from './ui'
import Overview from './pages/Overview'
import Documents from './pages/Documents'
import DataTables from './pages/DataTables'
import Metrics from './pages/Metrics'
import Graph from './pages/Graph'
import Agents from './pages/Agents'
import Playground from './pages/Playground'
import Activity from './pages/Activity'
import Quality from './pages/Quality'
import Access from './pages/Access'
import Settings from './pages/Settings'

export interface Session { user: User; organisation: string; version: string }

const NAV: { sec?: string; to?: string; label?: string; icon?: string }[] = [
  { to: '/', label: 'Overview', icon: 'home' },
  { sec: 'Sources' }, { to: '/sources/documents', label: 'Documents', icon: 'file' }, { to: '/sources/data', label: 'Data tables', icon: 'table' },
  { sec: 'Knowledge' }, { to: '/knowledge/metrics', label: 'Metrics', icon: 'metric' }, { to: '/knowledge/graph', label: 'Knowledge graph', icon: 'graph' },
  { sec: 'Agents' }, { to: '/agents', label: 'Agents', icon: 'agent' }, { to: '/playground', label: 'Playground', icon: 'play' },
  { sec: 'Govern' }, { to: '/activity', label: 'Activity', icon: 'activity' }, { to: '/quality', label: 'Quality', icon: 'check' }, { to: '/access', label: 'Access', icon: 'lock' },
  { to: '/settings', label: 'Settings', icon: 'gear' },
]

function Shell({ s, onLogout }: { s: Session; onLogout: () => void }) {
  const [open, setOpen] = useState(false)
  const loc = useLocation()
  useEffect(() => setOpen(false), [loc.pathname])
  const initials = s.user.name.split(/\s+/).map((x) => x[0]).join('').slice(0, 2).toUpperCase()
  return <div className={`shell${open ? ' open' : ''}`}>
    <aside className="side" aria-label="Main navigation">
      <div className="brand"><div className="mark"><Icon name="menu" size={17} /></div>
        <div><b>Context Layer</b><span>{s.organisation || 'Your organisation'}</span></div></div>
      {NAV.map((n, i) => n.sec ? <div key={i} className="navsec">{n.sec}</div> :
        <NavLink key={n.to} to={n.to!} end={n.to === '/'} className={({ isActive }) => `nav${isActive ? ' active' : ''}`}><Icon name={n.icon!} />{n.label}</NavLink>)}
      <div className="foot"><div className="avatar">{initials}</div>
        <div className="who"><b>{s.user.name}</b><span>{s.user.role}</span></div>
        <button className="btn ghost sm" onClick={onLogout} title="Sign out" aria-label="Sign out"><Icon name="out" size={16} /></button></div>
    </aside>
    <div className="main">
      <div className="topbar"><button className="btn ghost sm" onClick={() => setOpen(!open)} aria-label="Menu"><Icon name="menu" /></button><b>Context Layer</b></div>
      <Routes>
        <Route path="/" element={<Overview s={s} />} />
        <Route path="/sources/documents" element={<Documents s={s} />} />
        <Route path="/sources/data" element={<DataTables s={s} />} />
        <Route path="/knowledge/metrics" element={<Metrics />} />
        <Route path="/knowledge/graph" element={<Graph s={s} />} />
        <Route path="/agents" element={<Agents s={s} />} />
        <Route path="/playground" element={<Playground />} />
        <Route path="/activity" element={<Activity s={s} />} />
        <Route path="/quality" element={<Quality s={s} />} />
        <Route path="/access" element={<Access s={s} />} />
        <Route path="/settings" element={<Settings s={s} />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </div>
    {open && <div className="modal-bg" style={{ zIndex: 25, background: 'rgb(0 0 0 / .3)' }} onClick={() => setOpen(false)} />}
  </div>
}

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
      <label className="f">Password<span className="h">At least 10 characters.</span><input type="password" required minLength={10} value={f.password} onChange={set('password')} autoComplete="new-password" /></label>
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
  const [state, setState] = useState<{ phase: 'loading' | 'setup' | 'login' | 'in'; s?: Session; org?: string }>({ phase: 'loading' })
  const load = async () => {
    try {
      const st = await api<{ needs_setup: boolean; organisation: string }>('/setup')
      if (st.needs_setup) return setState({ phase: 'setup' })
      try { const me = await api<Session>('/me'); setState({ phase: 'in', s: me }) } catch { setState({ phase: 'login', org: st.organisation }) }
    } catch { setState({ phase: 'login' }) }
  }
  useEffect(() => { setUnauthorizedHandler(() => setState((s) => (s.phase === 'in' ? { phase: 'login', org: s.s?.organisation } : s))); load() }, [])
  const logout = async () => { await api('/logout', { method: 'POST' }).catch(() => {}); setState({ phase: 'login', org: state.s?.organisation }) }
  return <ToastProvider>
    {state.phase === 'loading' && <div className="auth"><Spinner /></div>}
    {state.phase === 'setup' && <Setup onDone={load} />}
    {state.phase === 'login' && <Login onDone={load} org={state.org || ''} />}
    {state.phase === 'in' && state.s && <BrowserRouter><Shell s={state.s} onLogout={logout} /></BrowserRouter>}
  </ToastProvider>
}
