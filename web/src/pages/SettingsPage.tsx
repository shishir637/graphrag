import { useState } from 'react'
import { api } from '../api'
import { Copy, Loading, useLoad, useToast } from '../ui'
import AppKeys from './AppKeys'

interface S { organisation: string; link: string; session_idle_minutes: number; session_hours: number; min_password: number; version: string; apps: number; users: number }

/* The few things a company admin sets once. */
export default function SettingsPage() {
  const d = useLoad<S>('/admin/settings'); const toast = useToast()
  const [org, setOrg] = useState<string | null>(null)
  if (!d.data) return d.error ? <div className="callout bad">{d.error}</div> : <Loading />
  const x = d.data
  const saveOrg = async () => { try { await api('/settings/organisation', { method: 'PUT', json: { organisation: org } }); toast('Saved'); d.reload(); setOrg(null) } catch (e: any) { toast(e.message, true) } }
  const job = async (kind: string, label: string) => { try { await api(`/jobs/${kind}`, { method: 'POST' }); toast(`${label} started. It runs in the background.`) } catch (e: any) { toast(e.message, true) } }
  return <div style={{ maxWidth: 720 }}>
    <h1 style={{ marginTop: 0 }}>Settings</h1>
    <div className="card"><div className="list">
      <div className="li"><div><b className="small">Company name</b><div className="tiny muted">Shown on sign-in and in Claude.</div></div>
        <div className="row">{org === null ? <><span className="small">{x.organisation || '—'}</span><button className="btn sm" onClick={() => setOrg(x.organisation)}>Change</button></>
          : <><input type="text" value={org} onChange={(e) => setOrg(e.target.value)} style={{ width: 220 }} autoFocus aria-label="Company name" /><button className="btn sm primary" onClick={saveOrg}>Save</button></>}</div></div>
      <div className="li" style={{ display: 'block' }}><b className="small">Company link for Claude and ChatGPT</b>
        <div className="tiny muted" style={{ margin: '2px 0 8px' }}>People add it as a connector and sign in. Each sees only what their access allows. Each team also has its own link (Users tab, Teams).</div>
        <Copy value={x.link} label="Copy" /></div>
      <div className="li"><div><b className="small">Sign-in rules</b><div className="tiny muted">Passwords: at least {x.min_password} characters, checked against known breaches. Web sessions end after {x.session_idle_minutes} minutes idle or {x.session_hours} hours.</div></div></div>
      <div className="li"><div><b className="small">Prepare everything again</b><div className="tiny muted">Re-reads files, data tables and the knowledge graph. Needed after many changes; takes a while.</div></div>
        <div className="row" style={{ flexWrap: 'nowrap' }}><button className="btn sm" onClick={() => job('documents', 'Files')}>Files</button><button className="btn sm" onClick={() => job('data', 'Tables')}>Tables</button><button className="btn sm" onClick={() => job('graph', 'Graph')}>Graph</button></div></div>
      <div className="li"><div><b className="small">Version</b></div><span className="small muted">{x.version} · {x.users} accounts · {x.apps} connected apps</span></div>
    </div></div>
    <AppKeys />
  </div>
}
