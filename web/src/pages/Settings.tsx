import { useState } from 'react'
import { api, can } from '../api'
import type { Session } from '../App'
import { Badge, ErrorBox, Loading, PageHead, useLoad, useToast } from '../ui'

interface S {
  organisation: string; version: string
  ai_model: { inside: boolean; note: string }; mcp: { url: string; transport: string; auth: string }
  models: Record<string, string>; graph_mode: string
  storage: { data_folder: string; graph_database: string; graph_database_ok: boolean; graph_database_message: string; disk: { free_gb: number; total_gb: number } }
}

export default function Settings({ s }: { s: Session }) {
  const { data, error, reload } = useLoad<S>('/settings')
  const [org, setOrg] = useState<string | null>(null)
  const [pw, setPw] = useState({ current: '', new: '' }); const toast = useToast(); const owner = can(s.user, 'owner')
  if (error) return <div className="page"><ErrorBox error={error} retry={reload} /></div>
  if (!data) return <div className="page"><Loading /></div>
  const setMode = async (mode: string) => {
    try { await api('/settings/graph', { method: 'PUT', json: { mode } }); reload(); toast('Saved: rebuild the graph to apply') } catch (e: any) { toast(e.message, true) }
  }
  return <div className="page">
    <PageHead title="Settings" sub={`Context Layer ${data.version}`} />
    <div className="card"><div className="hd"><div><h2>How agents connect</h2><div className="muted small">{data.ai_model.note}</div></div>
      <Badge kind="ok" dot>no AI model needed</Badge></div>
      <div className="bd stack small">
        <div className="between"><span className="muted">MCP link</span><code>{data.mcp.url}</code></div>
        <div className="between"><span className="muted">Transport</span><span>{data.mcp.transport}</span></div>
        <div className="between"><span className="muted">Sign-in</span><code>{data.mcp.auth}</code></div>
        <div className="muted">Create a key per agent under <a href="/agents">Agents</a>; each key decides which documents and tools the agent can use.</div>
        <div className="sep" /><b>Models (all run on this server)</b>
        {Object.entries(data.models).map(([k, v]) => <div key={k} className="between"><span className="muted">{k}</span><code>{v}</code></div>)}
        <div className="sep" /><div className="between" style={{ alignItems: 'center' }}><span><b>Knowledge graph quality</b><div className="muted">Accurate: GLiNER + spaCy (about 1-2 s per passage on a laptop CPU). Fast: spaCy only (about 0.05 s per passage).</div></span>
          <div className="seg" role="group" aria-label="Graph mode">{['accurate', 'fast'].map((m) => <button key={m} type="button" disabled={!owner} className={data.graph_mode === m ? 'on' : ''} onClick={() => setMode(m)}>{m}</button>)}</div></div>
      </div></div>
    <div className="grid g2" style={{ marginTop: 16 }}>
      <div className="card"><div className="hd"><h2>Organisation</h2></div><div className="bd stack">
        <label className="f">Name<input type="text" value={org ?? data.organisation} onChange={(e) => setOrg(e.target.value)} disabled={!owner} /></label>
        {owner && <div><button className="btn" disabled={org === null} onClick={async () => { try { await api('/settings/organisation', { method: 'PUT', json: { organisation: org } }); toast('Saved'); setOrg(null); reload() } catch (e: any) { toast(e.message, true) } }}>Save</button></div>}
      </div></div>
      <div className="card"><div className="hd"><h2>Storage</h2></div><div className="bd stack small">
        <div className="between"><span className="muted">Data folder</span><code>{data.storage.data_folder}</code></div>
        <div className="between"><span className="muted">Graph database</span>{data.storage.graph_database_ok ? <Badge kind="ok" dot>online</Badge> : <Badge kind="warn" dot>offline</Badge>}</div>
        {!data.storage.graph_database_ok && <div className="tiny muted">{data.storage.graph_database_message}</div>}
        <div className="between"><span className="muted">Free disk</span><span>{data.storage.disk.free_gb} GB of {data.storage.disk.total_gb} GB</span></div>
      </div></div>
    </div>
    <div className="card" style={{ marginTop: 16 }}><div className="hd"><h2>Your password</h2></div><div className="bd row" style={{ alignItems: 'flex-end' }}>
      <label className="f" style={{ flex: '1 1 220px' }}>Current<input type="password" value={pw.current} onChange={(e) => setPw({ ...pw, current: e.target.value })} autoComplete="current-password" /></label>
      <label className="f" style={{ flex: '1 1 220px' }}>New (10+ characters)<input type="password" value={pw.new} onChange={(e) => setPw({ ...pw, new: e.target.value })} autoComplete="new-password" /></label>
      <button className="btn" disabled={!pw.current || pw.new.length < 10} onClick={async () => { try { await api('/me/password', { json: pw }); setPw({ current: '', new: '' }); toast('Password changed') } catch (e: any) { toast(e.message, true) } }}>Change password</button>
    </div></div>
  </div>
}
