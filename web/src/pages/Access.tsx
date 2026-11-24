import { useState } from 'react'
import { ago, api, can, type Role, type User } from '../api'
import type { Session } from '../App'
import { Badge, ErrorBox, Icon, Loading, Modal, PageHead, useLoad, useToast } from '../ui'

const ROLE_HELP: Record<Role, string> = { owner: 'Everything, including team and settings', admin: 'Sources, agents and quality', viewer: 'Read-only; can use the Playground' }

export default function Access({ s }: { s: Session }) {
  const groups = useLoad<{ groups: { name: string; documents: string[]; agents: string[] }[] }>('/groups')
  const users = useLoad<{ users: User[] }>('/users')
  const [add, setAdd] = useState(false); const toast = useToast(); const owner = can(s.user, 'owner')
  const patch = async (u: User, body: Partial<{ role: Role; disabled: boolean }>) => {
    try { await api(`/users/${u.id}`, { method: 'PATCH', json: body }); users.reload(); toast('Saved') } catch (e: any) { toast(e.message, true) }
  }
  return <div className="page">
    <PageHead title="Access" sub="Access groups decide which documents an agent can see. Team roles decide what people can do in Context Layer." />
    <div className="card"><div className="hd"><h2>Access groups</h2><span className="muted small">Create a group by tagging a document with it</span></div>
      {groups.error ? <div className="bd"><ErrorBox error={groups.error} /></div> : !groups.data ? <div className="bd"><Loading /></div> :
        <div className="tablewrap"><table className="t"><thead><tr><th>Group</th><th>Documents</th><th>Agents that can read them</th></tr></thead>
          <tbody>{groups.data.groups.map((g) => <tr key={g.name}><td><Badge kind={g.name === 'public' ? '' : 'warn'}>{g.name}</Badge></td>
            <td className="small">{g.documents.length ? <>{g.documents.slice(0, 4).join(', ')}{g.documents.length > 4 && <span className="muted"> +{g.documents.length - 4} more</span>}</> : <span className="muted">none</span>}</td>
            <td className="small">{g.agents.length ? g.agents.join(', ') : <span className="muted">none</span>}</td></tr>)}</tbody></table></div>}</div>
    <div className="card"><div className="hd"><h2>Team</h2>{owner && <button className="btn sm" onClick={() => setAdd(true)}><Icon name="plus" size={14} />Add member</button>}</div>
      {users.error ? <div className="bd"><ErrorBox error={users.error} /></div> : !users.data ? <div className="bd"><Loading /></div> :
        <div className="tablewrap"><table className="t"><thead><tr><th>Member</th><th>Role</th><th>Last sign-in</th><th /></tr></thead>
          <tbody>{users.data.users.map((u) => <tr key={u.id} className={u.disabled ? 'dim' : ''}><td><b>{u.name}</b>{u.id === s.user.id && <span className="muted"> (you)</span>}<div className="tiny muted">{u.email}</div></td>
            <td>{owner && u.id !== s.user.id ? <select value={u.role} onChange={(e) => patch(u, { role: e.target.value as Role })} style={{ width: 130 }} aria-label={`Role of ${u.name}`}>
              {(['owner', 'admin', 'viewer'] as Role[]).map((r) => <option key={r} value={r}>{r}</option>)}</select> : <Badge>{u.role}</Badge>}</td>
            <td className="small muted">{ago(u.last_login)}</td>
            <td style={{ textAlign: 'right' }}>{owner && u.id !== s.user.id && <button className="btn sm" onClick={() => patch(u, { disabled: !u.disabled })}>{u.disabled ? 'Enable' : 'Disable'}</button>}</td></tr>)}</tbody></table></div>}
      <div className="bd tiny muted" style={{ borderTop: '1px solid var(--line)' }}>{(['owner', 'admin', 'viewer'] as Role[]).map((r) => <div key={r}><b>{r}</b>: {ROLE_HELP[r]}</div>)}</div></div>
    {add && <AddUser onClose={() => setAdd(false)} onDone={() => { setAdd(false); users.reload(); toast('Member added. Share their temporary password with them securely.') }} />}
  </div>
}

function AddUser({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [f, setF] = useState({ name: '', email: '', role: 'viewer', password: '' }); const toast = useToast()
  return <Modal title="Add a team member" onClose={onClose} footer={<><button className="btn" onClick={onClose}>Cancel</button>
    <button className="btn primary" disabled={!f.name || !f.email || f.password.length < 10} onClick={async () => { try { await api('/users', { json: f }); onDone() } catch (e: any) { toast(e.message, true) } }}>Add member</button></>}>
    <div className="stack">
      <label className="f">Name<input type="text" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></label>
      <label className="f">Email<input type="email" value={f.email} onChange={(e) => setF({ ...f, email: e.target.value })} /></label>
      <label className="f">Role<select value={f.role} onChange={(e) => setF({ ...f, role: e.target.value })}>{(['viewer', 'admin', 'owner'] as Role[]).map((r) => <option key={r} value={r}>{r}: {ROLE_HELP[r]}</option>)}</select></label>
      <label className="f">Temporary password<span className="h">At least 10 characters. They can change it under Settings after signing in.</span><input type="text" value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} autoComplete="off" /></label>
    </div></Modal>
}
