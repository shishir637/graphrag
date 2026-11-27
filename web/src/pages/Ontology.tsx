import { useEffect, useState } from 'react'
import { api, type Job } from '../api'
import { askConfirm, Badge, Icon, JobPanel, useJob, useLoad, useToast } from '../ui'

interface EType { name: string; description: string; label: string; examples?: string[]; votes?: number; status?: string; merged?: string[] }
interface RType { name: string; source?: string; target?: string; description: string; examples?: string[]; votes?: number }
interface Proposal { created: string; model: string; where: string; passages: number; documents: number; seconds: number
  entity_types: EType[]; relation_types: RType[]; dropped_without_examples: string[]; current_not_seen: string[]; current_source: string }
interface O { current: { source: string; entity_types: EType[]; relation_types: RType[] }; proposal: Proposal | null
  ai: { provider: string; model: string; where: string; local: boolean; problems: string[] } }

/** The knowledge graph's types: proposed by an AI model (a free local one by default), approved here. */
export default function Ontology({ onApplied }: { onApplied?: () => void }) {
  const { data, reload } = useLoad<O>('/ontology')
  const job = useJob(() => reload())
  const toast = useToast()
  const [ents, setEnts] = useState<Record<string, boolean>>({})
  const [rels, setRels] = useState<Record<string, boolean>>({})
  const [keepOld, setKeepOld] = useState<Record<string, boolean>>({})
  const p = data?.proposal
  useEffect(() => {
    if (!p) return
    setEnts(Object.fromEntries(p.entity_types.map((e) => [e.name, true])))
    setRels(Object.fromEntries(p.relation_types.map((r) => [r.name, true])))
    setKeepOld(Object.fromEntries(p.current_not_seen.map((n) => [n, false])))
  }, [p?.created])
  if (!data) return null
  const propose = async () => {
    try { const j = await api<Job>('/ontology/propose', { method: 'POST' }); job.attach(j) } catch (e: any) { toast(e.message, true) }
  }
  const apply = async () => {
    if (!p) return
    const cur = Object.fromEntries(data.current.entity_types.map((e) => [e.name, e]))
    const chosen = [...p.entity_types.filter((e) => ents[e.name]), ...Object.keys(keepOld).filter((n) => keepOld[n]).map((n) => cur[n])]
    if (!chosen.length) { toast('Keep at least one type', true); return }
    if (!await askConfirm(`Use these ${chosen.length} entity types and ${p.relation_types.filter((r) => rels[r.name]).length} link types for the knowledge graph? The change is recorded and can be undone on the Layers page; the graph uses them after its next build.`, 'Use these types')) return
    try {
      const r = await api<{ next: string }>('/ontology/apply', { method: 'POST', json: { entity_types: chosen, relation_types: p.relation_types.filter((x) => rels[x.name]) } })
      toast('Ontology saved. Rebuild the graph to use it.'); reload(); onApplied?.(); void r
    } catch (e: any) { toast(e.message, true) }
  }
  const ai = data.ai
  return <div className="card" style={{ marginTop: 16 }}>
    <div className="hd"><div><h2>Ontology: the kinds of things in the graph</h2>
      <div className="muted small">Now: {data.current.entity_types.length} entity types ({data.current.source}). An AI model reads a sample of your passages and proposes types with real examples; you choose what to keep.</div></div>
      <button className="btn" disabled={job.running || ai.problems.length > 0} onClick={propose}><Icon name="graph" size={16} />Propose types with AI</button></div>
    <div className="bd">
      <JobPanel job={job.job} />
      <div className="small" style={{ marginBottom: 8 }}>
        Model: <b>{ai.model || 'none chosen'}</b> at {ai.where} {ai.local ? <Badge kind="ok">stays on this machine or network</Badge> : <Badge kind="warn">passages leave this machine</Badge>}
        {ai.problems.length > 0 && <div className="muted">{ai.problems.join(' ')} Free option: install Ollama, run <code>ollama pull qwen3.5:4b</code>, then Settings → AI extraction → Local model.</div>}
      </div>
      <div className="small muted" style={{ marginBottom: 8 }}>Current types: {data.current.entity_types.map((e) => e.name).join(', ')}</div>
      {p && <>
        <div className="sep" />
        <div className="small" style={{ marginBottom: 6 }}>Proposed by {p.model} from {p.passages} passages in {p.documents} documents ({Math.round(p.seconds)} s). Every type has examples copied from your documents.</div>
        <table className="tbl"><thead><tr><th></th><th>Entity type</th><th>What it is</th><th>Examples from your documents</th><th></th></tr></thead><tbody>
          {p.entity_types.map((e) => <tr key={e.name}>
            <td><input type="checkbox" checked={!!ents[e.name]} onChange={(ev) => setEnts({ ...ents, [e.name]: ev.target.checked })} /></td>
            <td><b>{e.name}</b>{e.merged?.length ? <div className="tiny muted">also {e.merged.join(', ')}</div> : null}</td>
            <td className="small">{e.description}<div className="tiny muted">looks for: {e.label}</div></td>
            <td className="small">{(e.examples || []).join(', ')}</td>
            <td>{e.status === 'new' ? <Badge kind="ok">new</Badge> : <Badge>already there</Badge>}</td></tr>)}
        </tbody></table>
        {p.current_not_seen.length > 0 && <div className="small" style={{ marginTop: 8 }}>Current types not seen in these documents (tick to keep):{' '}
          {p.current_not_seen.map((n) => <label key={n} style={{ marginRight: 10 }}><input type="checkbox" checked={!!keepOld[n]} onChange={(ev) => setKeepOld({ ...keepOld, [n]: ev.target.checked })} /> {n}</label>)}</div>}
        {p.relation_types.length > 0 && <><div className="sep" /><table className="tbl"><thead><tr><th></th><th>Link type</th><th>From → to</th><th>Examples</th></tr></thead><tbody>
          {p.relation_types.map((r) => <tr key={r.name}>
            <td><input type="checkbox" checked={!!rels[r.name]} onChange={(ev) => setRels({ ...rels, [r.name]: ev.target.checked })} /></td>
            <td><b>{r.name}</b><div className="tiny muted">{r.description}</div></td><td className="small">{r.source || 'any'} → {r.target || 'any'}</td>
            <td className="small">{(r.examples || []).join('; ')}</td></tr>)}</tbody></table></>}
        {p.dropped_without_examples.length > 0 && <div className="tiny muted" style={{ marginTop: 6 }}>Left out (no real example in the passages): {p.dropped_without_examples.join(', ')}</div>}
        <div className="row" style={{ marginTop: 10 }}><button className="btn primary" onClick={apply}>Use the ticked types</button></div>
      </>}
    </div></div>
}
