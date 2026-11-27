import { useState } from 'react'
import { ago, api } from '../api'
import { Checks } from '../admin'
import { Copy, Icon, Loading, Modal, askConfirm, useLoad, useToast } from '../ui'

/* Keys for apps: an app (e.g. CodeVerse) calls Context Layer with a key. An app key acts for the app; a key that
   acts for the app's own users proves who each user is on every call and gets only that user's access. Keys can
   expire and be limited to IP ranges. */
interface Key { id: string; name: string; prefix: string; scope: 'agent' | 'delegate'; groups: string[]; capabilities: string[]; expires: string | null
  allowed_ips: string[]; created: string; last_used: string | null; calls: number; revoked: string | null }
interface AppUser { user_id: string; name: string; datasets: string[]; roles: string[]; disabled: boolean; updated: string }
interface Opts { datasets: { name: string; label: string }[]; roles: { name: string }[]; connectors: Record<string, string> }

export default function AppKeys() {
  const k = useLoad<{ agents: Key[] }>('/agents'); const users = useLoad<{ users: AppUser[] }>('/external-users')
  const acc = useLoad<Opts>('/access'); const toast = useToast()
  const [add, setAdd] = useState(false); const [shown, setShown] = useState<{ token: string; signing_secret?: string } | null>(null)
  const [user, setUser] = useState<AppUser | 'new' | null>(null)
  if (k.error) return <div className="callout bad">{k.error}</div>
  if (!k.data || !acc.data) return <Loading />
  const keys = k.data.agents.filter((x) => !x.revoked)
  const revoke = async (x: Key) => {
    if (!(await askConfirm(`Revoke ${x.name}? The app stops working until it gets a new key.`, 'Revoke'))) return
    try { await api(`/agents/${x.id}/revoke`, { method: 'POST' }); k.reload() } catch (e: any) { toast(e.message, true) }
  }
  return <div style={{ maxWidth: 900, marginTop: 28 }}>
    <div className="row" style={{ justifyContent: 'space-between' }}><h2 style={{ margin: 0, fontSize: 18 }}>Keys for apps</h2>
      <button className="btn sm primary" onClick={() => setAdd(true)}><Icon name="plus" size={14} />New key</button></div>
    <p className="tiny muted">For apps that call Context Layer from their own server. A key that acts for an app's users gets, on each call, only the access of the user it names.</p>
    <div className="card"><table className="t plain"><tbody>{keys.map((x) => <tr key={x.id}>
      <td className="small"><b>{x.name}</b><div className="tiny muted">{x.prefix}… · {x.scope === 'delegate' ? 'acts for its users' : 'app key'}</div></td>
      <td className="small">{x.expires ? `expires ${x.expires.slice(0, 10)}` : 'no expiry'}{x.allowed_ips.length > 0 && <div className="tiny muted">only from {x.allowed_ips.join(', ')}</div>}</td>
      <td className="small muted">{x.last_used ? `used ${ago(x.last_used)}` : 'not used yet'} · {x.calls} calls</td>
      <td style={{ textAlign: 'right' }}><button className="btn ghost sm" onClick={() => revoke(x)}>Revoke</button></td></tr>)}
      {!keys.length && <tr><td className="muted small" style={{ padding: 18 }}>No keys yet.</td></tr>}</tbody></table></div>
    <div className="row" style={{ justifyContent: 'space-between', marginTop: 18 }}><h3 style={{ margin: 0, fontSize: 15 }}>App users</h3>
      <button className="btn sm" onClick={() => setUser('new')}><Icon name="plus" size={14} />Add app user</button></div>
    <p className="tiny muted">The users an app acts for, by the id the app uses for them, with the data sets and roles they get.</p>
    <div className="card"><table className="t plain"><tbody>{(users.data?.users || []).map((u) => <tr key={u.user_id}>
      <td className="small"><b>{u.name || u.user_id}</b><div className="tiny muted">{u.user_id}{u.disabled ? ' · disabled' : ''}</div></td>
      <td className="small">{[...u.datasets, ...u.roles.map((r) => `role ${r}`)].join(', ') || <span className="muted">nothing</span>}</td>
      <td style={{ textAlign: 'right' }}><button className="btn ghost sm" onClick={() => setUser(u)}>Change</button></td></tr>)}
      {!users.data?.users.length && <tr><td className="muted small" style={{ padding: 18 }}>No app users yet.</td></tr>}</tbody></table></div>
    {add && <NewKey o={acc.data} onClose={() => setAdd(false)} onDone={(r) => { setAdd(false); setShown(r); k.reload() }} />}
    {shown && <Modal title="Copy these now" onClose={() => setShown(null)} footer={<button className="btn primary" onClick={() => setShown(null)}>Done</button>}>
      <p className="small">They are shown only once. Keep them on the app's server, never in a browser.</p>
      <span className="flabel">Key</span><Copy value={shown.token} label="Copy" />
      {shown.signing_secret && <><span className="flabel" style={{ marginTop: 10 }}>Signing secret (to prove each user's identity)</span><Copy value={shown.signing_secret} label="Copy" /></>}
    </Modal>}
    {user && <UserForm o={acc.data} user={user === 'new' ? null : user} onClose={() => setUser(null)} onDone={() => { setUser(null); users.reload() }} />}
  </div>
}

function NewKey({ o, onClose, onDone }: { o: Opts; onClose: () => void; onDone: (r: { token: string; signing_secret?: string }) => void }) {
  const [name, setName] = useState(''); const [scope, setScope] = useState<'agent' | 'delegate'>('delegate')
  const [ds, setDs] = useState<string[]>(['public']); const [cons, setCons] = useState<string[]>(['documents', 'data'])
  const [days, setDays] = useState(''); const [ips, setIps] = useState(''); const toast = useToast()
  const save = async () => {
    try {
      const r = await api<any>('/agents', { json: { name, scope, groups: ds, capabilities: cons, expires_days: days ? Number(days) : null,
        allowed_ips: ips.split(',').map((x) => x.trim()).filter(Boolean) } })
      onDone({ token: r.token, signing_secret: r.signing_secret })
    } catch (e: any) { toast(e.message, true) }
  }
  return <Modal title="New key for an app" onClose={onClose} footer={<><button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" disabled={!name.trim()} onClick={save}>Create</button></>}>
    <label className="f">App name<input type="text" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. CodeVerse" autoFocus /></label>
    <span className="flabel">The key</span>
    <div className="seg" style={{ marginBottom: 10 }}><button className={scope === 'delegate' ? 'on' : ''} onClick={() => setScope('delegate')}>Acts for the app's users</button>
      <button className={scope === 'agent' ? 'on' : ''} onClick={() => setScope('agent')}>Acts for the app itself</button></div>
    <span className="flabel">{scope === 'delegate' ? 'Most it may ever see (each user gets their own part of this)' : 'What it may see'}</span>
    <Checks options={o.datasets.map((d) => ({ v: d.name, label: d.label }))} value={ds} onChange={setDs} />
    <span className="flabel" style={{ marginTop: 10 }}>What it may use</span>
    <Checks options={Object.entries(o.connectors).filter(([c]) => c !== 'upload').map(([c, l]) => ({ v: c, label: l }))} value={cons} onChange={setCons} />
    <div className="row" style={{ gap: 10, marginTop: 10 }}>
      <label className="f" style={{ flex: 1 }}>Expires after (days)<input type="number" min={1} value={days} onChange={(e) => setDays(e.target.value)} placeholder="never" /></label>
      <label className="f" style={{ flex: 2 }}>Only from these addresses<input type="text" value={ips} onChange={(e) => setIps(e.target.value)} placeholder="e.g. 203.0.113.0/24 (empty: anywhere)" /></label></div>
  </Modal>
}

function UserForm({ o, user, onClose, onDone }: { o: Opts; user: AppUser | null; onClose: () => void; onDone: () => void }) {
  const [uid, setUid] = useState(user?.user_id || ''); const [name, setName] = useState(user?.name || '')
  const [ds, setDs] = useState<string[]>(user?.datasets || ['public']); const [roles, setRoles] = useState<string[]>(user?.roles || [])
  const [disabled, setDisabled] = useState(user?.disabled || false); const toast = useToast()
  const save = async () => {
    try { await api(`/external-users/${encodeURIComponent(uid.trim())}`, { method: 'PUT', json: { name, datasets: ds, roles, disabled } }); onDone() } catch (e: any) { toast(e.message, true) }
  }
  const del = async () => {
    if (!user || !(await askConfirm(`Remove ${user.name || user.user_id}? Calls for them are refused.`, 'Remove'))) return
    try { await api(`/external-users/${encodeURIComponent(user.user_id)}`, { method: 'DELETE' }); onDone() } catch (e: any) { toast(e.message, true) }
  }
  return <Modal title={user ? user.name || user.user_id : 'Add app user'} onClose={onClose} footer={<>{user && <button className="btn ghost" style={{ marginRight: 'auto' }} onClick={del}>Remove</button>}
    <button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" disabled={!uid.trim()} onClick={save}>Save</button></>}>
    <label className="f">User id in the app<input type="text" value={uid} disabled={!!user} onChange={(e) => setUid(e.target.value)} placeholder="e.g. u-1042" /></label>
    <label className="f">Name<input type="text" value={name} onChange={(e) => setName(e.target.value)} /></label>
    <span className="flabel">Data sets</span><Checks options={o.datasets.map((d) => ({ v: d.name, label: d.label }))} value={ds} onChange={setDs} />
    <span className="flabel" style={{ marginTop: 10 }}>Roles</span><Checks options={o.roles.map((r) => ({ v: r.name, label: r.name }))} value={roles} onChange={setRoles} empty="No roles yet (Access tab)." />
    <label className="row small" style={{ gap: 6, marginTop: 10 }}><input type="checkbox" checked={disabled} onChange={(e) => setDisabled(e.target.checked)} />Disabled</label>
  </Modal>
}
