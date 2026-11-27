import { useState } from 'react'
import { api } from '../api'
import type { Session } from '../App'
import { Badge, Copy, Empty, ErrorBox, Icon, Loading, Modal, PageHead, useLoad, useToast } from '../ui'

interface Person { email: string; name: string; title: string; team: string | null; manager_email: string | null; location: string; status: string; has_account: boolean }
interface Team { name: string; lead_email: string | null; parent: string | null; description: string; members: number }

const SAMPLE = 'name,email,title,team,manager_email,location\nPriya Nair,priya@example.com,Data Engineer,Data Engineering,meera@example.com,Pune'

export default function Directory({ s }: { s: Session }) {
  const [q, setQ] = useState(''); const [team, setTeam] = useState(''); const [left, setLeft] = useState(false)
  const d = useLoad<{ people: Person[]; teams: Team[] }>(`/directory?q=${encodeURIComponent(q)}&team=${encodeURIComponent(team)}&left=${left}`, [q, team, left])
  const [imp, setImp] = useState(false); const [inv, setInv] = useState(false); const [edit, setEdit] = useState<Partial<Person> | null>(null)
  const [tEdit, setTEdit] = useState<Partial<Team> | null>(null); const toast = useToast()
  const names = Object.fromEntries((d.data?.people || []).map((p) => [p.email, p.name]))
  return <div className="page">
    <PageHead title="Directory" sub={<>People and teams of {s.organisation || 'the company'}. Teams are access groups: they decide who sees team documents and who a layer can be shared with. Claude reads names, titles, teams and managers through <code>find_person</code> and <code>list_teams</code>.</>}>
      <button className="btn sm" onClick={() => setInv(true)}><Icon name="plus" size={14} />Invite</button>
      <button className="btn sm" onClick={() => setImp(true)}><Icon name="upload" size={14} />Import CSV</button>
      <button className="btn sm primary" onClick={() => setEdit({ status: 'active' })}><Icon name="plus" size={14} />Add person</button>
    </PageHead>
    <div className="card" style={{ marginBottom: 16 }}><div className="hd"><h2>Teams</h2><button className="btn sm" onClick={() => setTEdit({})}><Icon name="plus" size={14} />Add team</button></div>
      {!d.data ? <div className="bd"><Loading /></div> : !d.data.teams.length ? <div className="bd"><Empty icon="agent" title="No teams yet">Add teams, or import people with a team column.</Empty></div> :
        <div className="tablewrap"><table className="t"><thead><tr><th>Team</th><th>Lead</th><th>Part of</th><th>People</th><th /></tr></thead>
          <tbody>{d.data.teams.map((t) => <tr key={t.name}><td><b>{t.name}</b>{t.description && <div className="tiny muted">{t.description}</div>}</td>
            <td className="small">{t.lead_email ? names[t.lead_email] || t.lead_email : <span className="muted">none</span>}</td>
            <td className="small">{t.parent || <span className="muted">none</span>}</td><td className="small">{t.members}</td>
            <td style={{ textAlign: 'right' }}><button className="btn sm" onClick={() => setTEdit(t)}>Edit</button> <button className="btn sm" onClick={() => setTeam(t.name)}>People</button></td></tr>)}</tbody></table></div>}</div>
    <div className="card"><div className="hd"><h2>People</h2>
      <div className="row" style={{ gap: 8 }}><input type="search" placeholder="Search name, email, title" value={q} onChange={(e) => setQ(e.target.value)} style={{ width: 220 }} aria-label="Search people" />
        <select value={team} onChange={(e) => setTeam(e.target.value)} aria-label="Team" style={{ width: 170 }}><option value="">All teams</option>{(d.data?.teams || []).map((t) => <option key={t.name}>{t.name}</option>)}</select>
        <label className="small row" style={{ gap: 4 }}><input type="checkbox" checked={left} onChange={(e) => setLeft(e.target.checked)} />Include people who left</label></div></div>
      {d.error ? <div className="bd"><ErrorBox error={d.error} retry={d.reload} /></div> : !d.data ? <div className="bd"><Loading /></div> : !d.data.people.length ? <div className="bd"><Empty icon="agent" title="No people">Import a CSV from HR, or add people one by one.</Empty></div> :
        <div className="tablewrap"><table className="t"><thead><tr><th>Person</th><th>Title</th><th>Team</th><th>Manager</th><th>Location</th><th>Account</th><th /></tr></thead>
          <tbody>{d.data.people.map((p) => <tr key={p.email} className={p.status === 'left' ? 'dim' : ''}><td><b>{p.name}</b><div className="tiny muted">{p.email}</div></td>
            <td className="small">{p.title}</td><td className="small">{p.team}</td><td className="small">{p.manager_email ? names[p.manager_email] || p.manager_email : ''}</td>
            <td className="small">{p.location}</td><td>{p.status === 'left' ? <Badge kind="bad">left</Badge> : p.has_account ? <Badge kind="ok">yes</Badge> : <span className="muted small">no</span>}</td>
            <td style={{ textAlign: 'right' }}><button className="btn sm" onClick={() => setEdit(p)}>Edit</button></td></tr>)}</tbody></table></div>}</div>
    {edit && <PersonModal p={edit} teams={d.data?.teams || []} onClose={() => setEdit(null)} onDone={() => { setEdit(null); d.reload(); toast('Saved') }} />}
    {tEdit && <TeamModal t={tEdit} teams={d.data?.teams || []} onClose={() => setTEdit(null)} onDone={() => { setTEdit(null); d.reload(); toast('Saved') }} />}
    {imp && <ImportModal onClose={() => setImp(false)} onDone={(n) => { setImp(false); d.reload(); toast(`${n} people imported`) }} />}
    {inv && <InviteModal onClose={() => setInv(false)} />}
  </div>
}

function PersonModal({ p, teams, onClose, onDone }: { p: Partial<Person>; teams: Team[]; onClose: () => void; onDone: () => void }) {
  const [f, setF] = useState<Partial<Person>>({ name: '', email: '', title: '', team: '', manager_email: '', location: '', status: 'active', ...p }); const toast = useToast()
  const set = (k: keyof Person) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value })
  return <Modal title={p.email ? `Edit ${p.name}` : 'Add person'} onClose={onClose} footer={<><button className="btn" onClick={onClose}>Cancel</button>
    <button className="btn primary" disabled={!f.name || !f.email} onClick={async () => { try { await api('/directory/people', { json: { ...f, team: f.team || null, manager_email: f.manager_email || null } }); onDone() } catch (e: any) { toast(e.message, true) } }}>Save</button></>}>
    <label className="f">Name<input type="text" value={f.name} onChange={set('name')} /></label>
    <label className="f">Work email<input type="email" value={f.email} onChange={set('email')} disabled={!!p.email} /></label>
    <label className="f">Title<input type="text" value={f.title} onChange={set('title')} /></label>
    <label className="f">Team<input type="text" list="teamlist" value={f.team || ''} onChange={set('team')} /><datalist id="teamlist">{teams.map((t) => <option key={t.name} value={t.name} />)}</datalist></label>
    <label className="f">Manager's email<input type="email" value={f.manager_email || ''} onChange={set('manager_email')} /></label>
    <label className="f">Location<input type="text" value={f.location} onChange={set('location')} /></label>
    <label className="f">Status<span className="h">Marking someone as left disables their account, signs out their apps and stops shares to them.</span>
      <select value={f.status} onChange={set('status')}><option value="active">active</option><option value="left">left</option></select></label>
  </Modal>
}

function TeamModal({ t, teams, onClose, onDone }: { t: Partial<Team>; teams: Team[]; onClose: () => void; onDone: () => void }) {
  const [f, setF] = useState<Partial<Team>>({ name: '', lead_email: '', parent: '', description: '', ...t }); const toast = useToast()
  return <Modal title={t.name ? `Edit ${t.name}` : 'Add team'} onClose={onClose} footer={<><button className="btn" onClick={onClose}>Cancel</button>
    <button className="btn primary" disabled={!f.name} onClick={async () => { try { await api('/directory/teams', { json: { ...f, lead_email: f.lead_email || null, parent: f.parent || null } }); onDone() } catch (e: any) { toast(e.message, true) } }}>Save</button></>}>
    <label className="f">Name<input type="text" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} disabled={!!t.name} /></label>
    <label className="f">Lead's email<input type="email" value={f.lead_email || ''} onChange={(e) => setF({ ...f, lead_email: e.target.value })} /></label>
    <label className="f">Part of<select value={f.parent || ''} onChange={(e) => setF({ ...f, parent: e.target.value })}><option value="">none</option>{teams.filter((x) => x.name !== f.name).map((x) => <option key={x.name}>{x.name}</option>)}</select></label>
    <label className="f">Description<input type="text" value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} /></label>
  </Modal>
}

function ImportModal({ onClose, onDone }: { onClose: () => void; onDone: (n: number) => void }) {
  const [text, setText] = useState(''); const [errors, setErrors] = useState<string[]>([]); const toast = useToast()
  return <Modal title="Import people (CSV)" onClose={onClose} footer={<><button className="btn" onClick={onClose}>Cancel</button>
    <button className="btn primary" disabled={!text.trim()} onClick={async () => { try { const r = await api<{ imported: number; errors: string[] }>('/directory/import', { json: { text } }); if (r.errors.length) { setErrors(r.errors); toast(`${r.imported} imported, ${r.errors.length} rows skipped`, true) } else onDone(r.imported) } catch (e: any) { toast(e.message, true) } }}>Import</button></>}>
    <p className="small muted">Columns: name, email, title, team, manager_email, location (and status: active or left). People already in the directory are updated.</p>
    <label className="f">CSV file<input type="file" accept=".csv,text/csv" onChange={async (e) => { const f = e.target.files?.[0]; if (f) setText(await f.text()) }} /></label>
    <label className="f">Or paste it<textarea rows={8} value={text} onChange={(e) => setText(e.target.value)} placeholder={SAMPLE} /></label>
    {errors.length > 0 && <div className="small" style={{ color: 'var(--bad)' }}>{errors.slice(0, 10).map((e) => <div key={e}>{e}</div>)}</div>}
  </Modal>
}

function InviteModal({ onClose }: { onClose: () => void }) {
  const [email, setEmail] = useState(''); const [role, setRole] = useState('viewer'); const [link, setLink] = useState(''); const toast = useToast()
  return <Modal title="Invite someone" onClose={onClose} footer={<><button className="btn" onClick={onClose}>Close</button>
    {!link && <button className="btn primary" disabled={!email.includes('@')} onClick={async () => { try { const r = await api<{ invite_link?: string; note: string }>('/directory/invites', { json: { email, role } }); if (r.invite_link) setLink(r.invite_link); else toast(r.note) } catch (e: any) { toast(e.message, true) } }}>Create invite link</button>}</>}>
    {!link ? <><label className="f">Work email<input type="email" value={email} onChange={(e) => setEmail(e.target.value)} /></label>
      <label className="f">Role<select value={role} onChange={(e) => setRole(e.target.value)}><option value="viewer">member</option><option value="publisher">publisher</option><option value="admin">admin</option></select></label></>
      : <><p className="small">Send this link to {email}. It works once, for 7 days. No email service is set up, so Context Layer does not send it.</p><Copy value={link} /></>}
  </Modal>
}
