import { useState } from 'react'
import { api } from '../api'
import { CopyLink, dsLabel, type AccessData } from '../admin'
import { Icon, Loading, Modal, askConfirm, useLoad, useToast } from '../ui'

interface T { name: string; slug: string; parent: string | null; depth: number; admins: string[]; members: number; description?: string }
interface P { email: string; name: string; team: string | null; level: string }
interface Data { base: string; people: P[]; teams: T[]; company_admin: boolean }

/* Teams: create, nest, give admins, see what each can see, copy its link, delete. */
export default function TeamsPage() {
  const d = useLoad<Data>('/people'); const acc = useLoad<AccessData>('/access'); const toast = useToast()
  const [add, setAdd] = useState<string | null>(null); const [pick, setPick] = useState<T | null>(null)
  if (d.error) return <div className="callout bad">{d.error}</div>
  if (!d.data) return <Loading />
  const x = d.data; const company = x.company_admin
  const names = Object.fromEntries(x.people.map((p) => [p.email, p.name]))
  const reload = () => { d.reload(); acc.reload() }
  const setAdmin = async (t: T, email: string, admin: boolean) => {
    try { await api(`/teams/${t.slug}/admins`, { json: { email, admin } }); reload() } catch (e: any) { toast(e.message, true) }
  }
  const del = async (t: T) => {
    if (t.members > 0) { toast(`${t.name} still has ${t.members} ${t.members === 1 ? 'person' : 'people'}. Move them to another team on the Users tab first.`, true); return }
    if (!(await askConfirm(`Delete the team ${t.name}? Its files stay, visible to company admins until you give them another data set.`, 'Delete team'))) return
    try { await api(`/directory/teams/${encodeURIComponent(t.name)}`, { method: 'DELETE' }); toast('Team deleted'); reload() } catch (e: any) { toast(e.message, true) }
  }
  const grant = (t: T) => acc.data?.teams.find((z) => z.slug === t.slug)
  return <>
    <div className="row" style={{ justifyContent: 'space-between', marginBottom: 14 }}>
      <h1 style={{ margin: 0 }}>Teams <span className="muted" style={{ fontSize: 15, fontWeight: 400 }}>{x.teams.length}</span></h1>
      {company && <button className="btn primary" onClick={() => setAdd('')}><Icon name="plus" size={15} />New team</button>}
    </div>
    <div className="card"><table className="t plain">
      <thead><tr><th>Team</th><th>People</th><th>Admins</th><th>Can see</th><th /></tr></thead>
      <tbody>{x.teams.map((t) => { const g = grant(t)
        return <tr key={t.slug}>
          <td><span style={{ paddingLeft: company ? t.depth * 18 : 0 }}>{company && t.depth > 0 && <span className="muted">└ </span>}<b>{t.name}</b></span></td>
          <td className="small"><a href={`#users?team=${encodeURIComponent(t.name)}`}>{t.members}</a></td>
          <td className="small"><div className="row" style={{ gap: 4 }}>{t.admins.map((e) => <span key={e} className="tag">{names[e] || e}
            {company && <button className="x" onClick={() => setAdmin(t, e, false)} aria-label={`${names[e] || e} is no longer admin`}>×</button>}</span>)}
            {company ? <button className="iconplus" onClick={() => setPick(t)} title={`Add an admin to ${t.name}`} aria-label={`Add an admin to ${t.name}`}><Icon name="plus" size={14} /></button>
              : !t.admins.length && <span className="muted">No admin</span>}</div></td>
          <td className="small">{g ? [g.dataset, ...g.grant.datasets].map((n) => <span key={n} className="tag" style={{ margin: '0 4px 4px 0' }}>{dsLabel(acc.data, n)}</span>) : null}
            {company && <a href="#access" className="tiny" style={{ marginLeft: 2 }}>change</a>}</td>
          <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}><CopyLink url={`${x.base}/mcp/teams/${t.slug}`} label="Link" />
            {company && <button className="btn ghost sm" onClick={() => setAdd(t.name)} title={`New team inside ${t.name}`}><Icon name="plus" size={14} /></button>}
            {company && <button className="btn ghost sm" onClick={() => del(t)} title="Delete team" aria-label={`Delete ${t.name}`}><Icon name="x" size={14} /></button>}</td>
        </tr>
      })}
        {!x.teams.length && <tr><td colSpan={5} className="muted small" style={{ padding: 18 }}>No teams yet. Create your first one.</td></tr>}
      </tbody></table></div>
    <p className="tiny muted">Each team gets its own data set for its files, and its own link for Claude. Teams inside a team also get what the outer team can see. People and admins of a team are set on the Users tab too.</p>
    {pick && <AdminPicker t={pick} people={x.people} onClose={() => setPick(null)} onPick={async (e) => { await setAdmin(pick, e, true); setPick(null) }} />}
    {add !== null && <NewTeam parent={add} teams={x.teams} people={x.people} onClose={() => setAdd(null)} onDone={() => { setAdd(null); reload() }} />}
  </>
}

function NewTeam({ parent, teams, people, onClose, onDone }: { parent: string; teams: T[]; people: P[]; onClose: () => void; onDone: () => void }) {
  const [f, setF] = useState({ name: '', parent, admin: '' }); const toast = useToast()
  const save = async () => {
    try {
      const r = await api<{ name: string }>('/teams', { json: { name: f.name.trim(), parent: f.parent || null } })
      if (f.admin) {
        const slug = r.name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 60) || 'team'
        await api(`/teams/${slug}/admins`, { json: { email: f.admin, admin: true } })
      }
      toast(`${r.name} created`); onDone()
    } catch (e: any) { toast(e.message, true) }
  }
  return <Modal title="New team" onClose={onClose} footer={<><button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" disabled={!f.name.trim()} onClick={save}>Create team</button></>}>
    <label className="f">Name<input type="text" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} placeholder="e.g. Data Engineering" autoFocus /></label>
    <label className="f">Inside<select value={f.parent} onChange={(e) => setF({ ...f, parent: e.target.value })}>
      <option value="">Nothing: a top-level team</option>{teams.map((t) => <option key={t.slug} value={t.name}>{t.name}</option>)}</select></label>
    <label className="f">Admin<select value={f.admin} onChange={(e) => setF({ ...f, admin: e.target.value })}>
      <option value="">Choose later</option>{people.filter((p) => p.level !== 'owner').map((p) => <option key={p.email} value={p.email}>{p.name}</option>)}</select></label>
  </Modal>
}

/* choose a team admin: people in the team first, then everyone else; type to filter */
function AdminPicker({ t, people, onClose, onPick }: { t: T; people: P[]; onClose: () => void; onPick: (email: string) => void }) {
  const [q, setQ] = useState(''); const ql = q.trim().toLowerCase()
  const list = people.filter((p) => !t.admins.includes(p.email) && p.level !== 'owner' && (!ql || p.name.toLowerCase().includes(ql) || p.email.includes(ql)))
    .sort((a, b) => Number(b.team === t.name) - Number(a.team === t.name) || a.name.localeCompare(b.name))
  return <Modal title={`Add an admin to ${t.name}`} onClose={onClose} footer={<button className="btn" onClick={onClose}>Cancel</button>}>
    <input type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search people" autoFocus aria-label="Search people" />
    <div className="picklist">{list.slice(0, 60).map((p) => <button key={p.email} onClick={() => onPick(p.email)}>
      <span><b>{p.name}</b><span className="tiny muted"> {p.email}</span></span><span className="tiny muted">{p.team === t.name ? 'in this team' : p.team || 'no team'}</span></button>)}
      {!list.length && <div className="small muted" style={{ padding: 10 }}>No one found.</div>}</div>
  </Modal>
}
