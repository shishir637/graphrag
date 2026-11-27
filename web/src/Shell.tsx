/* The admin app: a top bar with a few tabs. Members get one page. */
import { useEffect, useState } from 'react'
import type { Session } from './App'
import { Icon, Loading, useLoad } from './ui'
import UsersPage, { Member, type Home } from './pages/Users'
import DataPage from './pages/DataPage'
import TeamsPage from './pages/TeamsPage'
import AccessPage from './pages/AccessPage'
import ActivityPage from './pages/ActivityPage'
import ApprovalsPage from './pages/ApprovalsPage'
import SettingsPage from './pages/SettingsPage'

type Tab = 'users' | 'teams' | 'approvals' | 'data' | 'access' | 'activity' | 'settings'
const TABS: { t: Tab; label: string; icon: string; company?: boolean }[] = [
  { t: 'users', label: 'Users', icon: 'agent' }, { t: 'teams', label: 'Teams', icon: 'graph' }, { t: 'data', label: 'Data', icon: 'file' },
  { t: 'access', label: 'Access', icon: 'lock' }, { t: 'approvals', label: 'Approvals', icon: 'check' }, { t: 'activity', label: 'Activity', icon: 'activity' }, { t: 'settings', label: 'Settings', icon: 'gear', company: true }]

const readCollapsed = () => { try { return localStorage.getItem('cl.nav') === 'small' } catch { return false } }

const fromHash = (): Tab => (TABS.find((x) => `#${x.t}` === location.hash.split('?')[0])?.t || 'users')

export default function Shell({ s, onLogout }: { s: Session; onLogout: () => void }) {
  const h = useLoad<Home>('/home')
  const [tab, setTab] = useState<Tab>(fromHash)
  useEffect(() => { const f = () => setTab(fromHash()); window.addEventListener('hashchange', f); return () => window.removeEventListener('hashchange', f) }, [])
  const go = (t: Tab) => { location.hash = t; setTab(t) }
  const admin = !!h.data && (h.data.company_admin || h.data.manages.length > 0)
  const waiting = h.data ? (h.data as any).waiting_for_me?.filter((w: any) => w.can_decide).length || 0 : 0
  const tabs = TABS.filter((x) => !x.company || h.data?.company_admin)
  useEffect(() => { if (h.data && !tabs.some((x) => x.t === tab)) setTab('users') }, [h.data, tab])
  const [small, setSmall] = useState(readCollapsed)
  const toggle = () => { const v = !small; setSmall(v); try { localStorage.setItem('cl.nav', v ? 'small' : 'big') } catch { /* private mode */ } }
  const initials = s.user.name.split(' ').map((w) => w[0]).slice(0, 2).join('').toUpperCase()
  if (h.data && !admin) return <div className="one">
    <header className="onebar">
      <div className="row" style={{ gap: 10 }}><div className="mark"><Icon name="menu" size={16} /></div><b>{s.organisation || 'Context Layer'}</b></div>
      <div className="row" style={{ gap: 12 }}><span className="small muted">{s.user.name}</span>
        <button className="btn ghost sm" onClick={onLogout}><Icon name="out" size={15} />Sign out</button></div>
    </header>
    <main className="onemain"><Member home={h.data} /></main>
  </div>
  return <div className={`app2${small ? ' small' : ''}`}>
    <aside className="nav2">
      <div className="nav2top">
        <div className="mark" title={s.organisation}><b>{(s.organisation || 'C')[0]}</b></div>
        {!small && <b className="ellipsis">{s.organisation || 'Context Layer'}</b>}
        <button className="nav2toggle" onClick={toggle} aria-label={small ? 'Expand menu' : 'Collapse menu'} title={small ? 'Expand' : 'Collapse'}>{small ? '»' : '«'}</button>
      </div>
      <nav>{tabs.map((x) => <button key={x.t} className={tab === x.t ? 'on' : ''} onClick={() => go(x.t)} title={small ? x.label : undefined}>
        <Icon name={x.icon} size={17} />{!small && <span>{x.label}</span>}
        {x.t === 'approvals' && waiting > 0 && <span className="tabcount">{waiting}</span>}</button>)}</nav>
      <div className="nav2user">
        <div className="avatar" title={s.user.name}>{initials}</div>
        {!small && <div style={{ minWidth: 0, flex: 1 }}><div className="small ellipsis"><b>{s.user.name}</b></div><div className="tiny muted ellipsis">{h.data?.company_admin ? 'Company admin' : 'Team admin'}</div></div>}
        <button className="btn ghost sm" onClick={onLogout} title="Sign out" aria-label="Sign out"><Icon name="out" size={15} /></button>
      </div>
    </aside>
    <main className="onemain">
      {!h.data ? <Loading /> :
        tab === 'users' ? <UsersPage home={h.data} /> : tab === 'teams' ? <TeamsPage /> : tab === 'data' ? <DataPage /> : tab === 'access' ? <AccessPage /> :
          tab === 'approvals' ? <ApprovalsPage onChange={h.reload} /> : tab === 'activity' ? <ActivityPage onChange={h.reload} /> : <SettingsPage />}
    </main>
  </div>
}
