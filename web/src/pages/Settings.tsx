import { useState } from 'react'
import { api, can } from '../api'
import type { Session } from '../App'
import { askConfirm, Badge, ErrorBox, Loading, PageHead, useLoad, useToast } from '../ui'

interface S {
  organisation: string; version: string
  ai_model: { inside: boolean; note: string }; mcp: { url: string; public_url: string | null; transport: string; auth: string }
  models: Record<string, string>; graph_mode: string; ai_extraction: AI
  storage: { data_folder: string; graph_database: string; graph_database_ok: boolean; graph_database_message: string; disk: { free_gb: number; total_gb: number } }
}

interface Prov { label: string; base_url: string; model: string; needs_key: boolean; workers: number }
interface AI {
  provider: string; model: string; base_url: string; workers: number; key_set: boolean; key: string | null
  destination: string; local: boolean; ready: boolean; problems: string[]; providers: Record<string, Prov>
  last_build: null | { model: string; passages: number; calls: number; from_cache: number; fell_back_to_spacy: number; input_tokens: number; output_tokens: number; seconds: number }
}

const MODES: Record<string, string> = {
  fast: 'spaCy only, about 0.05 s per passage. No AI model.',
  accurate: 'GLiNER + spaCy, about 1-2 s per passage on a laptop CPU. Links are "named in the same sentence". No AI model.',
  ai: 'An AI model reads each passage and returns entities with named links (heads, owns, located in...). Many passages at a time. Uses the provider below.',
}

function AIExtraction({ ai, owner, reload }: { ai: AI; owner: boolean; reload: () => void }) {
  const toast = useToast()
  const [f, setF] = useState({ provider: ai.provider, model: ai.model, base_url: ai.base_url, workers: ai.workers, api_key: '' })
  const [busy, setBusy] = useState(false)
  const [test, setTest] = useState<null | { ok: boolean; message: string; seconds?: number; entities?: number; relations?: number }>(null)
  const p = ai.providers[f.provider]
  const host = (f.base_url.replace(/^https?:\/\//, '').split(/[/:]/)[0] || '').toLowerCase()
  const onNet = ['localhost', '127.0.0.1', 'host.docker.internal', 'ollama'].includes(host) || host.endsWith('.local') || /^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)/.test(host)
  const pick = (provider: string) => { const d = ai.providers[provider]; setF({ provider, model: d.model, base_url: d.base_url, workers: d.workers, api_key: '' }); setTest(null) }
  const save = async () => {
    setBusy(true)
    try {
      await api('/settings/ai', { method: 'PUT', json: { ...f, api_key: f.api_key ? f.api_key : null } })
      setF({ ...f, api_key: '' }); toast('Saved'); reload()
    } catch (e: any) { toast(e.message, true) } finally { setBusy(false) }
  }
  const runTest = async () => {
    setBusy(true); setTest(null)
    try { setTest(await api('/settings/ai/test', { method: 'POST' })) } catch (e: any) { setTest({ ok: false, message: e.message }) } finally { setBusy(false) }
  }
  return <div className="card" style={{ marginTop: 16 }}>
    <div className="hd"><div><h2>AI extraction (optional)</h2><div className="muted small">Used only while building the knowledge graph, never for answering. Off unless graph quality is set to "ai".</div></div>
      {ai.ready ? <Badge kind="ok" dot>ready</Badge> : <Badge kind="warn" dot>not set up</Badge>}</div>
    <div className="bd stack small">
      <div className="seg" role="group" aria-label="AI provider">{Object.entries(ai.providers).map(([k, v]) =>
        <button key={k} type="button" disabled={!owner} className={f.provider === k ? 'on' : ''} onClick={() => pick(k)}>{v.label}</button>)}</div>
      <div className="row">
        <label className="f" style={{ flex: '1 1 200px' }}>Model<input type="text" value={f.model} disabled={!owner}
          placeholder={f.provider === 'local' ? 'e.g. the name shown by "ollama list"' : 'model name'} onChange={(e) => setF({ ...f, model: e.target.value })} /></label>
        <label className="f" style={{ flex: '2 1 280px' }}>Address<input type="text" value={f.base_url} disabled={!owner} onChange={(e) => setF({ ...f, base_url: e.target.value })} /></label>
        <label className="f" style={{ flex: '0 1 110px' }}>At a time<input type="number" min={1} max={32} value={f.workers} disabled={!owner} onChange={(e) => setF({ ...f, workers: Number(e.target.value) || 1 })} /></label>
      </div>
      {p?.needs_key || f.provider === 'openai' ? <label className="f">API key{ai.key_set && f.provider === ai.provider ? <span className="muted"> (saved: {ai.key}; leave empty to keep)</span> : null}
        <input type="password" autoComplete="off" value={f.api_key} disabled={!owner} placeholder={ai.key_set ? 'leave empty to keep the saved key' : 'paste the key'} onChange={(e) => setF({ ...f, api_key: e.target.value })} /></label> : null}
      <div className={onNet ? 'muted' : 'warnline'}>
        {onNet ? 'The model runs on your own machine or network: passages stay inside.'
          : `Passages are sent to ${f.base_url.replace(/^https?:\/\//, '').split('/')[0]} during a graph build. Use a local model if documents must not leave your network.`}</div>
      {ai.problems.length > 0 && <div className="muted">{ai.problems.join(' ')}</div>}
      {owner && <div className="row"><button className="btn" disabled={busy} onClick={save}>Save</button>
        <button className="btn ghost" disabled={busy || !ai.ready} onClick={runTest}>Test connection</button>
        {ai.key_set && <button className="btn ghost" disabled={busy} onClick={async () => { try { await api('/settings/ai', { method: 'PUT', json: { ...f, api_key: '' } }); toast('Key removed'); reload() } catch (e: any) { toast(e.message, true) } }}>Remove key</button>}</div>}
      {test && <div className={test.ok ? '' : 'warnline'}>{test.ok ? `Connected: ${test.entities} entities and ${test.relations} links from a test sentence in ${test.seconds} s.` : test.message}</div>}
      {ai.last_build && <div className="muted">Last AI build: {ai.last_build.passages} passages in {ai.last_build.seconds} s ({ai.last_build.calls} calls, {ai.last_build.from_cache} from cache{ai.last_build.fell_back_to_spacy ? `, ${ai.last_build.fell_back_to_spacy} fell back to spaCy` : ''}), {ai.last_build.input_tokens.toLocaleString()} input / {ai.last_build.output_tokens.toLocaleString()} output tokens.</div>}
    </div></div>
}


interface ChatS { guests: boolean; company_key_for: string; uploads: string; company_model: string; company_key_set: boolean; company_key: string | null; models: [string, string][]; keys_url: string }

function ChatSettings({ owner }: { owner: boolean }) {
  const toast = useToast()
  const { data, reload } = useLoad<ChatS>('/settings/chat')
  const [f, setF] = useState<null | { guests: boolean; company_key_for: string; uploads: string; company_model: string; company_key: string }>(null)
  const [busy, setBusy] = useState(false)
  if (!data) return null
  const v = f ?? { guests: data.guests, company_key_for: data.company_key_for, uploads: data.uploads, company_model: data.company_model, company_key: '' }
  const save = async (extra: { company_key: string | null }) => {
    setBusy(true)
    try { await api('/settings/chat', { method: 'PUT', json: { ...v, ...extra } }); setF(null); toast('Saved'); reload() } catch (e: any) { toast(e.message, true) } finally { setBusy(false) }
  }
  return <div className="card" style={{ marginTop: 16 }}>
    <div className="hd"><div><h2>Chat</h2><div className="muted small">People chat on the Chat page with their own Claude API key (billed to their own Claude Console account). Their questions and the passages Claude reads are sent to Anthropic.</div></div>
      <Badge kind={data.guests || (data.company_key_for === 'everyone' && data.company_key_set) ? 'ok' : ''} dot>{data.guests ? 'open to guests' : data.company_key_for === 'everyone' && data.company_key_set ? 'company key on' : 'own keys only'}</Badge></div>
    <div className="bd stack small">
      <div className="between"><span><b>People without their own key</b><div className="muted">Let them use a company Claude key (your organisation pays).</div></span>
        <div className="seg" role="group" aria-label="Company key">{[['nobody', 'Must add their own'], ['everyone', 'May use the company key']].map(([k, l]) =>
          <button key={k} type="button" disabled={!owner} className={v.company_key_for === k ? 'on' : ''} onClick={() => setF({ ...v, company_key_for: k })}>{l}</button>)}</div></div>
      <div className="between"><span><b>Chat without signing in</b><div className="muted">Anyone who opens Context Layer on this computer can chat right away, as a guest: public documents only, on the company key. Not available over the public link.</div></span>
        <div className="seg" role="group" aria-label="Guests">{[[false, 'Off'], [true, 'On']].map(([k, l]) =>
          <button key={String(k)} type="button" disabled={!owner} className={v.guests === k ? 'on' : ''} onClick={() => setF({ ...v, guests: k as boolean })}>{l as string}</button>)}</div></div>
      {(v.company_key_for === 'everyone' || v.guests) && <div className="row">
        <label className="f" style={{ flex: '2 1 260px' }}>Company Claude API key{data.company_key_set && <span className="muted"> (saved: {data.company_key}; leave empty to keep)</span>}
          <input type="password" autoComplete="off" disabled={!owner} value={v.company_key} placeholder={data.company_key_set ? 'leave empty to keep the saved key' : 'sk-ant-…'} onChange={(e) => setF({ ...v, company_key: e.target.value })} /></label>
        <label className="f" style={{ flex: '1 1 200px' }}>Model for the company key<select disabled={!owner} value={v.company_model} onChange={(e) => setF({ ...v, company_model: e.target.value })}>
          {data.models.map(([m, l]) => <option key={m} value={m}>{l}</option>)}{!data.models.some(([m]) => m === v.company_model) && <option value={v.company_model}>{v.company_model}</option>}</select></label></div>}
      <div className="between"><span><b>Adding files from the chat</b><div className="muted">Files are shared with the person's own access groups.</div></span>
        <div className="seg" role="group" aria-label="Chat uploads">{[['everyone', 'Everyone'], ['admins', 'Owners and admins']].map(([k, l]) =>
          <button key={k} type="button" disabled={!owner} className={v.uploads === k ? 'on' : ''} onClick={() => setF({ ...v, uploads: k })}>{l}</button>)}</div></div>
      {owner && <div className="row"><button className="btn" disabled={busy || f === null} onClick={() => save({ company_key: v.company_key ? v.company_key : null })}>Save</button>
        {data.company_key_set && <button className="btn ghost" disabled={busy} onClick={() => save({ company_key: '' })}>Remove company key</button>}</div>}
    </div></div>
}

function Demo({ owner }: { owner: boolean }) {
  const { data } = useLoad<{ available: boolean; company?: string; tables?: string[]; documents?: string[]; people?: number }>('/demo')
  const [busy, setBusy] = useState(false); const toast = useToast()
  if (!data || !data.available) return null
  const go = async () => {
    if (!await askConfirm('Replace the company data tables, documents and definitions with the Kestrel Labs demo company? Personal layers stay. The current company layer is saved in a backup first, and every definition change is recorded in the history.', 'Load Kestrel Labs')) return
    setBusy(true)
    try { await api('/demo/load', { method: 'POST' }); toast('Loading Kestrel Labs: follow it on the Activity page') } catch (e: any) { toast(e.message, true) } finally { setBusy(false) }
  }
  return <div className="card" style={{ marginTop: 16 }}>
    <div className="hd"><div><h2>Demo company</h2><div className="muted small">Kestrel Labs, a made-up software company: {data.people} people, {data.tables?.length} data tables (teams, deployments, incidents, sprints, pipelines, cloud costs, customers, support) and {data.documents?.length} documents that match the data.</div></div></div>
    <div className="bd row">{owner ? <button className="btn" disabled={busy} onClick={go}>Load Kestrel Labs as the company layer</button> : <span className="muted small">Only the owner can load it.</span>}</div></div>
}

export default function Settings({ s }: { s: Session }) {
  const { data, error, reload } = useLoad<S>('/settings')
  const [org, setOrg] = useState<string | null>(null)
  const [pw, setPw] = useState({ current: '', new: '' }); const toast = useToast(); const owner = can(s.user, 'owner')
  if (error) return <div className="page"><ErrorBox error={error} retry={reload} /></div>
  if (!data) return <div className="page"><Loading /></div>
  const setMode = async (mode: string) => {
    try { await api('/settings/graph', { method: 'PUT', json: { mode } }); reload(); toast('Saved: rebuild the graph to apply') } catch (e: any) { toast(e.message, true) }
  }
  return <div className="page">
    <PageHead title="Settings" sub={`Context Layer ${data.version}`} />
    <div className="card"><div className="hd"><div><h2>How agents connect</h2><div className="muted small">{data.ai_model.note}</div></div>
      <Badge kind="ok" dot>{data.graph_mode === 'ai' ? 'agents: AI used for graph builds only' : 'agents: no AI model needed'}</Badge></div>
      <div className="bd stack small">
        <div className="between"><span className="muted">MCP link (this computer)</span><code>{data.mcp.url}</code></div>
        <div className="between"><span className="muted">Public MCP link</span>{data.mcp.public_url ? <code>{data.mcp.public_url}</code>
          : <span className="muted">none: start a Cloudflare Tunnel to publish /mcp (the admin app stays private)</span>}</div>
        <div className="between"><span className="muted">Transport</span><span>{data.mcp.transport}</span></div>
        <div className="between"><span className="muted">Sign-in</span><code>{data.mcp.auth}</code></div>
        <div className="muted">Create a key per agent under <a href="/agents">Agents</a>; each key decides which documents and tools the agent can use.</div>
        <div className="sep" /><b>Models (all run on this server)</b>
        {Object.entries(data.models).map(([k, v]) => <div key={k} className="between"><span className="muted">{k}</span><code>{v}</code></div>)}
        <div className="sep" /><div className="between" style={{ alignItems: 'center' }}><span><b>Knowledge graph quality</b><div className="muted">{MODES[data.graph_mode]}</div></span>
          <div className="seg" role="group" aria-label="Graph mode">{['fast', 'accurate', 'ai'].map((m) => <button key={m} type="button" disabled={!owner || (m === 'ai' && !data.ai_extraction.ready)} title={m === 'ai' && !data.ai_extraction.ready ? 'Set up AI extraction below first' : MODES[m]} className={data.graph_mode === m ? 'on' : ''} onClick={() => setMode(m)}>{m}</button>)}</div></div>
      </div></div>
    <ChatSettings owner={owner} />
    <AIExtraction ai={data.ai_extraction} owner={owner} reload={reload} />
    <div className="grid g2" style={{ marginTop: 16 }}>
      <div className="card"><div className="hd"><h2>Organisation</h2></div><div className="bd stack">
        <label className="f">Name<input type="text" value={org ?? data.organisation} onChange={(e) => setOrg(e.target.value)} disabled={!owner} /></label>
        {owner && <div><button className="btn" disabled={org === null} onClick={async () => { try { await api('/settings/organisation', { method: 'PUT', json: { organisation: org } }); toast('Saved'); setOrg(null); reload() } catch (e: any) { toast(e.message, true) } }}>Save</button></div>}
      </div></div>
      <div className="card"><div className="hd"><h2>Storage</h2></div><div className="bd stack small">
        <div className="between"><span className="muted">Data folder</span><code>{data.storage.data_folder}</code></div>
        <div className="between"><span className="muted">Graph database</span>{data.storage.graph_database_ok ? <Badge kind="ok" dot>online</Badge> : <Badge kind="warn" dot>offline</Badge>}</div>
        {!data.storage.graph_database_ok && <div className="tiny muted">{data.storage.graph_database_message}</div>}
        <div className="between"><span className="muted">Free disk</span><span>{data.storage.disk.free_gb} GB of {data.storage.disk.total_gb} GB</span></div>
      </div></div>
    </div>
    <Demo owner={owner} />
    <div className="card" style={{ marginTop: 16 }}><div className="hd"><h2>Your password</h2></div><div className="bd row" style={{ alignItems: 'flex-end' }}>
      <label className="f" style={{ flex: '1 1 220px' }}>Current<input type="password" value={pw.current} onChange={(e) => setPw({ ...pw, current: e.target.value })} autoComplete="current-password" /></label>
      <label className="f" style={{ flex: '1 1 220px' }}>New (10+ characters)<input type="password" value={pw.new} onChange={(e) => setPw({ ...pw, new: e.target.value })} autoComplete="new-password" /></label>
      <button className="btn" disabled={!pw.current || pw.new.length < 10} onClick={async () => { try { await api('/me/password', { json: pw }); setPw({ current: '', new: '' }); toast('Password changed') } catch (e: any) { toast(e.message, true) } }}>Change password</button>
    </div></div>
  </div>
}
