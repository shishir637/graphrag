import { useEffect } from 'react'
import { ago, can, type Job } from '../api'
import type { Session } from '../App'
import { Empty, ErrorBox, Icon, JobPanel, Loading, PageHead, useJob, useLoad } from '../ui'

interface Suite { id: string; name: string; what: string; last_run: string | null; scores: Record<string, number> }
const pretty = (k: string) => k.replace(/^summary\./, '').replace(/[._]/g, ' ').replace(/\bllm\b/i, 'LLM')
const fmt = (v: number) => (v <= 1 && v >= 0 && !Number.isInteger(v) ? `${Math.round(v * 1000) / 10}%` : Number.isInteger(v) ? v.toLocaleString() : v.toFixed(2))

export default function Quality({ s }: { s: Session }) {
  const { data, error, reload } = useLoad<{ suites: Suite[] }>('/quality')
  const running = useLoad<{ running: Job | null }>('/jobs')
  const job = useJob(() => reload())
  useEffect(() => { if (running.data?.running?.kind.startsWith('eval')) job.attach(running.data.running) }, [running.data])  // eslint-disable-line
  const admin = can(s.user, 'owner', 'admin')
  if (error) return <div className="page"><ErrorBox error={error} retry={reload} /></div>
  if (!data) return <div className="page"><Loading /></div>
  return <div className="page">
    <PageHead title="Quality" sub="Test sets with known answers, scored the same way every time. Run them after changing documents, metrics or settings, and before showing results to anyone." />
    <JobPanel job={job.job} />
    <div className="grid g3">{data.suites.map((su) => { const entries = Object.entries(su.scores).slice(0, 8)
      return <div key={su.id} className="card"><div className="hd"><div><h2>{su.name}</h2><div className="tiny muted">{su.last_run ? `last run ${ago(su.last_run)}` : 'never run'}</div></div>
        {admin && <button className="btn sm" disabled={job.running} onClick={() => job.start(su.id)}><Icon name="play" size={14} />Run</button>}</div>
        <div className="bd small"><p className="muted" style={{ marginTop: 0 }}>{su.what}</p>
          {entries.length ? entries.map(([k, v]) => <div key={k} className="between" style={{ padding: '5px 0', borderBottom: '1px solid var(--line-2)' }}><span>{pretty(k)}</span><b>{fmt(v)}</b></div>)
            : <Empty icon="check" title="No results yet">Run the suite to get scores.</Empty>}</div></div> })}</div>
    <div className="callout" style={{ marginTop: 16 }}>Runs are free: no AI model judges them; they compare against hand-checked gold answers. Test questions live with the engine (<code>eval_questions.json</code>, <code>semantic_layer/eval_questions.yaml</code>); add your own to cover what your agents are really asked.</div>
  </div>
}
