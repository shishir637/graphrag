import { useState } from 'react'
import { ago, api } from '../api'
import { Copy, Icon, Loading, Modal, askConfirm, useLoad, useToast } from '../ui'
import { Checks, dsLabel, type AccessData, type Effective, type Grant } from '../admin'

type Level = 'owner' | 'company_admin' | 'team_admin' | 'member'
interface P { apps: string[]; roles: string[]; unique: boolean; email: string; name: string; title: string; team: string | null; admin_of: string[]; level: Level; account: 'active' | 'invited' | 'none' | 'disabled'; last_login: string | null }
interface T { name: string; slug: string; parent: string | null; depth: number; admins: string[]; members: number }
interface Data { base: string; people: P[]; teams: T[]; company_admin: boolean; you: string }
export interface Home { user: { name: string; email: string; teams: string[] }; links: { kind: string; team?: string; url: string }[]; manages: string[]; company_admin: boolean }

/* ---------------- an end user: their team and their link ---------------- */
export function Member({ home }: { home: Home }) {
  const link = home.links[home.links.length - 1]
  const me = useLoad<{ effective: Effective; labels: Record<string, string> }>(`/access/user?email=${encodeURIComponent(home.user.email)}`)
  const pretty = (n: string) => me.data?.labels[n] || n
  const CON: Record<string, string> = { documents: 'documents', data: 'metrics and tables', graph: 'the knowledge graph', upload: 'adding files' }
  return <div style={{ maxWidth: 620 }}>
    <h1>Hello, {home.user.name.split(' ')[0]}</h1>
    <p className="muted">{home.user.teams.length ? <>You are in <b>{home.user.teams.join(', ')}</b>.</> : 'You are not in a team yet. An admin will add you.'}</p>
    <div className="card"><div className="bd stack">
      <b className="small">Your link for Claude or ChatGPT</b>
      <Copy value={link.url} label="Copy" />
      <span className="small muted">Add it as a connector and sign in as {home.user.email}.</span></div></div>
    {me.data && <div className="card" style={{ marginTop: 14 }}><div className="bd small" style={{ lineHeight: 1.7 }}>
      <div><span className="muted">Claude can see files for:</span> {Object.keys(me.data.effective.datasets).filter((k) => !k.startsWith('layer:')).map(pretty).join(', ') || 'nothing yet'}</div>
      <div><span className="muted">Claude can use:</span> {Object.keys(me.data.effective.connectors).map((c) => CON[c] || c).join(', ') || 'nothing yet'}</div>
      <div className="tiny muted" style={{ marginTop: 4 }}>Need more? Ask your team admin.</div></div></div>}
  </div>
}

/* ---------------- admins: users and teams ---------------- */
export default function UsersPage({ home }: { home: Home }) {
  const d = useLoad<Data>('/people')
  const [filter, setFilter] = useState(() => decodeURIComponent(location.hash.split('team=')[1] || '')); const [q, setQ] = useState('')
  const [add, setAdd] = useState(false); const [edit, setEdit] = useState<P | null>(null); const [imp, setImp] = useState(false)
  if (d.error) return <div className="callout bad">{d.error}</div>
  if (!d.data) return <Loading />
  const x = d.data; const company = x.company_admin
  const mine = new Set(company ? x.teams.map((t) => t.name) : home.manages)
  const ql = q.trim().toLowerCase()
  const people = x.people.filter((p) => (!filter || (filter === '-' ? !p.team : p.team === filter || p.admin_of.includes(filter)))
    && (!ql || p.name.toLowerCase().includes(ql) || p.email.includes(ql)))
  const canEdit = (p: P) => p.level !== 'owner' && p.email !== x.you && (company || (!!p.team && mine.has(p.team)))
  return <>
    <div className="row" style={{ justifyContent: 'space-between', marginBottom: 14 }}>
      <h1 style={{ margin: 0 }}>Users <span className="muted" style={{ fontSize: 15, fontWeight: 400 }}>{x.people.length}</span></h1>
      <div className="row">
        <input type="search" placeholder="Search name or email" value={q} onChange={(e) => setQ(e.target.value)} style={{ width: 220 }} aria-label="Search" />
        <select value={filter} onChange={(e) => setFilter(e.target.value)} aria-label="Team" style={{ width: 'auto' }}>
          <option value="">{company ? 'All teams' : 'All my teams'}</option>
          {x.teams.map((t) => <option key={t.slug} value={t.name}>{t.name}</option>)}
          {company && <option value="-">No team</option>}</select>
        {company && <button className="btn" onClick={() => setImp(true)}><Icon name="upload" size={15} />Import</button>}
        <button className="btn primary" onClick={() => setAdd(true)}><Icon name="plus" size={15} />Add user</button></div>
    </div>

    {company && (x.teams.length === 0 || x.people.length <= 1) && <div className="callout" style={{ marginBottom: 14 }}>
      <b>Getting started:</b> 1. add teams on the <a href="#teams">Teams</a> tab · 2. add users or import them from a CSV · 3. upload files on the <a href="#data">Data</a> tab and choose who sees them · 4. give teams more on the <a href="#access">Access</a> tab.</div>}
    <div className="card"><table className="t plain users">
      <thead><tr><th>Name</th><th>Team</th><th>Access</th><th>Status</th><th /></tr></thead>
      <tbody>{people.map((p) => <tr key={p.email} className={canEdit(p) ? 'click' : ''} onClick={() => canEdit(p) && setEdit(p)}>
        <td><b>{p.name}</b>{p.email === x.you && <span className="muted"> (you)</span>}<div className="tiny muted">{p.email}</div></td>
        <td className="small">{p.team || <span className="muted">No team</span>}</td>
        <td className="small"><Access p={p} /></td>
        <td className="small muted">{p.account === 'invited' ? 'Invited' : p.account === 'none' ? 'Not invited' : p.last_login ? `Active ${ago(p.last_login)}` : 'Never signed in'}{p.apps.length > 0 && <div className="tiny">✓ {p.apps.join(', ')} connected</div>}</td>
        <td style={{ textAlign: 'right' }}>{canEdit(p) && <button className="btn sm" onClick={(e) => { e.stopPropagation(); setEdit(p) }}>Edit</button>}</td>
      </tr>)}
        {!people.length && <tr><td colSpan={5} className="muted small" style={{ padding: 18 }}>No users found.</td></tr>}</tbody></table></div>

    {add && <AddUser teams={x.teams.filter((t) => mine.has(t.name))} company={company} preset={filter && filter !== '-' ? filter : ''}
      onClose={() => setAdd(false)} onDone={d.reload} />}
    {imp && <Import onClose={() => setImp(false)} onDone={() => { setImp(false); d.reload() }} />}
    {edit && <EditUser p={edit} teams={x.teams.filter((t) => mine.has(t.name))} company={company}
      onClose={() => setEdit(null)} onDone={() => { setEdit(null); d.reload() }} />}
  </>
}

/* one plain phrase: what this person can do */
function Access({ p }: { p: P }) {
  const extra = [...p.roles, ...(p.unique ? ['extra access'] : [])]
  const plus = extra.length ? <span className="tiny muted"> + {extra.join(', ')}</span> : null
  if (p.level === 'owner') return <span className="tag strong">Owner</span>
  if (p.level === 'company_admin') return <span className="tag strong">Company admin</span>
  if (p.admin_of.length) return <><span className="tag">Admin of {p.admin_of.join(', ')}</span>{plus}</>
  return <><span className="muted">User</span>{plus}</>
}

/* Data access for one person: their team's access, optionally plus roles, data sets and connectors of their own */
function DataAccess({ a, g, setG, eff, extra, setExtra, labels = {} }: { a: AccessData; g: Grant; setG: (g: Grant) => void; eff: Effective | null; extra: boolean; setExtra: (b: boolean) => void; labels?: Record<string, string> }) {
  const can = a.can_give
  const L = (n: string) => labels[n] || dsLabel(a, n)
  const fixedR = g.roles.filter((r) => !can.roles.includes(r)), fixedD = g.datasets.filter((d) => !can.datasets.includes(d)), fixedC = g.connectors.filter((c) => !can.connectors.includes(c))
  return <div className="f"><span className="flabel">Data access</span>
    <div className="seg wide"><button type="button" className={!extra ? 'on' : ''} onClick={() => setExtra(false)}>Same as their team</button>
      <button type="button" className={extra ? 'on' : ''} onClick={() => setExtra(true)}>Team access + extra</button></div>
    {extra && <div className="extra">
      {can.roles.length > 0 && <><span className="sublabel">Roles</span>
        <Checks options={can.roles.map((r) => ({ v: r, label: r, hint: a.roles.find((x) => x.name === r)?.description }))} value={g.roles} onChange={(v) => setG({ ...g, roles: v })} /></>}
      {fixedR.length + fixedD.length + fixedC.length > 0 && <div className="tiny muted" style={{ marginBottom: 8 }}>Also has, from a company admin: {[...fixedR.map((r) => `role ${r}`), ...fixedD.map(L), ...fixedC.map((c) => a.connectors[c] || c)].join(', ')}</div>}
      <span className="sublabel">Data sets</span>
      <Checks options={can.datasets.filter((d) => d !== 'public').map((d) => ({ v: d, label: L(d) }))} value={g.datasets} onChange={(v) => setG({ ...g, datasets: v })} empty="Your teams have no data sets to give." />
      <span className="sublabel">Connectors</span>
      <Checks options={can.connectors.map((c) => ({ v: c, label: a.connectors[c] }))} value={g.connectors} onChange={(v) => setG({ ...g, connectors: v })} />
    </div>}
    {eff && <div className="effline"><span>Can see</span> {Object.keys(eff.datasets).map(L).join(', ') || 'nothing'}<br />
      <span>Claude can use</span> {Object.keys(eff.connectors).map((k) => a.connectors[k]).join(', ') || 'nothing'}</div>}
  </div>
}

type Kind = 'user' | 'team_admin' | 'company_admin'
const KINDS: { k: Kind; label: string; help: string }[] = [
  { k: 'user', label: 'User', help: 'A regular user: uses Claude with the access below.' },
  { k: 'team_admin', label: 'Team admin', help: 'Adds and removes people, and gives access, in these teams:' },
  { k: 'company_admin', label: 'Company admin', help: 'Manages every user, team, file and setting, and sees all data.' },
]

/* the access choice, shared by Add and Edit: three options; team admins pick their teams */
function AccessChoice({ kind, setKind, manages, setManages, teams, team }: { kind: Kind; setKind: (k: Kind) => void; manages: string[]; setManages: (t: string[]) => void; teams: T[]; team: string }) {
  return <div className="f"><span className="flabel">Can manage</span>
    <div className="seg wide">{KINDS.map((o) => <button type="button" key={o.k} className={kind === o.k ? 'on' : ''}
      onClick={() => { setKind(o.k); if (o.k === 'team_admin' && !manages.length && team) setManages([team]) }}>{o.k === 'user' ? 'Nothing' : o.label}</button>)}</div>
    <div className="tiny muted" style={{ marginTop: 6 }}>{KINDS.find((o) => o.k === kind)?.help}</div>
    {kind === 'team_admin' && <div style={{ marginTop: 8 }}><Checks options={teams.map((t) => ({ v: t.name, label: t.name }))} value={manages} onChange={setManages} /></div>}
  </div>
}

function EditUser({ p, teams, company, onClose, onDone }: { p: P; teams: T[]; company: boolean; onClose: () => void; onDone: () => void }) {
  const [team, setTeam] = useState(p.team || '')
  const [kind, setKind] = useState<Kind>(p.level === 'company_admin' ? 'company_admin' : p.admin_of.length ? 'team_admin' : 'user')
  const [manages, setManages] = useState<string[]>(p.admin_of); const toast = useToast()
  const acc = useLoad<AccessData>('/access'); const cur = useLoad<{ grant: Grant; effective: Effective; labels: Record<string, string> }>(`/access/user?email=${encodeURIComponent(p.email)}`)
  const [g, setG] = useState<Grant | null>(null); const [extra, setExtra] = useState<boolean | null>(null)
  const grant = g || cur.data?.grant || { datasets: [], connectors: [], roles: [] }
  const isExtra = extra ?? (grant.roles.length + grant.datasets.length + grant.connectors.length > 0)
  const save = async () => {
    const body: any = { email: p.email, team: team || null }
    if (cur.data && (g || extra !== null)) body.access = isExtra ? grant : { datasets: [], connectors: [], roles: [] }
    if (company) Object.assign(body, { role: kind === 'company_admin' ? 'admin' : 'user', admin_of: kind === 'team_admin' ? manages : [] })
    try { await api('/people', { json: body }); toast('Saved'); onDone() } catch (e: any) { toast(e.message, true) }
  }
  const remove = async () => {
    const ok = await askConfirm(company ? `Remove ${p.name}? Their account is switched off and Claude is disconnected for them.` : `Take ${p.name} out of ${p.team}?`, 'Remove')
    if (!ok) return
    try { await api('/people/remove', { json: { email: p.email } }); onDone() } catch (e: any) { toast(e.message, true) }
  }
  return <Modal title={p.name} onClose={onClose} footer={<>
    <button className="btn ghost" style={{ marginRight: 'auto', color: 'var(--bad)' }} onClick={remove}>{company ? 'Remove user' : 'Remove from team'}</button>
    <button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" disabled={kind === 'team_admin' && !manages.length} onClick={save}>Save</button></>}>
    <div className="row small muted" style={{ marginTop: -10, marginBottom: 6, justifyContent: 'space-between' }}><span>{p.email}</span>
      {(p.account === 'invited' || p.account === 'none') && <Reinvite email={p.email} />}</div>
    <label className="f">Team<select value={team} onChange={(e) => setTeam(e.target.value)} disabled={!company && teams.length < 2}>
      {company && <option value="">No team</option>}{teams.map((t) => <option key={t.slug} value={t.name}>{t.name}</option>)}</select></label>
    {company && <AccessChoice kind={kind} setKind={setKind} manages={manages} setManages={setManages} teams={teams} team={team} />}
    {kind !== 'company_admin' && acc.data && cur.data && <DataAccess a={acc.data} g={grant} setG={setG} eff={cur.data.effective} extra={isExtra} setExtra={setExtra} labels={cur.data.labels} />}
  </Modal>
}

function AddUser({ teams, company, preset, onClose, onDone }: { teams: T[]; company: boolean; preset: string; onClose: () => void; onDone: () => void }) {
  const [f, setF] = useState({ email: '', name: '', team: preset || (company ? '' : teams[0]?.name || '') })
  const [kind, setKind] = useState<Kind>('user'); const [manages, setManages] = useState<string[]>([])
  const [link, setLink] = useState(''); const toast = useToast()
  const acc = useLoad<AccessData>('/access'); const [extra, setExtra] = useState(false)
  const [g, setG] = useState<Grant>({ datasets: [], connectors: [], roles: [] })
  const save = async () => {
    try {
      const body: any = { email: f.email, name: f.name, team: f.team || null }
      if (extra) body.access = g
      if (company) Object.assign(body, { role: kind === 'company_admin' ? 'admin' : 'user', admin_of: kind === 'team_admin' ? manages : [] })
      const r = await api<{ invite?: { invite_link?: string } }>('/people', { json: body }); onDone()
      if (r.invite?.invite_link) setLink(r.invite.invite_link); else { toast('Saved'); onClose() }
    } catch (e: any) { toast(e.message, true) }
  }
  if (link) return <Modal title="Send them this sign-up link" onClose={onClose} footer={<button className="btn primary" onClick={onClose}>Done</button>}>
    <p className="small" style={{ marginTop: 0 }}>{f.email} opens it, chooses a password and is in. It works once, for 7 days.</p>
    <Copy value={link} label="Copy" /></Modal>
  return <Modal title="Add user" onClose={onClose} footer={<><button className="btn" onClick={onClose}>Cancel</button>
    <button className="btn primary" disabled={!f.email.includes('@') || (!company && !f.team) || (kind === 'team_admin' && !manages.length)} onClick={save}>Add</button></>}>
    <label className="f">Work email<input type="email" value={f.email} onChange={(e) => setF({ ...f, email: e.target.value })} autoFocus /></label>
    <label className="f">Name<input type="text" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} placeholder="Optional" /></label>
    <label className="f">Team<select value={f.team} onChange={(e) => setF({ ...f, team: e.target.value })}>
      {company && <option value="">No team</option>}{teams.map((t) => <option key={t.slug} value={t.name}>{t.name}</option>)}</select></label>
    {company && <AccessChoice kind={kind} setKind={setKind} manages={manages} setManages={setManages} teams={teams} team={f.team} />}
    {kind !== 'company_admin' && acc.data && <DataAccess a={acc.data} g={g} setG={setG} eff={null} extra={extra} setExtra={setExtra} />}
  </Modal>
}

function Reinvite({ email }: { email: string }) {
  const [link, setLink] = useState(''); const toast = useToast()
  if (link) return <span style={{ flex: 1, maxWidth: 380 }}><Copy value={link} label="Copy" /></span>
  return <button type="button" className="btn ghost sm" onClick={async () => {
    try { const r = await api<{ invite_link?: string; note?: string }>('/people/invite', { json: { email } }); if (r.invite_link) setLink(r.invite_link); else toast(r.note || 'Done') } catch (e: any) { toast(e.message, true) }
  }}>New sign-up link</button>
}

const SAMPLE = 'name,email,title,team\nPriya Nair,priya@example.com,Data Engineer,Data Engineering'

/* bring people in from HR: a CSV with name, email, title, team (manager_email and location optional) */
function Import({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [text, setText] = useState(''); const toast = useToast()
  const go = async () => {
    try { const r = await api<{ added?: number; updated?: number; imported?: number }>('/directory/import', { json: { text } })
      toast(`Imported ${r.imported ?? ((r.added || 0) + (r.updated || 0))} people. They have no account yet: open someone to send a sign-up link.`); onDone() } catch (e: any) { toast(e.message, true) }
  }
  return <Modal title="Import people" onClose={onClose} footer={<><button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" disabled={!text.trim()} onClick={go}>Import</button></>}>
    <p className="small muted" style={{ marginTop: 0 }}>A CSV from HR with a header row: <code>name, email, title, team</code> (and optionally <code>manager_email, location</code>). Teams that do not exist yet are created.</p>
    <input type="file" accept=".csv,text/csv" onChange={async (e) => { const f = e.target.files?.[0]; if (f) setText(await f.text()) }} />
    <textarea value={text} onChange={(e) => setText(e.target.value)} placeholder={SAMPLE} rows={8} style={{ width: '100%', marginTop: 10, fontFamily: 'monospace', fontSize: 12.5 }} aria-label="CSV" />
  </Modal>
}
