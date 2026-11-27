import { useState } from 'react'
import { api } from '../api'
import { Checks, dsLabel, type AccessData, type Effective, type Role, type TeamA } from '../admin'
import { Icon, Loading, Modal, askConfirm, useLoad, useToast } from '../ui'

type Sub = 'teams' | 'roles' | 'datasets' | 'rows'

/* Access: what each team gets, roles to hand out, and the data sets everything is built from. */
export default function AccessPage() {
  const d = useLoad<AccessData>('/access'); const [sub, setSub] = useState<Sub>(() => (['roles', 'datasets', 'rows'].find((x) => location.hash.endsWith(`?${x}`)) as Sub) || 'teams')
  if (d.error) return <div className="callout bad">{d.error}</div>
  if (!d.data) return <Loading />
  const a = d.data
  return <>
    <div className="row" style={{ justifyContent: 'space-between', marginBottom: 14 }}>
      <h1 style={{ margin: 0 }}>Access</h1>
      <div className="seg">{((a.company_admin ? ['teams', 'roles', 'datasets', 'rows'] : ['teams', 'roles', 'datasets']) as Sub[]).map((x) => <button key={x} className={sub === x ? 'on' : ''} onClick={() => setSub(x)}>
        {x === 'teams' ? 'Teams' : x === 'roles' ? 'Roles' : x === 'rows' ? 'Row rules' : 'Data sets'}</button>)}</div>
    </div>
    <p className="small muted" style={{ marginTop: -4 }}>Access comes from a person's <b>team</b> and from what is given to them <b>directly</b> (on the Users tab). It adds up.</p>
    <CheckPerson a={a} />
    {sub === 'teams' ? <Teams a={a} reload={d.reload} /> : sub === 'roles' ? <Roles a={a} reload={d.reload} /> : sub === 'rows' ? <RowRules a={a} /> : <DataSets a={a} reload={d.reload} />}
  </>
}

/* type a person, see exactly what they can see and why */
function CheckPerson({ a }: { a: AccessData }) {
  const ppl = useLoad<{ people: { email: string; name: string }[] }>('/people')
  const [who, setWho] = useState(''); const [res, setRes] = useState<{ email: string; effective: Effective } | null>(null); const toast = useToast()
  const look = async (v: string) => {
    setWho(v); const p = ppl.data?.people.find((x) => x.email === v || x.name === v)
    if (!p) { setRes(null); return }
    try { setRes(await api(`/access/user?email=${encodeURIComponent(p.email)}`)) } catch (e: any) { toast(e.message, true) }
  }
  return <div className="card" style={{ marginBottom: 18 }}><div className="bd">
    <div className="row"><Icon name="search" size={16} /><input list="ppl" type="search" value={who} onChange={(e) => look(e.target.value)} placeholder="Check what someone can see: type a name" style={{ flex: 1, border: 0, background: 'none', boxShadow: 'none' }} aria-label="Check a person" />
      <datalist id="ppl">{ppl.data?.people.map((p) => <option key={p.email} value={p.name}>{p.email}</option>)}</datalist></div>
    {res && <div className="eff">
      <div><span className="flabel">Can see</span>{Object.entries(res.effective.datasets).map(([k, why]) =>
        <div key={k} className="small"><b>{dsLabel(a, k)}</b> <span className="muted">from {why.join(', ')}</span></div>)}</div>
      <div><span className="flabel">Claude can use</span>{Object.entries(res.effective.connectors).map(([k, why]) =>
        <div key={k} className="small"><b>{a.connectors[k] || k}</b> <span className="muted">from {why.join(', ')}</span></div>)}
        {!Object.keys(res.effective.connectors).length && <div className="small muted">Nothing: no connector is given to them.</div>}</div>
    </div>}
  </div></div>
}

const chips = (a: AccessData, list: string[], cls = '') => list.map((x) => <span key={x} className={`tag ${cls}`} style={{ margin: '0 4px 4px 0' }}>{dsLabel(a, x)}</span>)

function Teams({ a, reload }: { a: AccessData; reload: () => void }) {
  const [edit, setEdit] = useState<TeamA | null>(null)
  if (!a.teams.length) return <div className="card"><div className="bd small muted">No teams yet. Create teams on the Users tab.</div></div>
  return <div className="card"><table className="t plain">
    <thead><tr><th>Team</th><th>Can see</th><th>Claude can use</th><th /></tr></thead>
    <tbody>{a.teams.map((t) => <tr key={t.slug}>
      <td><span style={{ paddingLeft: a.company_admin ? t.depth * 16 : 0 }}><b>{t.name}</b></span><div className="tiny muted" style={{ paddingLeft: a.company_admin ? t.depth * 16 : 0 }}>{t.members} {t.members === 1 ? 'person' : 'people'}</div></td>
      <td className="small">{chips(a, [t.dataset, ...t.grant.datasets])}{t.grant.roles.map((r) => <span key={r} className="tag role" style={{ margin: '0 4px 4px 0' }}>role {r}</span>)}</td>
      <td className="small">{Object.keys(a.connectors).filter((c) => t.grant.connectors.includes(c) || (a.roles.find((r) => r.builtin)?.connectors || []).includes(c)).map((c) => a.connectors[c]).join(', ') || <span className="muted">nothing</span>}</td>
      <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>{a.company_admin && <button className="btn sm" onClick={() => setEdit(t)}>Edit</button>}</td>
    </tr>)}</tbody></table>
    <div className="bd tiny muted" style={{ borderTop: '1px solid var(--line)' }}>Every team can see its own data set. Teams inside a team also get what the outer team gets. On top: the Everyone role ({chips(a, a.roles.find((r) => r.builtin)?.datasets || [])}).</div>
    {edit && <TeamEdit t={edit} a={a} onClose={() => setEdit(null)} onDone={() => { setEdit(null); reload() }} />}
  </div>
}

function TeamEdit({ t, a, onClose, onDone }: { t: TeamA; a: AccessData; onClose: () => void; onDone: () => void }) {
  const [g, setG] = useState(t.grant); const toast = useToast()
  const everyone = a.roles.find((r) => r.builtin)?.connectors || []
  const save = async () => { try { await api(`/access/teams/${t.slug}`, { method: 'PUT', json: g }); toast('Saved'); onDone() } catch (e: any) { toast(e.message, true) } }
  return <Modal title={`${t.name}: access`} onClose={onClose} footer={<><button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" onClick={save}>Save</button></>}>
    <span className="flabel">Data sets</span>
    <Checks options={a.datasets.filter((d) => d.name !== 'public').map((d) => ({ v: d.name, label: d.label }))} value={g.datasets} locked={[t.dataset]} lockHint="The team's own data set" onChange={(v) => setG({ ...g, datasets: v.filter((x) => x !== t.dataset) })} />
    <span className="flabel" style={{ marginTop: 14 }}>Roles</span>
    <Checks options={a.roles.filter((r) => !r.builtin).map((r) => ({ v: r.name, label: r.name, hint: r.description }))} value={g.roles} onChange={(v) => setG({ ...g, roles: v })} empty="No roles yet: add them under Roles." />
    <span className="flabel" style={{ marginTop: 14 }}>Connectors</span>
    <Checks options={Object.entries(a.connectors).map(([v, label]) => ({ v, label }))} value={g.connectors} locked={everyone} lockHint="Everyone has this (Roles, Everyone)" onChange={(v) => setG({ ...g, connectors: v.filter((x) => !everyone.includes(x)) })} />
    {everyone.length === Object.keys(a.connectors).length && <p className="tiny muted">Everyone already has every connector. To limit them, edit the Everyone role under Roles, then give connectors back to teams here.</p>}
  </Modal>
}

function Roles({ a, reload }: { a: AccessData; reload: () => void }) {
  const [edit, setEdit] = useState<Partial<Role> | null>(null); const toast = useToast()
  const del = async (r: Role) => {
    if (!(await askConfirm(`Delete the role ${r.name}? Teams and people lose what it gave them.`, 'Delete'))) return
    try { await api(`/access/roles/${encodeURIComponent(r.name)}`, { method: 'DELETE' }); reload() } catch (e: any) { toast(e.message, true) }
  }
  return <>
    <div className="card"><table className="t plain">
      <thead><tr><th>Role</th><th>Can see</th><th>Claude can use</th><th /></tr></thead>
      <tbody>{a.roles.map((r) => <tr key={r.name}>
        <td><b>{r.name}</b>{r.builtin && <span className="tag strong" style={{ marginLeft: 6 }}>every account</span>}<div className="tiny muted">{r.description}</div></td>
        <td className="small">{r.datasets.length ? chips(a, r.datasets) : <span className="muted">nothing</span>}</td>
        <td className="small">{r.connectors.length ? r.connectors.map((c) => a.connectors[c]).join(', ') : <span className="muted">nothing</span>}</td>
        <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>{a.company_admin && <><button className="btn sm" onClick={() => setEdit(r)}>Edit</button>
          {!r.builtin && <button className="btn ghost sm" onClick={() => del(r)} aria-label={`Delete ${r.name}`}><Icon name="x" size={14} /></button>}</>}</td>
      </tr>)}</tbody></table></div>
    {a.company_admin && <button className="btn" style={{ marginTop: 12 }} onClick={() => setEdit({ datasets: [], connectors: ['documents'] })}><Icon name="plus" size={15} />New role</button>}
    {edit && <RoleEdit r={edit} a={a} onClose={() => setEdit(null)} onDone={() => { setEdit(null); reload() }} />}
  </>
}

function RoleEdit({ r, a, onClose, onDone }: { r: Partial<Role>; a: AccessData; onClose: () => void; onDone: () => void }) {
  const [f, setF] = useState({ name: r.name || '', description: r.description || '', datasets: r.datasets || [], connectors: r.connectors || [] }); const toast = useToast()
  const save = async () => { try { await api('/access/roles', { json: { ...f, old: r.name || null } }); toast('Saved'); onDone() } catch (e: any) { toast(e.message, true) } }
  return <Modal title={r.name ? `Role: ${r.name}` : 'New role'} onClose={onClose} footer={<><button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" disabled={!f.name.trim()} onClick={save}>Save</button></>}>
    {!r.builtin && <label className="f">Name<input type="text" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} placeholder="e.g. Finance reader" autoFocus /></label>}
    <label className="f">What it is for<input type="text" value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} placeholder="Optional" /></label>
    <span className="flabel">Can see</span>
    <Checks options={a.datasets.map((d) => ({ v: d.name, label: d.label }))} value={f.datasets} onChange={(v) => setF({ ...f, datasets: v })} />
    <span className="flabel" style={{ marginTop: 14 }}>Claude can use</span>
    <Checks options={Object.entries(a.connectors).map(([v, label]) => ({ v, label }))} value={f.connectors} onChange={(v) => setF({ ...f, connectors: v })} />
    {r.builtin && <p className="tiny muted">Every account gets this role. Keep it small: give more through teams and roles.</p>}
  </Modal>
}

function DataSets({ a, reload }: { a: AccessData; reload: () => void }) {
  const [name, setName] = useState(''); const toast = useToast()
  const add = async () => { try { await api('/access/datasets', { json: { name } }); setName(''); reload() } catch (e: any) { toast(e.message, true) } }
  const del = async (n: string) => {
    if (!(await askConfirm(`Delete the data set ${n}?`, 'Delete'))) return
    try { await api(`/access/datasets/${encodeURIComponent(n)}`, { method: 'DELETE' }); reload() } catch (e: any) { toast(e.message, true) }
  }
  return <div className="card"><table className="t plain">
    <thead><tr><th>Data set</th><th>Files</th><th>Who can see it</th><th /></tr></thead>
    <tbody>{a.datasets.map((d) => <tr key={d.name}>
      <td><b>{d.label}</b>{d.kind === 'team' && <span className="tiny muted"> · team</span>}</td>
      <td className="small"><a href="#data">{d.files}</a></td>
      <td className="small">{d.who.everyone ? 'Everyone' : [...d.who.teams.map((t) => `${t} team`), ...d.who.roles.map((r) => `role ${r}`),
        ...(d.who.people.length ? [`${d.who.people.length} directly`] : [])].join(', ') || <span className="muted">only company admins</span>}</td>
      <td style={{ textAlign: 'right' }}>{a.company_admin && d.kind === 'custom' && <button className="btn ghost sm" onClick={() => del(d.name)} aria-label={`Delete ${d.name}`}><Icon name="x" size={14} /></button>}</td>
    </tr>)}</tbody></table>
    {a.company_admin && <div className="bd row" style={{ borderTop: '1px solid var(--line)' }}>
      <input type="text" value={name} onChange={(e) => setName(e.target.value)} placeholder="New data set, e.g. board-papers" style={{ flex: 1 }} onKeyDown={(e) => { if (e.key === 'Enter' && name.trim()) add() }} aria-label="New data set" />
      <button className="btn" disabled={!name.trim()} onClick={add}>Add data set</button></div>}
  </div>
}

/* Row rules: metrics only count the rows a data set, team or role may see (e.g. region managers see their region). */
interface RowRule { entity: string; column: string; kind: 'dataset' | 'team' | 'role'; audience: string; values: string[]; source: string; by: string | null }
interface RowData { rules: RowRule[]; entities: { name: string; label: string; columns: string[] }[]; kinds: string[] }

function RowRules({ a }: { a: AccessData }) {
  const d = useLoad<RowData>('/access/row-rules'); const toast = useToast()
  const [entity, setEntity] = useState(''); const [column, setColumn] = useState(''); const [kind, setKind] = useState<'dataset' | 'team' | 'role'>('dataset')
  const [audience, setAudience] = useState(''); const [values, setValues] = useState('')
  if (d.error) return <div className="callout bad">{d.error}</div>
  if (!d.data) return <Loading />
  const ent = d.data.entities.find((e) => e.name === entity)
  const who = kind === 'dataset' ? a.datasets.map((x) => ({ v: x.name, l: x.label })) : kind === 'team' ? a.teams.map((t) => ({ v: t.name, l: t.name })) : a.roles.map((r) => ({ v: r.name, l: r.name }))
  const label = (r: RowRule) => r.kind === 'dataset' ? `data set ${dsLabel(a, r.audience)}` : `${r.kind} ${r.audience}`
  const save = async () => {
    try { await api('/access/row-rules', { method: 'PUT', json: { entity, column, kind, audience, values: values.split(',').map((x) => x.trim()).filter(Boolean) } })
      toast('Saved. Numbers for these people now count only those rows.'); setValues(''); d.reload() } catch (e: any) { toast(e.message, true) }
  }
  const del = async (r: RowRule) => {
    if (!(await askConfirm(`Remove the rule for ${label(r)} on ${r.entity}.${r.column}?`, 'Remove'))) return
    try { await api(`/access/row-rules?entity=${encodeURIComponent(r.entity)}&column=${encodeURIComponent(r.column)}&kind=${r.kind}&audience=${encodeURIComponent(r.audience)}`, { method: 'DELETE' }); d.reload() } catch (e: any) { toast(e.message, true) }
  }
  return <>
    <p className="small muted">A table with rules is closed to anyone no rule covers; tables without rules are not filtered. Company admins always see every row.</p>
    <div className="card" style={{ marginBottom: 14 }}><table className="t plain"><thead><tr><th>Table</th><th>Who</th><th>Only rows where</th><th /></tr></thead>
      <tbody>{d.data.rules.map((r) => <tr key={`${r.entity}|${r.column}|${r.kind}|${r.audience}`}>
        <td className="small"><b>{r.entity}</b></td><td className="small">{label(r)}</td>
        <td className="small">{r.column} is {r.values.join(', ')}{r.source !== 'manual' && <span className="muted"> ({r.source})</span>}</td>
        <td style={{ textAlign: 'right' }}><button className="btn ghost sm" onClick={() => del(r)} aria-label="Remove rule"><Icon name="x" size={14} /></button></td></tr>)}
        {!d.data.rules.length && <tr><td colSpan={4} className="muted small" style={{ padding: 18 }}>No row rules: everyone with the Metrics connector counts every row.</td></tr>}</tbody></table></div>
    <div className="card"><div className="bd"><div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
      <select value={entity} onChange={(e) => { setEntity(e.target.value); setColumn('') }} aria-label="Table"><option value="">Table…</option>
        {d.data.entities.map((e) => <option key={e.name} value={e.name}>{e.label}</option>)}</select>
      <select value={column} onChange={(e) => setColumn(e.target.value)} aria-label="Column" disabled={!ent}><option value="">Column…</option>
        {ent?.columns.map((c) => <option key={c} value={c}>{c}</option>)}</select>
      <select value={kind} onChange={(e) => { setKind(e.target.value as any); setAudience('') }} aria-label="Rule for"><option value="dataset">Data set</option><option value="team">Team</option><option value="role">Role</option></select>
      <select value={audience} onChange={(e) => setAudience(e.target.value)} aria-label="Who"><option value="">Who…</option>
        {who.map((w) => <option key={w.v} value={w.v}>{w.l}</option>)}</select>
      <input type="text" value={values} onChange={(e) => setValues(e.target.value)} placeholder="Allowed values, comma-separated" style={{ flex: 1, minWidth: 180 }} aria-label="Allowed values" />
      <button className="btn primary sm" disabled={!entity || !column || !audience || !values.trim()} onClick={save}>Add rule</button></div></div></div>
  </>
}
