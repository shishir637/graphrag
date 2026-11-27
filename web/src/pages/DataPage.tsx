import { useRef, useState } from 'react'
import { ago, api, fmtBytes } from '../api'
import { Checks, type AccessData } from '../admin'
import { Icon, Loading, Modal, askConfirm, useLoad, useToast } from '../ui'

interface File { name: string; size: number; modified: string; datasets: string[]; private_to: string[]; passages: number; status: 'ready' | 'new' | 'changed'; editable: boolean }
interface DS { name: string; label: string; kind: string; team?: string; files: number }
interface Data { files: File[]; datasets: DS[]; labels: Record<string, string>; upload_to: string[]; tables: { name: string; rows: number | null; columns: number; size: number; modified: string }[]
  company_admin: boolean; pending: number; job: { label: string; status: string } | null }

const PRIVATE = '__private__'

/* Files, and who can see them. Left: data sets. Right: the files in the chosen one and who has access to it. */
export default function DataPage() {
  const d = useLoad<Data>('/data'); const acc = useLoad<AccessData>('/access'); const toast = useToast()
  const [sel, setSel] = useState(''); const [q, setQ] = useState('')
  const [upload, setUpload] = useState(false); const [edit, setEdit] = useState<File | null>(null); const [newDs, setNewDs] = useState(false)
  if (d.error) return <div className="callout bad">{d.error}</div>
  if (!d.data) return <Loading />
  const x = d.data; const label = (n: string) => x.labels[n] || n
  const ql = q.trim().toLowerCase()
  const files = x.files.filter((f) => (!sel || (sel === PRIVATE ? f.private_to.length > 0 : f.datasets.includes(sel))) && (!ql || f.name.toLowerCase().includes(ql)))
  const ds = acc.data?.datasets.find((z) => z.name === sel)
  const prepare = async () => {
    try { await api('/data/prepare', { method: 'POST' }); toast('Preparing files for Claude. This takes a few minutes.'); setTimeout(d.reload, 1500) } catch (e: any) { toast(e.message, true) }
  }
  const del = async (f: File) => {
    if (!(await askConfirm(`Delete ${f.name}? Claude stops finding it after the next preparation.`, 'Delete'))) return
    try { await api(`/data/files/${encodeURIComponent(f.name)}`, { method: 'DELETE' }); d.reload() } catch (e: any) { toast(e.message, true) }
  }
  const privateCount = x.files.filter((f) => f.private_to.length).length
  const cur = x.datasets.find((z) => z.name === sel)
  const delDs = async (n: string) => {
    if ((cur?.files || 0) > 0) { toast('Move its files to another data set first (Access button on each file).', true); return }
    if (!(await askConfirm(`Delete the data set ${label(n)}? Teams, roles and people lose it.`, 'Delete'))) return
    try { await api(`/access/datasets/${encodeURIComponent(n)}`, { method: 'DELETE' }); setSel(''); d.reload(); acc.reload() } catch (e: any) { toast(e.message, true) }
  }
  return <div className="split">
    <aside className="sidelist">
      <button className={!sel ? 'on' : ''} onClick={() => setSel('')}><span>All files</span><span className="muted">{x.files.length}</span></button>
      <div className="sidecap">Data sets</div>
      {x.datasets.map((z) => <button key={z.name} className={sel === z.name ? 'on' : ''} onClick={() => setSel(z.name)}>
        <span>{z.label}</span><span className="muted">{z.files}</span></button>)}
      {privateCount > 0 && <button className={sel === PRIVATE ? 'on' : ''} onClick={() => setSel(PRIVATE)}><span>Personal files</span><span className="muted">{privateCount}</span></button>}
      {x.company_admin && <button className="addds" onClick={() => setNewDs(true)}><Icon name="plus" size={14} />New data set</button>}
      <div className="tiny muted" style={{ padding: '8px 10px' }}>Each team gets a data set automatically (Teams tab).</div>
    </aside>
    <section style={{ minWidth: 0 }}>
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 12 }}>
        <h1 style={{ margin: 0 }}>{!sel ? 'All files' : sel === PRIVATE ? 'Personal files' : label(sel)}</h1>
        <div className="row">{x.company_admin && cur?.kind === 'custom' && <button className="btn ghost sm" style={{ color: 'var(--bad)' }} onClick={() => delDs(cur.name)}>Delete data set</button>}
          <input type="search" placeholder="Find a file" value={q} onChange={(e) => setQ(e.target.value)} style={{ width: 190 }} aria-label="Find a file" />
          <button className="btn primary" onClick={() => setUpload(true)}><Icon name="upload" size={15} />Upload</button></div>
      </div>

      {ds && <div className="whobar small"><b>Who can see this:</b> {ds.who.everyone ? 'everyone' : [
        ...ds.who.teams.map((t) => `${t} team`), ...ds.who.roles.map((r) => `role ${r}`), ...(ds.who.people.length ? [`${ds.who.people.length} ${ds.who.people.length === 1 ? 'person' : 'people'} directly`] : [])].join(', ') || 'only company admins'}
        {acc.data?.company_admin && <a href="#access?datasets" style={{ marginLeft: 8 }}>change</a>}</div>}
      {sel === PRIVATE && <div className="whobar small">Files people added from Claude for themselves. Only they, and anyone they shared their layer with, can see them.</div>}

      {(x.pending > 0 || x.job) && <div className="callout" style={{ marginBottom: 12, display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12 }}>
        <span>{x.job && x.job.status === 'running' ? `${x.job.label}…` : `${x.pending} ${x.pending === 1 ? 'file is' : 'files are'} not ready for Claude yet.`}</span>
        {!(x.job && x.job.status === 'running') ? <button className="btn sm primary" onClick={prepare}>Prepare now</button> : <button className="btn sm" onClick={d.reload}>Refresh</button>}</div>}

      <div className="card"><table className="t plain">
        <thead><tr><th>File</th><th>Who can see it</th><th>Ready</th><th /></tr></thead>
        <tbody>{files.map((f) => <tr key={f.name}>
          <td><b className="small">{f.name}</b><div className="tiny muted">{fmtBytes(f.size)} · {ago(f.modified)}</div></td>
          <td className="small">{f.private_to.length ? <span className="tag">Only {f.private_to.join(', ')}</span> :
            f.datasets.map((g) => <span key={g} className={`tag${g === 'public' ? ' strong' : ''}`} style={{ marginRight: 4 }}>{label(g)}</span>)}</td>
          <td className="small">{f.status === 'ready' ? <span className="muted">{f.passages} passages</span> : <span className="warn-text">not yet</span>}</td>
          <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>{f.editable && <>
            <button className="btn sm" onClick={() => setEdit(f)}>Access</button>{' '}
            <button className="btn ghost sm" onClick={() => del(f)} aria-label={`Delete ${f.name}`} title="Delete"><Icon name="x" size={14} /></button></>}</td>
        </tr>)}
          {!files.length && <tr><td colSpan={4} className="muted small" style={{ padding: 18 }}>No files here yet. Upload some to make them available to Claude.</td></tr>}
        </tbody></table></div>

      {x.company_admin && !sel && <Tables tables={x.tables} onDone={d.reload} />}
    </section>
    {upload && <Upload data={x} preset={sel && sel !== PRIVATE ? sel : ''} onClose={() => setUpload(false)} onDone={() => { setUpload(false); d.reload(); acc.reload() }} />}
    {newDs && <NewDataSet onClose={() => setNewDs(false)} onDone={(n) => { setNewDs(false); d.reload(); acc.reload(); setSel(n) }} />}
    {edit && <FileAccess f={edit} data={x} onClose={() => setEdit(null)} onDone={() => { setEdit(null); d.reload(); acc.reload() }} />}
  </div>
}

function Upload({ data, preset, onClose, onDone }: { data: Data; preset: string; onClose: () => void; onDone: () => void }) {
  const [sets, setSets] = useState<string[]>(preset && data.upload_to.includes(preset) ? [preset] : data.upload_to.length === 1 ? data.upload_to : [])
  const [picked, setPicked] = useState<FileList | null>(null); const [busy, setBusy] = useState(false); const toast = useToast()
  const inp = useRef<HTMLInputElement>(null)
  const go = async () => {
    if (!picked?.length) return
    const fd = new FormData(); Array.from(picked).forEach((f) => fd.append('files', f)); fd.append('datasets', sets.join(','))
    setBusy(true)
    try { const r = await api<{ saved: any[]; errors: string[] }>('/data/files', { form: fd })
      toast(`${r.saved.length} added${r.errors.length ? `; ${r.errors.join('; ')}` : ''}. Press "Prepare now" when you are done adding.`, r.errors.length > 0); onDone() }
    catch (e: any) { toast(e.message, true) } finally { setBusy(false) }
  }
  return <Modal title="Upload files" onClose={onClose} footer={<><button className="btn" onClick={onClose}>Cancel</button>
    <button className="btn primary" disabled={!picked?.length || !sets.length || busy} onClick={go}>{busy ? 'Uploading…' : 'Upload'}</button></>}>
    <div className={`drop${picked?.length ? ' has' : ''}`} onClick={() => inp.current?.click()}
      onDragOver={(e) => e.preventDefault()} onDrop={(e) => { e.preventDefault(); setPicked(e.dataTransfer.files) }}>
      <Icon name="upload" size={20} /><div>{picked?.length ? Array.from(picked).map((f) => f.name).join(', ') : 'Drop files here or click to choose'}</div>
      <div className="tiny muted">PDF, Word, PowerPoint, Excel, text, Markdown, HTML</div>
      <input ref={inp} type="file" multiple hidden onChange={(e) => setPicked(e.target.files)} /></div>
    <div className="f" style={{ marginTop: 14 }}><span className="flabel">Who can see them</span>
      <Checks options={data.upload_to.map((n) => ({ v: n, label: data.labels[n] || n }))} value={sets} onChange={setSets} /></div>
  </Modal>
}

function FileAccess({ f, data, onClose, onDone }: { f: File; data: Data; onClose: () => void; onDone: () => void }) {
  const [sets, setSets] = useState<string[]>(f.datasets); const toast = useToast()
  const save = async () => {
    try { await api(`/data/files/${encodeURIComponent(f.name)}`, { method: 'PATCH', json: { groups: sets } }); toast('Saved. Claude uses it after the next preparation.'); onDone() } catch (e: any) { toast(e.message, true) }
  }
  return <Modal title={f.name} onClose={onClose} footer={<><button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" disabled={!sets.length} onClick={save}>Save</button></>}>
    <span className="flabel">Who can see it</span>
    <Checks options={data.upload_to.map((n) => ({ v: n, label: data.labels[n] || n }))} value={sets} onChange={setSets} />
    <p className="tiny muted">A file can be in several data sets. Anyone with access to any of them can see it.</p>
  </Modal>
}

function Tables({ tables, onDone }: { tables: Data['tables']; onDone: () => void }) {
  const inp = useRef<HTMLInputElement>(null); const toast = useToast()
  const up = async (fl: FileList | null) => {
    if (!fl?.length) return
    const fd = new FormData(); Array.from(fl).forEach((f) => fd.append('files', f))
    try { await api('/tables', { form: fd }); toast('Table added. Prepare data from Settings when ready.'); onDone() } catch (e: any) { toast(e.message, true) }
  }
  return <>
    <div className="row" style={{ justifyContent: 'space-between', margin: '26px 0 10px' }}><h2 style={{ fontSize: 17, margin: 0 }}>Data tables <span className="muted small" style={{ fontWeight: 400 }}>for metrics; every connected person with the Metrics connector can ask about them</span></h2>
      <button className="btn sm" onClick={() => inp.current?.click()}><Icon name="upload" size={14} />Add table</button>
      <input ref={inp} type="file" accept=".csv,.xlsx,.xls,.json,.jsonl,.parquet" multiple hidden onChange={(e) => up(e.target.files)} /></div>
    <div className="card"><table className="t plain"><tbody>{tables.map((t) => <tr key={t.name}>
      <td className="small"><b>{t.name}</b></td><td className="small muted">{t.rows != null ? `${t.rows} rows · ${t.columns} columns` : 'not prepared yet'}</td><td className="small muted">{ago(t.modified)}</td></tr>)}
      {!tables.length && <tr><td className="muted small" style={{ padding: 18 }}>No tables yet.</td></tr>}</tbody></table></div>
  </>
}

function NewDataSet({ onClose, onDone }: { onClose: () => void; onDone: (name: string) => void }) {
  const [name, setName] = useState(''); const [files, setFiles] = useState<FileList | null>(null); const [busy, setBusy] = useState(false); const toast = useToast()
  const inp = useRef<HTMLInputElement>(null)
  const save = async () => {
    setBusy(true)
    try {
      const r = await api<{ name: string }>('/access/datasets', { json: { name } })
      if (files?.length) {
        const fd = new FormData(); Array.from(files).forEach((f) => fd.append('files', f)); fd.append('datasets', r.name)
        await api('/data/files', { form: fd })
      }
      toast(`${name} created${files?.length ? ` with ${files.length} ${files.length === 1 ? 'file' : 'files'}` : ''}. Choose who sees it on the Access tab.`); onDone(r.name)
    } catch (e: any) { toast(e.message, true) } finally { setBusy(false) }
  }
  return <Modal title="New data set" onClose={onClose} footer={<><button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" disabled={!name.trim() || busy} onClick={save}>{busy ? 'Creating…' : 'Create'}</button></>}>
    <p className="small muted" style={{ marginTop: 0 }}>A group of files that only some people should see, such as board papers or salaries. Only company admins see it until you give it to a team, a role or a person.</p>
    <label className="f">Name<input type="text" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Board papers" autoFocus /></label>
    <span className="flabel">Files</span>
    <div className={`drop${files?.length ? ' has' : ''}`} onClick={() => inp.current?.click()}
      onDragOver={(e) => e.preventDefault()} onDrop={(e) => { e.preventDefault(); setFiles(e.dataTransfer.files) }}>
      <Icon name="upload" size={18} /><div className="small">{files?.length ? Array.from(files).map((f) => f.name).join(', ') : 'Drop files here or click to choose (you can also add them later)'}</div>
      <input ref={inp} type="file" multiple hidden onChange={(e) => setFiles(e.target.files)} /></div>
  </Modal>
}
