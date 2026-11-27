import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { ago, api } from '../api'
import { Copy, Empty, ErrorBox, Icon, Loading, Modal, askConfirm, useLoad, useToast } from '../ui'

type Level = 'owner' | 'company_admin' | 'team_admin' | 'member'
interface P { email: string; name: string; title: string; team: string | null; level: Level; account: 'active' | 'invited' | 'none' | 'disabled'; last_login: string | null }
interface T { name: string; slug: string; parent: string | null; depth: number; admins: string[]; members: number }
interface Data { people: P[]; teams: T[]; company_admin: boolean; you: string }
interface TeamInfo { link: string | null; link_extra: { kind: string; target: string }[]; items?: { kind: string; key: string; removed: boolean; body: any }[] }

const LEVEL: Record<Level, string> = { owner: 'Owner', company_admin: 'Company admin', team_admin: 'Team admin', member: 'Member' }
const NOTEAM = '__none__'

export default function People() {
  const d = useLoad<Data>('/people'); const toast = useToast()
  const [sp, setSp] = useSearchParams(); const sel = sp.get('team') || ''
  const [add, setAdd] = useState(false); const [newTeam, setNewTeam] = useState<string | null>(null)
  // team admins start on their (first) team; company admins on everyone
  useEffect(() => { if (d.data && !d.data.company_admin && !sel && d.data.teams.length) setSp({ team: d.data.teams[0].slug }, { replace: true }) }, [d.data, sel])
  if (d.error) return <div className="page"><ErrorBox error={d.error} retry={d.reload} /></div>
  if (!d.data) return <div className="page"><Loading /></div>
  const x = d.data
  const team = x.teams.find((t) => t.slug === sel)
  const list = x.people.filter((p) => !sel ? true : sel === NOTEAM ? !p.team : p.team === team?.name)
  const save = async (p: P, change: Partial<{ team: string; level: Level }>) => {
    try { await api('/people', { json: { email: p.email, ...change } }); d.reload() } catch (e: any) { toast(e.message, true) }
  }
  const remove = async (p: P) => {
    const msg = x.company_admin ? `${p.name} has left? Their account is switched off and Claude is disconnected for them.` : `Take ${p.name} out of ${p.team}?`
    if (!(await askConfirm(msg, x.company_admin ? 'They left' : 'Remove'))) return
    try { await api('/people/remove', { json: { email: p.email } }); d.reload() } catch (e: any) { toast(e.message, true) }
  }
  const noTeam = x.people.filter((p) => !p.team).length
  const one = !x.company_admin && x.teams.length === 1
  return <div className={`page${one ? '' : ' people'}`}>
    {!one && <aside className="teamcol">
      {x.company_admin && <TeamBtn on={!sel} onClick={() => setSp({})} label="Everyone" n={x.people.length} />}
      {x.teams.map((t) => <TeamBtn key={t.slug} on={sel === t.slug} onClick={() => setSp({ team: t.slug })} label={t.name} n={t.members} depth={t.depth} />)}
      {x.company_admin && noTeam > 0 && <TeamBtn on={sel === NOTEAM} onClick={() => setSp({ team: NOTEAM })} label="No team" n={noTeam} muted />}
      {x.company_admin && <button className="btn ghost sm" style={{ marginTop: 8 }} onClick={() => setNewTeam(team?.name || '')}><Icon name="plus" size={14} />New team</button>}
    </aside>}
    <section style={{ minWidth: 0 }}>
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 12 }}>
        <h1 style={{ fontSize: 20, margin: 0 }}>{team ? team.name : sel === NOTEAM ? 'No team' : 'Everyone'}</h1>
        <button className="btn sm primary" onClick={() => setAdd(true)}><Icon name="plus" size={14} />Add person</button></div>
      {team && <TeamBar slug={team.slug} name={team.name} />}
      <div className="card">{!list.length ? <div className="bd"><Empty icon="agent" title="No one here yet" /></div> :
        <table className="t plain"><tbody>{list.map((p) => <tr key={p.email} className={p.account === 'disabled' ? 'dim' : ''}>
          <td><b>{p.name}</b>{p.email === x.you && <span className="muted"> (you)</span>}<div className="tiny muted">{p.email}{p.title ? ` · ${p.title}` : ''}</div></td>
          {x.company_admin && !team && <td className="small">
            <select value={p.team || ''} onChange={(e) => save(p, { team: e.target.value })} aria-label={`Team of ${p.name}`}>
              <option value="">No team</option>{x.teams.map((t) => <option key={t.slug} value={t.name}>{t.name}</option>)}</select></td>}
          <td className="small">{x.company_admin && p.level !== 'owner' ?
            <select value={p.level} onChange={(e) => save(p, { level: e.target.value as Level })} aria-label={`Access of ${p.name}`}>
              <option value="member">Member</option>{p.team && <option value="team_admin">Team admin</option>}<option value="company_admin">Company admin</option></select>
            : LEVEL[p.level]}</td>
          <td className="small muted">{p.account === 'invited' ? 'Invited' : p.account === 'none' ? 'Not invited' : p.account === 'disabled' ? 'Left' : p.last_login ? `Seen ${ago(p.last_login)}` : 'Never signed in'}</td>
          <td style={{ textAlign: 'right' }}>{p.email !== x.you && p.level !== 'owner' && p.account !== 'disabled' &&
            <button className="btn ghost sm" onClick={() => remove(p)} aria-label={`Remove ${p.name}`} title={x.company_admin ? 'Left the company' : 'Remove from team'}><Icon name="x" size={14} /></button>}</td>
        </tr>)}</tbody></table>}</div>
    </section>
    {add && <AddPerson teams={x.teams} team={team?.name || ''} company={x.company_admin} onClose={() => setAdd(false)} onDone={d.reload} />}
    {newTeam !== null && <NewTeam parent={newTeam} teams={x.teams} onClose={() => setNewTeam(null)} onDone={(slug) => { setNewTeam(null); d.reload(); setSp({ team: slug }) }} />}
  </div>
}

function TeamBtn({ on, onClick, label, n, depth = 0, muted }: { on: boolean; onClick: () => void; label: string; n: number; depth?: number; muted?: boolean }) {
  return <button className={`teambtn${on ? ' on' : ''}${muted ? ' muted' : ''}`} style={{ paddingLeft: 10 + depth * 14 }} onClick={onClick}>
    <span>{label}</span><span className="tiny muted">{n}</span></button>
}

/* the team's link, who else may use it, and its own rules: one line, details on demand */
function TeamBar({ slug, name }: { slug: string; name: string }) {
  const d = useLoad<TeamInfo>(`/teams/${slug}`, [slug]); const [open, setOpen] = useState(false)
  if (!d.data) return null
  const n = d.data.items?.filter((i) => !i.removed).length || 0
  return <div className="teambar">
    {d.data.link && <div style={{ flex: 1, minWidth: 0 }}><Copy value={d.data.link} label="Copy team link" /></div>}
    <button className="btn ghost sm" onClick={() => setOpen(true)}>{n ? `${n} team ${n === 1 ? 'rule' : 'rules'} · ` : ''}Link access</button>
    {open && <TeamDetails slug={slug} name={name} info={d.data} reload={d.reload} onClose={() => setOpen(false)} />}
  </div>
}

function TeamDetails({ slug, name, info, reload, onClose }: { slug: string; name: string; info: TeamInfo; reload: () => void; onClose: () => void }) {
  const toast = useToast(); const [who, setWho] = useState('')
  const post = async (body: object) => { try { await api(`/teams/${slug}/link`, { json: body }); reload() } catch (e: any) { toast(e.message, true) } }
  const items = (info.items || []).filter((i) => !i.removed)
  return <Modal title={name} onClose={onClose} footer={<button className="btn primary" onClick={onClose}>Done</button>}>
    <div className="small"><b>Who can use the team link:</b> everyone in {name} and the teams under it.</div>
    {info.link_extra.map((x) => <div key={x.kind + x.target} className="row small">Also {x.target} <button className="btn ghost sm" onClick={() => post({ remove: x })}>remove</button></div>)}
    <div className="row"><input type="text" placeholder="Also allow: emails or team names" value={who} onChange={(e) => setWho(e.target.value)} style={{ flex: 1 }} aria-label="Also allow" />
      <button className="btn sm" disabled={!who.trim()} onClick={() => { const v = who.split(',').map((s) => s.trim()).filter(Boolean); post({ people: v.filter((s) => s.includes('@')), teams: v.filter((s) => !s.includes('@')) }); setWho('') }}>Allow</button></div>
    <div className="small" style={{ marginTop: 14 }}><b>Team rules and words</b> <span className="muted">(added through Claude: "for my team")</span></div>
    {items.length ? items.map((i) => <div key={i.kind + i.key} className="small">{i.body?.text || `${i.key.split('>')[0]} = ${i.body?.target ?? i.body?.description ?? ''}`}</div>)
      : <div className="small muted">None yet.</div>}
  </Modal>
}

function AddPerson({ teams, team, company, onClose, onDone }: { teams: T[]; team: string; company: boolean; onClose: () => void; onDone: () => void }) {
  const [f, setF] = useState({ email: '', name: '', team: team || (company ? '' : teams[0]?.name || ''), level: 'member' as Level })
  const [link, setLink] = useState(''); const toast = useToast()
  const save = async () => {
    try {
      const r = await api<{ invite?: { invite_link?: string } }>('/people', { json: { ...f, team: f.team || null } }); onDone()
      if (r.invite?.invite_link) setLink(r.invite.invite_link); else { toast(`${f.email} saved`); onClose() }
    } catch (e: any) { toast(e.message, true) }
  }
  if (link) return <Modal title="Send them this link" onClose={onClose} footer={<button className="btn primary" onClick={onClose}>Done</button>}>
    <p className="small" style={{ marginTop: 0 }}>{f.email} signs up with it and chooses a password. It works once, for 7 days.</p>
    <Copy value={link} label="Copy" /></Modal>
  return <Modal title="Add person" onClose={onClose} footer={<><button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" disabled={!f.email.includes('@')} onClick={save}>Add</button></>}>
    <label className="f">Work email<input type="email" value={f.email} onChange={(e) => setF({ ...f, email: e.target.value })} autoFocus /></label>
    <label className="f">Name<input type="text" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} placeholder="Optional" /></label>
    <label className="f">Team<select value={f.team} onChange={(e) => setF({ ...f, team: e.target.value, level: e.target.value ? f.level : f.level === 'team_admin' ? 'member' : f.level })}>
      {company && <option value="">No team</option>}{teams.map((t) => <option key={t.slug} value={t.name}>{t.name}</option>)}</select></label>
    {company && <label className="f">Access<select value={f.level} onChange={(e) => setF({ ...f, level: e.target.value as Level })}>
      <option value="member">Member: uses Claude with the company's and their team's knowledge</option>
      {f.team && <option value="team_admin">Team admin: also manages {f.team}</option>}
      <option value="company_admin">Company admin: manages everything</option></select></label>}
  </Modal>
}

function NewTeam({ parent, teams, onClose, onDone }: { parent: string; teams: T[]; onClose: () => void; onDone: (slug: string) => void }) {
  const [f, setF] = useState({ name: '', parent }); const toast = useToast()
  const save = async () => {
    try { await api('/teams', { json: { name: f.name.trim(), parent: f.parent || null } })
      onDone(f.name.trim().toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 60) || 'team') } catch (e: any) { toast(e.message, true) }
  }
  return <Modal title="New team" onClose={onClose} footer={<><button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" disabled={!f.name.trim()} onClick={save}>Create</button></>}>
    <label className="f">Name<input type="text" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} autoFocus /></label>
    <label className="f">Inside<select value={f.parent} onChange={(e) => setF({ ...f, parent: e.target.value })}><option value="">Nothing: a top-level team</option>
      {teams.map((t) => <option key={t.slug} value={t.name}>{t.name}</option>)}</select></label>
  </Modal>
}
