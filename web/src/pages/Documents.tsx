import { useEffect, useRef, useState } from 'react'
import { ago, api, can, fmtBytes, type Doc, type Job } from '../api'
import type { Session } from '../App'
import { Badge, Empty, ErrorBox, GroupChips, Icon, JobPanel, Loading, Modal, PageHead, useJob, useLoad, useToast } from '../ui'

const STATUS: Record<Doc['status'], { kind: 'ok' | 'warn' | 'brand'; label: string }> = {
  ready: { kind: 'ok', label: 'Ready' }, new: { kind: 'brand', label: 'Not prepared yet' }, changed: { kind: 'warn', label: 'Changed since last prepare' },
}

export default function Documents({ s }: { s: Session }) {
  const { data, error, reload } = useLoad<{ documents: Doc[]; groups: string[] }>('/documents')
  const running = useLoad<{ running: Job | null }>('/jobs')
  const job = useJob(() => reload())
  const toast = useToast()
  const [upload, setUpload] = useState(false)
  const [edit, setEdit] = useState<Doc | null>(null)
  const [del, setDel] = useState<Doc | null>(null)
  const [q, setQ] = useState('')
  const admin = can(s.user, 'owner', 'admin')
  useEffect(() => { if (running.data?.running?.kind === 'documents') job.attach(running.data.running) }, [running.data])  // eslint-disable-line
  if (error) return <div className="page"><ErrorBox error={error} retry={reload} /></div>
  if (!data) return <div className="page"><Loading /></div>
  const pending = data.documents.filter((d) => d.status !== 'ready').length
  const shown = data.documents.filter((d) => !q || d.name.toLowerCase().includes(q.toLowerCase()) || d.groups.some((g) => g.includes(q.toLowerCase())))
  return <div className="page">
    <PageHead title="Documents" sub="Files your agents can search and quote. Each file belongs to access groups; an agent only sees files whose groups its key allows.">
      {admin && <><button className="btn" onClick={() => setUpload(true)}><Icon name="upload" size={16} />Upload</button>
        <button className="btn primary" disabled={job.running || !data.documents.length} onClick={() => job.start('documents')}><Icon name="refresh" size={16} />Prepare documents{pending ? ` (${pending})` : ''}</button></>}
    </PageHead>
    <JobPanel job={job.job} />
    {pending > 0 && !job.running && <div className="callout warn" style={{ marginBottom: 16 }}>{pending} file{pending > 1 ? 's are' : ' is'} not searchable yet. Click <b>Prepare documents</b> to read, split and index {pending > 1 ? 'them' : 'it'}.</div>}
    <div className="card">
      {data.documents.length ? <>
        <div className="hd"><input type="search" placeholder="Filter by name or group" value={q} onChange={(e) => setQ(e.target.value)} style={{ maxWidth: 320 }} aria-label="Filter documents" /><span className="muted small">{data.documents.length} files</span></div>
        <div className="tablewrap"><table className="t"><thead><tr><th>File</th><th>Access groups</th><th>Status</th><th>Passages</th><th>Size</th><th>Modified</th>{admin && <th />}</tr></thead>
          <tbody>{shown.map((d) => <tr key={d.name}>
            <td><b>{d.name}</b></td><td><GroupChips groups={d.groups} /></td>
            <td><Badge kind={STATUS[d.status].kind} dot>{STATUS[d.status].label}</Badge></td>
            <td>{d.passages || '—'}</td><td className="muted">{fmtBytes(d.size)}</td><td className="muted small">{ago(d.modified)}</td>
            {admin && <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}><button className="btn sm" onClick={() => setEdit(d)}>Groups</button> <button className="btn sm danger" onClick={() => setDel(d)}>Delete</button></td>}
          </tr>)}</tbody></table></div></>
        : <Empty icon="file" title="No documents yet" action={admin && <button className="btn primary" onClick={() => setUpload(true)}><Icon name="upload" size={16} />Upload documents</button>}>
          PDF, Word, PowerPoint, Excel, HTML, Markdown or text. Policies, handbooks, product docs, contracts: anything your agents should answer from.</Empty>}
    </div>
    {upload && <UploadDialog groups={data.groups} onClose={() => setUpload(false)} onDone={(n) => { setUpload(false); reload(); toast(`Uploaded ${n} file${n > 1 ? 's' : ''}. Prepare documents to make ${n > 1 ? 'them' : 'it'} searchable.`) }} />}
    {edit && <GroupsDialog doc={edit} groups={data.groups} onClose={() => setEdit(null)} onDone={() => { setEdit(null); reload(); toast('Groups saved. Prepare documents again to apply them to agents.') }} />}
    {del && <Modal title={`Delete ${del.name}?`} onClose={() => setDel(null)} footer={<><button className="btn" onClick={() => setDel(null)}>Cancel</button>
      <button className="btn primary" style={{ background: 'var(--bad)', borderColor: 'var(--bad)' }} onClick={async () => {
        try { await api(`/documents/${encodeURIComponent(del.name)}`, { method: 'DELETE' }); setDel(null); reload(); toast('Deleted. Prepare documents to remove it from search.') } catch (e: any) { toast(e.message, true) } }}>Delete file</button></>}>
      <p>The file is removed from the documents folder. Agents stop seeing it after the next <b>Prepare documents</b>.</p></Modal>}
  </div>
}

function GroupPicker({ all, value, onChange }: { all: string[]; value: string[]; onChange: (v: string[]) => void }) {
  const [extra, setExtra] = useState('')
  const opts = Array.from(new Set([...all, ...value])).sort()
  return <div className="stack">
    <div className="checks">{opts.map((g) => <label key={g} className="check"><input type="checkbox" checked={value.includes(g)} onChange={(e) => onChange(e.target.checked ? [...value, g] : value.filter((x) => x !== g))} />{g}</label>)}</div>
    <div className="row"><input type="text" placeholder="New group, e.g. hr or finance" value={extra} onChange={(e) => setExtra(e.target.value.toLowerCase())} style={{ maxWidth: 260 }}
      onKeyDown={(e) => { if (e.key === 'Enter' && extra.trim()) { e.preventDefault(); onChange([...value, extra.trim()]); setExtra('') } }} />
      <button type="button" className="btn sm" disabled={!extra.trim()} onClick={() => { onChange([...value, extra.trim()]); setExtra('') }}>Add group</button></div>
  </div>
}

function UploadDialog({ groups, onClose, onDone }: { groups: string[]; onClose: () => void; onDone: (n: number) => void }) {
  const [files, setFiles] = useState<File[]>([]); const [g, setG] = useState<string[]>(['public']); const [busy, setBusy] = useState(false)
  const toast = useToast(); const input = useRef<HTMLInputElement>(null)
  const send = async () => {
    const fd = new FormData(); files.forEach((f) => fd.append('files', f)); fd.append('groups', g.join(','))
    setBusy(true)
    try { const r = await api<{ saved: unknown[]; errors: string[] }>('/documents', { form: fd }); if (r.errors.length) toast(r.errors.join('; '), true); onDone(r.saved.length) }
    catch (e: any) { toast(e.message, true) } finally { setBusy(false) }
  }
  return <Modal title="Upload documents" onClose={onClose} footer={<><button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" disabled={!files.length || !g.length || busy} onClick={send}>Upload {files.length || ''}</button></>}>
    <div className="stack">
      <div className="empty" style={{ border: '2px dashed var(--line)', borderRadius: 10, padding: 24, cursor: 'pointer' }} onClick={() => input.current?.click()}
        onDragOver={(e) => e.preventDefault()} onDrop={(e) => { e.preventDefault(); setFiles([...files, ...Array.from(e.dataTransfer.files)]) }}>
        <Icon name="upload" size={22} /><div style={{ marginTop: 6 }}><b>Drop files here</b> or click to choose</div><div className="tiny muted">PDF, DOCX, PPTX, XLSX, HTML, MD, TXT · up to 200 MB each</div>
        <input ref={input} type="file" multiple hidden onChange={(e) => setFiles([...files, ...Array.from(e.target.files || [])])} /></div>
      {files.length > 0 && <div className="small">{files.map((f) => <div key={f.name} className="between"><span>{f.name}</span><span className="muted">{fmtBytes(f.size)}</span></div>)}</div>}
      <div><b className="small">Who may read these files</b><div className="tiny muted" style={{ margin: '2px 0 8px' }}>Agents whose key includes one of these groups will see them.</div><GroupPicker all={groups} value={g} onChange={setG} /></div>
    </div></Modal>
}

function GroupsDialog({ doc, groups, onClose, onDone }: { doc: Doc; groups: string[]; onClose: () => void; onDone: () => void }) {
  const [g, setG] = useState(doc.groups); const toast = useToast()
  return <Modal title={`Access groups: ${doc.name}`} onClose={onClose} footer={<><button className="btn" onClick={onClose}>Cancel</button>
    <button className="btn primary" disabled={!g.length} onClick={async () => { try { await api(`/documents/${encodeURIComponent(doc.name)}`, { method: 'PATCH', json: { groups: g } }); onDone() } catch (e: any) { toast(e.message, true) } }}>Save</button></>}>
    <GroupPicker all={groups} value={g} onChange={setG} /></Modal>
}
