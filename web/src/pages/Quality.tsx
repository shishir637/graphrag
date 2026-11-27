import { useEffect, useState } from 'react'
import { ago, api } from '../api'
import { Icon, Loading, useLoad, useToast } from '../ui'

/* Quality: scores after every build, with the trend over builds. Deterministic: no AI model judges anything; the
   answers are compared with hand-checked gold answers (app/quality.py). */
interface Point { time: string; value: number | null; trigger: string }
interface Suite { id: string; name: string; what: string; main: string; last_run: string | null; ok: boolean | null; error: string | null
  scores: Record<string, number>; change: number | null; trend: Point[] }
interface Run { id: string; time: string; suite: string; trigger: string; ok: boolean; scores: Record<string, number>; seconds: number | null }
interface Q { suites: Suite[]; after_build: boolean; runs: Run[] }

const pretty = (k: string) => k.replace(/_/g, ' ').replace(/^mrr$/, 'MRR').replace(/^ndcg@10$/, 'nDCG@10').replace(/\bf1\b/, 'F1')
const isShare = (k: string) => !['questions', 'passages'].includes(k)
const fmt = (k: string, v: number) => (isShare(k) && v >= 0 && v <= 1 ? `${Math.round(v * 1000) / 10}%` : Number.isInteger(v) ? v.toLocaleString() : v.toFixed(2))

function Trend({ points }: { points: Point[] }) {
  const p = points.filter((x) => typeof x.value === 'number') as { time: string; value: number; trigger: string }[]
  if (p.length < 2) return <div className="tiny muted" style={{ height: 44, display: 'flex', alignItems: 'center' }}>{p.length ? 'One run so far; the trend shows after the next build.' : 'No runs yet.'}</div>
  const w = 260, h = 44, lo = Math.min(...p.map((x) => x.value)), hi = Math.max(...p.map((x) => x.value)), span = hi - lo || 1
  const xy = p.map((x, i) => [4 + (i * (w - 8)) / (p.length - 1), h - 4 - ((x.value - lo) / span) * (h - 8)])
  return <svg viewBox={`0 0 ${w} ${h}`} width="100%" height={h} role="img" aria-label={`Trend over ${p.length} runs`}>
    <polyline fill="none" stroke="var(--accent, #4f6bed)" strokeWidth="2" points={xy.map((c) => c.join(',')).join(' ')} />
    {xy.map((c, i) => <circle key={i} cx={c[0]} cy={c[1]} r="2.5" fill="var(--accent, #4f6bed)"><title>{`${ago(p[i].time)}: ${Math.round(p[i].value * 1000) / 10}% (${p[i].trigger})`}</title></circle>)}
  </svg>
}

export default function QualityPage() {
  const d = useLoad<Q>('/quality'); const toast = useToast()
  const [job, setJob] = useState<string | null>(null)
  useEffect(() => {
    if (!job) return
    const t = window.setInterval(async () => {
      try { const { job: j } = await api<{ job: { status: string; error: string | null } }>(`/jobs/${job}`)
        if (j.status === 'done' || j.status === 'failed') { setJob(null); d.reload(); j.status === 'failed' ? toast(`Scoring failed: ${j.error}`, true) : toast('Scores updated') }
      } catch { /* keep polling */ }
    }, 3000)
    return () => window.clearInterval(t)
  }, [job])  // eslint-disable-line react-hooks/exhaustive-deps
  if (d.error) return <div className="callout bad">{d.error}</div>
  if (!d.data) return <Loading />
  const run = async (suites?: string[]) => {
    try { const r = await api<{ job: { id: string } }>('/quality/run', { json: suites ? { suites } : {} }); setJob(r.job.id); toast('Scoring… this takes a few minutes.') } catch (e: any) { toast(e.message, true) }
  }
  const toggle = async () => {
    try { await api('/quality/settings', { method: 'PUT', json: { after_build: !d.data!.after_build } }); d.reload() } catch (e: any) { toast(e.message, true) }
  }
  const names = Object.fromEntries(d.data.suites.map((s) => [s.id, s.name]))
  return <div>
    <div className="row" style={{ justifyContent: 'space-between', marginBottom: 12 }}>
      <h1 style={{ margin: 0 }}>Quality</h1>
      <div className="row"><label className="row small" style={{ gap: 6 }}><input type="checkbox" checked={d.data.after_build} onChange={toggle} />Score after every build</label>
        <button className="btn primary" disabled={!!job} onClick={() => run()}><Icon name="play" size={14} />{job ? 'Scoring…' : 'Run all now'}</button></div>
    </div>
    <p className="small muted" style={{ marginTop: 0 }}>Gold questions with known answers, scored the same way every time, after documents, data or the graph are prepared.
      No AI model judges the results, so runs are free and repeatable.</p>
    <div className="grid g3">{d.data.suites.map((s) => <div key={s.id} className="card">
      <div className="hd"><div><h2>{s.name}</h2><div className="tiny muted">{s.last_run ? `last run ${ago(s.last_run)}` : 'never run'}</div></div>
        <button className="btn sm" disabled={!!job} onClick={() => run([s.id])}>Run</button></div>
      <div className="bd small">
        <p className="muted" style={{ marginTop: 0 }}>{s.what}</p>
        {s.error && <div className="callout bad tiny">{s.error}</div>}
        {typeof s.scores[s.main] === 'number' && <div className="row" style={{ alignItems: 'baseline', gap: 8 }}>
          <b style={{ fontSize: 26 }}>{fmt(s.main, s.scores[s.main])}</b><span className="muted">{pretty(s.main)}</span>
          {s.change !== null && s.change !== 0 && <span className={s.change > 0 ? 'ok-text' : 'warn-text'}>{s.change > 0 ? '▲' : '▼'} {Math.abs(Math.round(s.change * 1000) / 10)} pts</span>}</div>}
        <Trend points={s.trend} />
        {Object.entries(s.scores).filter(([k]) => k !== s.main).map(([k, v]) => <div key={k} className="between" style={{ padding: '4px 0', borderBottom: '1px solid var(--line-2)' }}>
          <span>{pretty(k)}</span><b>{fmt(k, v)}</b></div>)}
      </div></div>)}</div>
    <h2 style={{ fontSize: 17, margin: '26px 0 10px' }}>Recent runs</h2>
    <div className="card"><table className="t plain"><thead><tr><th>When</th><th>Suite</th><th>Why</th><th>Main score</th></tr></thead>
      <tbody>{d.data.runs.map((r) => { const main = d.data!.suites.find((s) => s.id === r.suite)?.main || ''
        return <tr key={r.id}><td className="small">{ago(r.time)}</td><td className="small">{names[r.suite] || r.suite}</td><td className="small muted">{r.trigger}</td>
          <td className="small">{!r.ok ? <span className="warn-text">failed</span> : typeof r.scores[main] === 'number' ? fmt(main, r.scores[main]) : '–'}</td></tr> })}
        {!d.data.runs.length && <tr><td colSpan={4} className="muted small" style={{ padding: 18 }}>No runs yet. Prepare documents or data, or press "Run all now".</td></tr>}</tbody></table></div>
  </div>
}
