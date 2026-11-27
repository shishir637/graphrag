import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { ago, api, can, fmtBytes, type Job } from '../api'
import type { Session } from '../App'
import { Empty, ErrorBox, Icon, JobPanel, Loading, PageHead, useJob, useLoad, useToast } from '../ui'

interface Table { name: string; table: string; size: number; modified: string; rows: number | null; columns: number }

export default function DataTables({ s }: { s: Session }) {
  const { data, error, reload } = useLoad<{ tables: Table[] }>('/tables')
  const running = useLoad<{ running: Job | null }>('/jobs')
  const job = useJob(() => reload())
  const toast = useToast(); const input = useRef<HTMLInputElement>(null); const [busy, setBusy] = useState(false)
  const admin = can(s.user, 'owner', 'admin')
  useEffect(() => { if (running.data?.running?.kind === 'data') job.attach(running.data.running) }, [running.data])  // eslint-disable-line
  const upload = async (files: FileList | null) => {
    if (!files?.length) return
    const fd = new FormData(); Array.from(files).forEach((f) => fd.append('files', f)); setBusy(true)
    try { const r = await api<{ saved: string[]; errors: string[] }>('/tables', { form: fd }); r.errors.length ? toast(r.errors.join('; '), true) : toast(`Uploaded ${r.saved.length}. Prepare data to profile it.`); reload() }
    catch (e: any) { toast(e.message, true) } finally { setBusy(false); if (input.current) input.current.value = '' }
  }
  if (error) return <div className="page"><ErrorBox error={error} retry={reload} /></div>
  if (!data) return <div className="page"><Loading /></div>
  return <div className="page">
    <PageHead title="Data tables" sub={<>Tables behind your business numbers. Agents never write SQL against them: they ask for a <Link to="/knowledge/metrics">metric</Link>, and Context Layer computes it from the reviewed definition.</>}>
      {admin && <><button className="btn" disabled={busy} onClick={() => input.current?.click()}><Icon name="upload" size={16} />Upload tables</button>
        <input ref={input} type="file" multiple hidden accept=".csv,.xlsx,.xls,.json,.jsonl,.ndjson,.parquet" onChange={(e) => upload(e.target.files)} />
        <button className="btn primary" disabled={job.running || !data.tables.length} onClick={() => job.start('data')}><Icon name="refresh" size={16} />Prepare data</button></>}
    </PageHead>
    <JobPanel job={job.job} />
    <div className="card">{data.tables.length ?
      <div className="tablewrap"><table className="t"><thead><tr><th>Table</th><th>File</th><th>Rows</th><th>Columns</th><th>Size</th><th>Modified</th></tr></thead>
        <tbody>{data.tables.map((t) => <tr key={t.name}><td><b>{t.table}</b></td><td className="mono">{t.name}</td><td>{t.rows?.toLocaleString() ?? '—'}</td><td>{t.columns || '—'}</td>
          <td className="muted">{fmtBytes(t.size)}</td><td className="muted small">{ago(t.modified)}</td></tr>)}</tbody></table></div>
      : <Empty icon="table" title="No data tables" action={admin && <button className="btn primary" onClick={() => input.current?.click()}><Icon name="upload" size={16} />Upload CSV, Excel or Parquet</button>}>
        One file per table: orders, customers, products… Prepare data then profiles them and checks every metric definition against them.</Empty>}</div>
    <div className="callout" style={{ marginTop: 16 }}>Rows and columns appear after <b>Prepare data</b>. Joins and metric definitions live in <code>semantic_layer/</code> and are changed by review, so every agent gets the same number.</div>
  </div>
}
