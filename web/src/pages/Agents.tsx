import { useState } from 'react'
import { Link } from 'react-router-dom'
import { ago, api, can, MCP_URL, type Agent } from '../api'
import type { Session } from '../App'
import { Badge, CAP_LABEL, Copy, CopyBlock, Empty, ErrorBox, GroupChips, Icon, Loading, Modal, PageHead, useLoad, useToast } from '../ui'

interface Resp { agents: Agent[]; groups: string[]; capabilities: string[]; tools: { name: string; description: string }[] }
const CAP_HELP: Record<string, string> = {
  documents: 'Search and quote documents in its groups', data: 'Compute business numbers from reviewed metrics', graph: 'Answer questions that connect facts (uses documents in its groups)',
}

function snippets(token: string) {
  return [
    { id: 'claude-code', label: 'Claude Code', code: `claude mcp add --transport http context-layer ${MCP_URL} --header "Authorization: Bearer ${token}"`,
      hint: <>Run in a terminal, then type <code>/mcp</code> in Claude Code to check the connection.</> },
    { id: 'claude-desktop', label: 'Claude Desktop', code: JSON.stringify({ mcpServers: { 'context-layer': { command: 'npx', args: ['-y', 'mcp-remote', MCP_URL, '--header', 'Authorization:${AUTH_HEADER}'], env: { AUTH_HEADER: `Bearer ${token}` } } } }, null, 2),
      hint: <>Settings → Developer → Edit Config, merge this into <code>claude_desktop_config.json</code>, restart Claude. Needs Node.js 18+.</> },
    { id: 'cursor', label: 'Cursor', code: JSON.stringify({ mcpServers: { 'context-layer': { url: MCP_URL, headers: { Authorization: `Bearer ${token}` } } } }, null, 2),
      hint: <>Save as <code>.cursor/mcp.json</code> in a project (or <code>~/.cursor/mcp.json</code> for all), then check Settings → MCP.</> },
    { id: 'vscode', label: 'VS Code', code: JSON.stringify({ servers: { 'context-layer': { type: 'http', url: MCP_URL, headers: { Authorization: `Bearer ${token}` } } } }, null, 2),
      hint: <>Save as <code>.vscode/mcp.json</code>, start the server from the MCP view, use it in Copilot agent mode.</> },
    { id: 'python', label: 'Python', code: `# pip install mcp
import asyncio, httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

async def main():
    async with httpx2.AsyncClient(headers={"Authorization": "Bearer ${token}"}, timeout=180) as http:
        async with streamable_http_client("${MCP_URL}", http_client=http) as (read, write, *_):
            async with ClientSession(read, write) as s:
                await s.initialize()
                r = await s.call_tool("ask", {"question": "What does our leave policy say about carry-over?"})
                print(r.structured_content)

asyncio.run(main())`, hint: <>Any MCP client works: Streamable HTTP at this address, key as a Bearer token.</> },
  ]
}

export default function Agents({ s }: { s: Session }) {
  const { data, error, reload } = useLoad<Resp>('/agents')
  const [wizard, setWizard] = useState(false)
  const [edit, setEdit] = useState<Agent | null>(null)
  const [revoke, setRevoke] = useState<Agent | null>(null)
  const [showRevoked, setShowRevoked] = useState(false)
  const toast = useToast(); const admin = can(s.user, 'owner', 'admin')
  if (error) return <div className="page"><ErrorBox error={error} retry={reload} /></div>
  if (!data) return <div className="page"><Loading /></div>
  const active = data.agents.filter((a) => !a.revoked), revoked = data.agents.filter((a) => a.revoked)
  return <div className="page">
    <PageHead title="Agents" sub="Each AI agent or app gets its own key. The key decides what it may read and use; every call is logged under its name.">
      {admin && <button className="btn primary" onClick={() => setWizard(true)}><Icon name="plus" size={16} />Connect an agent</button>}
    </PageHead>
    <div className="card" style={{ marginBottom: 16 }}><div className="bd between"><div><b>Connection address for all agents</b><div className="muted small">Model Context Protocol (Streamable HTTP). Each agent adds its own key.</div></div>
      <div style={{ minWidth: 0, flex: '1 1 300px', maxWidth: 480 }}><Copy value={MCP_URL} /></div></div></div>
    <div className="card">{active.length ?
      <div className="tablewrap"><table className="t"><thead><tr><th>Agent</th><th>Documents in groups</th><th>May use</th><th>Calls</th><th>Last used</th><th>Key</th>{admin && <th />}</tr></thead>
        <tbody>{active.map((a) => <tr key={a.id}><td><b>{a.name}</b>{a.description && <div className="tiny muted">{a.description}</div>}<div className="tiny muted">added {ago(a.created)} by {a.created_by}</div></td>
          <td>{a.groups.length ? <GroupChips groups={a.groups} /> : <span className="muted">—</span>}</td>
          <td><span className="row" style={{ gap: 4 }}>{a.capabilities.map((c) => <Badge key={c} kind="brand">{CAP_LABEL[c]}</Badge>)}</span></td>
          <td>{a.calls.toLocaleString()}</td><td className="small muted">{ago(a.last_used)}</td><td className="mono muted">{a.prefix}…</td>
          {admin && <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}><Link className="btn sm" to={`/activity?actor=${encodeURIComponent(a.name)}`}>Activity</Link> <button className="btn sm" onClick={() => setEdit(a)}>Access</button> <button className="btn sm danger" onClick={() => setRevoke(a)}>Revoke</button></td>}
        </tr>)}</tbody></table></div>
      : <Empty icon="agent" title="No agents connected" action={admin && <button className="btn primary" onClick={() => setWizard(true)}><Icon name="plus" size={16} />Connect your first agent</button>}>
        Give Claude, ChatGPT, Cursor, Copilot or your own bot one governed link to your company knowledge.</Empty>}</div>
    {revoked.length > 0 && <p className="small"><a href="#" onClick={(e) => { e.preventDefault(); setShowRevoked(!showRevoked) }}>{showRevoked ? 'Hide' : 'Show'} {revoked.length} revoked</a></p>}
    {showRevoked && <div className="card"><table className="t"><tbody>{revoked.map((a) => <tr key={a.id} className="dim"><td><b>{a.name}</b></td><td>revoked {ago(a.revoked)}</td><td>{a.calls} calls</td></tr>)}</tbody></table></div>}
    <div className="card" style={{ marginTop: 16 }}><div className="hd"><h2>What agents can do</h2><span className="muted small">All tools are read-only</span></div>
      <table className="t"><tbody>{data.tools.map((t) => <tr key={t.name}><td className="mono" style={{ whiteSpace: 'nowrap' }}>{t.name}</td><td className="small">{t.description}</td></tr>)}</tbody></table></div>
    {wizard && <Wizard groups={data.groups} onClose={() => { setWizard(false); reload() }} />}
    {edit && <AccessDialog agent={edit} groups={data.groups} onClose={() => setEdit(null)} onDone={() => { setEdit(null); reload(); toast('Access updated. It applies from the agent’s next call.') }} />}
    {revoke && <Modal title={`Revoke ${revoke.name}?`} onClose={() => setRevoke(null)} footer={<><button className="btn" onClick={() => setRevoke(null)}>Cancel</button>
      <button className="btn primary" style={{ background: 'var(--bad)', borderColor: 'var(--bad)' }} onClick={async () => {
        try { await api(`/agents/${revoke.id}/revoke`, { method: 'POST' }); setRevoke(null); reload(); toast('Key revoked') } catch (e: any) { toast(e.message, true) } }}>Revoke key</button></>}>
      <p>The agent is cut off at its next request. This can't be undone: to reconnect it, create a new key.</p></Modal>}
  </div>
}

function AccessFields({ groups, g, setG, caps, setCaps }: { groups: string[]; g: string[]; setG: (v: string[]) => void; caps: string[]; setCaps: (v: string[]) => void }) {
  return <div className="stack">
    <div><b className="small">May use</b><div className="checks" style={{ marginTop: 8 }}>{['documents', 'data', 'graph'].map((c) =>
      <label key={c} className="check" style={{ alignItems: 'flex-start' }}><input type="checkbox" checked={caps.includes(c)} onChange={(e) => setCaps(e.target.checked ? [...caps, c] : caps.filter((x) => x !== c))} style={{ marginTop: 3 }} />
        <span>{CAP_LABEL[c]}<div className="d">{CAP_HELP[c]}</div></span></label>)}</div></div>
    {(caps.includes('documents') || caps.includes('graph')) && <div><b className="small">Documents in these groups</b><div className="tiny muted" style={{ margin: '2px 0 8px' }}>Only files tagged with one of these groups are visible to this agent.</div>
      <div className="checks">{groups.map((x) => <label key={x} className="check"><input type="checkbox" checked={g.includes(x)} onChange={(e) => setG(e.target.checked ? [...g, x] : g.filter((y) => y !== x))} />{x}</label>)}</div></div>}
  </div>
}

function Wizard({ groups, onClose }: { groups: string[]; onClose: () => void }) {
  const [step, setStep] = useState(1); const [name, setName] = useState(''); const [desc, setDesc] = useState('')
  const [g, setG] = useState<string[]>(['public']); const [caps, setCaps] = useState<string[]>(['documents'])
  const [created, setCreated] = useState<{ agent: Agent; token: string } | null>(null); const [client, setClient] = useState('claude-code')
  const [busy, setBusy] = useState(false); const toast = useToast()
  const create = async () => {
    setBusy(true)
    try { const r = await api<{ agent: Agent; token: string }>('/agents', { json: { name, description: desc, groups: g, capabilities: caps } }); setCreated(r); setStep(3) }
    catch (e: any) { toast(e.message, true) } finally { setBusy(false) }
  }
  const sn = created ? snippets(created.token) : []; const cur = sn.find((x) => x.id === client)
  return <Modal title={step === 3 ? `Connect ${created?.agent.name}` : 'Connect an agent'} onClose={onClose}
    footer={step === 1 ? <><button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" disabled={!name.trim()} onClick={() => setStep(2)}>Next</button></>
      : step === 2 ? <><button className="btn" onClick={() => setStep(1)}>Back</button><button className="btn primary" disabled={busy || !caps.length || ((caps.includes('documents') || caps.includes('graph')) && !g.length)} onClick={create}>Create key</button></>
        : <button className="btn primary" onClick={onClose}>Done</button>}>
    <div className="row tiny muted" style={{ marginBottom: 14 }}>{['Name', 'Access', 'Connect'].map((l, i) => <span key={l} className={`badge ${step === i + 1 ? 'brand' : ''}`}>{i + 1}. {l}</span>)}</div>
    {step === 1 && <div className="stack">
      <label className="f">Agent name<span className="h">The app, bot or person using it. Shown in activity and audit.</span><input type="text" autoFocus value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Support bot, Claude Desktop – Finance team" maxLength={60} /></label>
      <label className="f">What it's for (optional)<input type="text" value={desc} onChange={(e) => setDesc(e.target.value)} placeholder="Answers customer questions from product docs" maxLength={300} /></label></div>}
    {step === 2 && <AccessFields groups={groups} g={g} setG={setG} caps={caps} setCaps={setCaps} />}
    {step === 3 && created && <div className="stack">
      <div className="callout warn">Copy the key now. It is shown only once; if it is lost, revoke it and create a new one.</div>
      <div><b className="small">Address</b><div style={{ marginTop: 6 }}><Copy value={MCP_URL} /></div></div>
      <div><b className="small">Key</b><div style={{ marginTop: 6 }}><Copy value={created.token} secret /></div></div>
      <div className="sep" /><b className="small">Set it up in</b>
      <div className="seg">{sn.map((x) => <button key={x.id} className={client === x.id ? 'on' : ''} onClick={() => setClient(x.id)}>{x.label}</button>)}</div>
      {cur && <><CopyBlock value={cur.code} /><div className="small muted">{cur.hint}</div></>}
      <div className="small">Test it first in the <Link to="/playground" onClick={onClose}>Playground</Link>, asking as {created.agent.name}.</div></div>}
  </Modal>
}

function AccessDialog({ agent, groups, onClose, onDone }: { agent: Agent; groups: string[]; onClose: () => void; onDone: () => void }) {
  const [g, setG] = useState(agent.groups); const [caps, setCaps] = useState(agent.capabilities); const toast = useToast()
  return <Modal title={`Access: ${agent.name}`} onClose={onClose} footer={<><button className="btn" onClick={onClose}>Cancel</button>
    <button className="btn primary" disabled={!caps.length} onClick={async () => { try { await api(`/agents/${agent.id}`, { method: 'PATCH', json: { groups: g, capabilities: caps } }); onDone() } catch (e: any) { toast(e.message, true) } }}>Save</button></>}>
    <AccessFields groups={Array.from(new Set([...groups, ...agent.groups])).sort()} g={g} setG={setG} caps={caps} setCaps={setCaps} /></Modal>
}
