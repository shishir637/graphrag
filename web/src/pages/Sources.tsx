import { useState } from 'react'
import { ago, api } from '../api'
import { Icon, Loading, Modal, askConfirm, useLoad, useToast } from '../ui'

/* Sources: outside systems (GitHub, databases, S3 folders) kept up to date on their own. Each puts its files into
   one data set, so who sees them is decided on the Access tab like any other file. ("Connectors" in this app are
   what Claude may use for a person; outside systems are sources.) */
export interface SourceRow {
  id: string; kind: string; label: string; name: string; config: Record<string, any>; dataset: string; every_minutes: number
  status: 'active' | 'paused'; last_run: string | null; last_ok: string | null; last_error: string | null; next_run: string | null
  summary: Record<string, any>; documents: number; secrets: Record<string, string | null>; webhook: string | null
}
interface Kind { kind: string; label: string; config: Record<string, string>; secrets: Record<string, string>; webhook: boolean }

const EVERY = [[15, 'every 15 minutes'], [60, 'every hour'], [360, 'every 6 hours'], [1440, 'once a day']] as const

export default function Sources({ datasets, labels, onChanged }: { datasets: string[]; labels: Record<string, string>; onChanged: () => void }) {
  const d = useLoad<{ sources: SourceRow[]; kinds: Kind[] }>('/sources'); const toast = useToast()
  const [add, setAdd] = useState<Kind | null>(null); const [edit, setEdit] = useState<SourceRow | null>(null)
  if (d.error) return <div className="callout bad">{d.error}</div>
  if (!d.data) return <Loading />
  const act = async (s: SourceRow, what: 'sync' | 'test') => {
    try {
      const r = await api<any>(`/sources/${s.id}/${what}`, { method: 'POST' })
      if (what === 'test') toast(r.ok ? `Works: ${r.detail}` : `Not working: ${r.detail}`, !r.ok)
      else toast(`Syncing ${s.name}. New and changed files are prepared for Claude when it finishes.`)
      d.reload()
    } catch (e: any) { toast(e.message, true) }
  }
  const pause = async (s: SourceRow) => {
    try { await api(`/sources/${s.id}`, { method: 'PATCH', json: { status: s.status === 'active' ? 'paused' : 'active' } }); d.reload() } catch (e: any) { toast(e.message, true) }
  }
  const del = async (s: SourceRow) => {
    if (!(await askConfirm(`Remove ${s.name}? Its ${s.documents} files are deleted; tables stay until you remove them.`, 'Remove'))) return
    try { await api(`/sources/${s.id}`, { method: 'DELETE' }); d.reload(); onChanged() } catch (e: any) { toast(e.message, true) }
  }
  return <>
    <p className="small muted" style={{ marginTop: 0 }}>Connect GitHub, a database or a file store once; Context Layer keeps it up to date on a schedule
      {d.data.kinds.some((k) => k.webhook) ? ' (GitHub also right away, through a webhook)' : ''}. Each source puts its files into one data set.</p>
    <div className="row" style={{ gap: 8, marginBottom: 14, flexWrap: 'wrap' }}>
      {d.data.kinds.map((k) => <button key={k.kind} className="btn sm" onClick={() => setAdd(k)}><Icon name="plus" size={14} />{k.label}</button>)}
    </div>
    <div className="card"><table className="t plain">
      <thead><tr><th>Source</th><th>Into data set</th><th>Last sync</th><th /></tr></thead>
      <tbody>{d.data.sources.map((s) => <tr key={s.id}>
        <td><b className="small">{s.name}</b><div className="tiny muted">{s.label} · {EVERY.find((e) => e[0] === s.every_minutes)?.[1] || `every ${s.every_minutes} min`}{s.status === 'paused' ? ' · paused' : ''}</div></td>
        <td className="small"><span className="tag">{labels[s.dataset] || s.dataset}</span><div className="tiny muted">{s.documents} files</div></td>
        <td className="small">{s.last_error ? <span className="warn-text" title={s.last_error}>failed {ago(s.last_run)}</span> :
          s.last_ok ? <span className="muted">{ago(s.last_ok)}</span> : <span className="muted">not yet</span>}</td>
        <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
          <button className="btn sm" onClick={() => act(s, 'sync')}>Sync now</button>{' '}
          <button className="btn ghost sm" onClick={() => act(s, 'test')}>Test</button>{' '}
          <button className="btn ghost sm" onClick={() => setEdit(s)}>Settings</button>{' '}
          <button className="btn ghost sm" onClick={() => pause(s)}>{s.status === 'active' ? 'Pause' : 'Resume'}</button>{' '}
          <button className="btn ghost sm" onClick={() => del(s)} aria-label={`Remove ${s.name}`} title="Remove"><Icon name="x" size={14} /></button></td>
      </tr>)}
        {!d.data.sources.length && <tr><td colSpan={4} className="muted small" style={{ padding: 18 }}>No sources yet.</td></tr>}
      </tbody></table></div>
    {add && <SourceForm kind={add} datasets={datasets} labels={labels} onClose={() => setAdd(null)} onDone={() => { setAdd(null); d.reload(); onChanged() }} />}
    {edit && <SourceForm kind={d.data.kinds.find((k) => k.kind === edit.kind)!} existing={edit} datasets={datasets} labels={labels}
      onClose={() => setEdit(null)} onDone={() => { setEdit(null); d.reload(); onChanged() }} />}
  </>
}

const asValue = (s: string) => {
  const t = s.trim()
  if (t === '') return undefined
  if (t === 'true' || t === 'false') return t === 'true'
  if (/^\d+$/.test(t)) return Number(t)
  if (t.startsWith('[')) { try { return JSON.parse(t) } catch { /* keep text */ } }
  return t
}
const asText = (v: any) => v === undefined || v === null ? '' : typeof v === 'string' ? v : JSON.stringify(v)
const LISTS = new Set(['repos', 'schemas', 'tables', 'extensions'])

function SourceForm({ kind, existing, datasets, labels, onClose, onDone }: { kind: Kind; existing?: SourceRow; datasets: string[]; labels: Record<string, string>; onClose: () => void; onDone: () => void }) {
  const [name, setName] = useState(existing?.name || '')
  const [cfg, setCfg] = useState<Record<string, string>>(() => Object.fromEntries(Object.keys(kind.config).map((k) => {
    const v = existing?.config[k]; return [k, LISTS.has(k) && Array.isArray(v) ? v.join(', ') : asText(v)] })))
  const [sec, setSec] = useState<Record<string, string>>({})
  const [ds, setDs] = useState(existing?.dataset || ''); const [every, setEvery] = useState(existing?.every_minutes || 360)
  const [busy, setBusy] = useState(false); const toast = useToast()
  const save = async () => {
    const config: Record<string, any> = {}
    for (const [k, v] of Object.entries(cfg)) {
      if (LISTS.has(k)) { const l = v.split(',').map((x) => x.trim()).filter(Boolean); if (l.length) config[k] = l }
      else { const x = asValue(v); if (x !== undefined) config[k] = x }
    }
    const secrets = Object.fromEntries(Object.entries(sec).filter(([, v]) => v.trim()))
    setBusy(true)
    try {
      if (existing) await api(`/sources/${existing.id}`, { method: 'PATCH', json: { name, config, secrets, dataset: ds || existing.dataset, every_minutes: every } })
      else await api('/sources', { json: { kind: kind.kind, name, config, secrets, dataset: ds || null, every_minutes: every } })
      toast(existing ? 'Saved.' : `${name || kind.label} added. Press "Sync now" for the first sync, or wait for the schedule.`); onDone()
    } catch (e: any) { toast(e.message, true) } finally { setBusy(false) }
  }
  return <Modal title={existing ? existing.name : `Add ${kind.label}`} onClose={onClose} footer={<><button className="btn" onClick={onClose}>Cancel</button>
    <button className="btn primary" disabled={busy} onClick={save}>{busy ? 'Saving…' : 'Save'}</button></>}>
    <label className="f">Name<input type="text" value={name} onChange={(e) => setName(e.target.value)} placeholder={kind.label} /></label>
    {Object.entries(kind.config).map(([k, help]) => <label key={k} className="f">{k}<input type="text" value={cfg[k] || ''}
      onChange={(e) => setCfg({ ...cfg, [k]: e.target.value })} /><span className="tiny muted">{help}{LISTS.has(k) ? ' (comma-separated)' : ''}</span></label>)}
    {Object.entries(kind.secrets).map(([k, help]) => <label key={k} className="f">{k}<input type="password" autoComplete="off" value={sec[k] || ''}
      placeholder={existing?.secrets[k] ? `saved (${existing.secrets[k]}); type to replace` : ''} onChange={(e) => setSec({ ...sec, [k]: e.target.value })} />
      <span className="tiny muted">{help} Stored encrypted; never shown again.</span></label>)}
    <label className="f">Into data set<select value={ds} onChange={(e) => setDs(e.target.value)}>
      {!existing && <option value="">A new data set named after this source</option>}
      {datasets.map((n) => <option key={n} value={n}>{labels[n] || n}</option>)}</select>
      <span className="tiny muted">Who can see the files is set for the data set on the Access tab.</span></label>
    <label className="f">Keep up to date<select value={every} onChange={(e) => setEvery(Number(e.target.value))}>
      {EVERY.map(([m, l]) => <option key={m} value={m}>{l}</option>)}</select></label>
    {existing?.webhook && <p className="tiny muted">Webhook (GitHub → Settings → Webhooks, content type application/json, the same secret as webhook_secret): <code>{location.origin}{existing.webhook}</code></p>}
  </Modal>
}
