/* Context Layer console: connect agents over MCP, manage data, keys and activity. */
'use strict';

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const when = (iso) => (iso ? new Date(iso).toLocaleString() : '—');
const MCP_URL = `${location.origin}/mcp`;

const S = { tab: 'connect', state: null, keys: [], tools: [], newKey: null, client: 'claude-code', tryKey: null, tryTool: 'ask', tryArgs: {}, tryResult: null, job: null, confirmRevoke: null };

async function api(path, opts = {}) {
  const o = { ...opts };
  if (o.json !== undefined) { o.method = o.method || 'POST'; o.headers = { 'content-type': 'application/json' }; o.body = JSON.stringify(o.json); delete o.json; }
  const r = await fetch(path, o);
  let data = null;
  try { data = await r.json(); } catch (_) { /* no body */ }
  if (!r.ok) throw new Error((data && data.detail) || `Request failed (${r.status})`);
  return data;
}

let toastTimer;
function toast(msg, bad = false) {
  const t = $('#toast');
  t.textContent = msg; t.className = 'toast' + (bad ? ' bad' : ''); t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, bad ? 7000 : 2500);
}

async function copy(text, what) {
  try { await navigator.clipboard.writeText(text); toast(`${what} copied`); } catch (_) { toast('Copy failed: select the text and copy it', true); }
}

async function load() {
  const [state, keys, tools] = await Promise.all([api('/api/state'), api('/api/console/keys'), api('/api/console/tools')]);
  S.state = state; S.keys = keys.keys; S.tools = tools.tools;
}

/* ------------------------------------------------------------------ status */

function ready() {
  const o = S.state.outputs;
  return { docs: S.state.documents.length, index: !!(o.index && o.index.exists), graph: !!(o.graph && o.graph.exists) };
}

function renderStatus() {
  const r = ready();
  const pill = (ok, okText, noText) => `<span class="pill ${ok ? 'ok' : 'warn'}"><span class="dot"></span>${ok ? okText : noText}</span>`;
  $('#status').innerHTML = pill(r.index, `${r.docs} document${r.docs === 1 ? '' : 's'} searchable`, 'Data not prepared')
    + pill(r.graph, 'Knowledge graph built', 'No knowledge graph')
    + pill(S.state.can_answer, S.state.has_api_key ? 'Answers: Claude' : `Answers: ${esc(S.state.answer_model || '')}`, 'No LLM: agents get passages');
}

/* ------------------------------------------------------------------ connect */

function snippets(url, token) {
  const tok = token || '<your key>';
  return {
    'claude-code': {
      label: 'Claude Code',
      code: `claude mcp add --transport http context-layer ${url} --header "Authorization: Bearer ${tok}"`,
      hint: 'Run this in a terminal. Then start <code>claude</code> and type <code>/mcp</code> to check that context-layer is connected.',
    },
    'claude-desktop': {
      label: 'Claude Desktop',
      code: JSON.stringify({ mcpServers: { 'context-layer': { command: 'npx', args: ['-y', 'mcp-remote', url, '--header', 'Authorization:${AUTH_HEADER}'], env: { AUTH_HEADER: `Bearer ${tok}` } } } }, null, 2),
      hint: 'In Claude Desktop open Settings → Developer → Edit Config, add this to <code>claude_desktop_config.json</code> (merge it into any existing <code>mcpServers</code>), then fully quit and reopen Claude. Needs Node.js 18+ for <code>npx</code>. The header is written without a space after the colon on purpose: Claude Desktop on Windows breaks arguments that contain spaces.',
    },
    cursor: {
      label: 'Cursor',
      code: JSON.stringify({ mcpServers: { 'context-layer': { url, headers: { Authorization: `Bearer ${tok}` } } } }, null, 2),
      hint: 'Put this in <code>.cursor/mcp.json</code> in your project (or <code>~/.cursor/mcp.json</code> for all projects), then check Cursor Settings → MCP.',
    },
    vscode: {
      label: 'VS Code',
      code: JSON.stringify({ servers: { 'context-layer': { type: 'http', url, headers: { Authorization: `Bearer ${tok}` } } } }, null, 2),
      hint: 'Put this in <code>.vscode/mcp.json</code> in your workspace, then start the server from the MCP view and use it in Copilot Chat agent mode.',
    },
    python: {
      label: 'Your own agent (Python)',
      code: `# pip install mcp
import asyncio, httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

async def main():
    headers = {"Authorization": "Bearer ${tok}"}
    async with httpx2.AsyncClient(headers=headers, timeout=120) as http:
        async with streamable_http_client("${url}", http_client=http) as (read, write, *_):
            async with ClientSession(read, write) as s:
                await s.initialize()
                print([t.name for t in (await s.list_tools()).tools])
                r = await s.call_tool("ask", {"question": "What are the main themes across the documents?"})
                print(r.structured_content)

asyncio.run(main())`,
      hint: 'Any MCP client works the same way: Streamable HTTP at this URL, with the key as a Bearer token.',
    },
  };
}

function viewConnect() {
  const r = ready();
  const groups = S.state.groups;
  const k = S.newKey;
  const sn = snippets(MCP_URL, k && k.token);
  const cur = sn[S.client];
  return `<h2>Connect an agent</h2>
  <p class="lead">Each agent or app gets its own key. The key decides which documents the agent may read, using the same access groups the documents are tagged with. The agent then connects to one MCP link and can search, ask questions and explore the knowledge graph, always with sources.</p>
  <div class="steps3">
    <div class="s ${r.index ? 'done' : ''}"><b>1 · Prepare your data</b><span>${r.index ? `${r.docs} document(s) ready${r.graph ? ', knowledge graph built' : ', graph not built yet'}` : 'Add documents and click Prepare data'}</span></div>
    <div class="s ${k || S.keys.some((x) => !x.revoked) ? 'done' : ''}"><b>2 · Create a key</b><span>One per agent, with the groups it may read</span></div>
    <div class="s"><b>3 · Paste into your agent</b><span>Claude, Cursor, VS Code or your own code</span></div>
  </div>
  ${r.index ? '' : '<div class="panel" style="border-color:var(--warn)"><p class="small" style="margin:0">No data is prepared yet, so agents would find nothing. Go to <button class="btn sm" data-tab="data">Data</button> first.</p></div>'}
  <div class="panel"><h3>New key</h3>
    <div class="grid2" style="margin-top:10px">
      <div class="field"><label class="lbl" for="kname">Who will use it</label><input type="text" id="kname" placeholder="e.g. Claude Desktop – Shishir, Support bot" maxlength="60"></div>
      <div class="field"><span class="lbl">May read documents in these groups</span><div class="row" id="kgroups">${groups.map((g) => `<label class="check"><input type="checkbox" value="${esc(g)}" ${g === 'public' ? 'checked' : ''}>${esc(g)}</label>`).join('')}</div></div>
    </div>
    <button class="btn primary" id="createkey">Create key</button>
  </div>
  ${k ? `<div class="panel" style="border-color:var(--accent)"><h3>Key for “${esc(k.key.name)}” <span class="chip">${k.key.groups.map(esc).join(', ')}</span></h3>
    <div class="field" style="margin-top:10px"><span class="lbl">MCP link</span><div class="copyrow"><code>${esc(MCP_URL)}</code><button class="btn sm" data-copy="${esc(MCP_URL)}" data-what="Link">Copy</button></div></div>
    <div class="field"><span class="lbl">Key</span><div class="copyrow"><code class="secret">${esc(k.token)}</code><button class="btn sm" data-copy="${esc(k.token)}" data-what="Key">Copy</button></div>
      <p class="once">Shown only once. Store it now; if it is lost, revoke it and create a new one.</p></div>
  </div>` : ''}
  <div class="panel"><h3>Set up your agent</h3>
    <p class="small muted" style="margin:4px 0 0">${k ? 'Your new key is already filled in below.' : 'Create a key above and it will be filled in below. The link is the same for every key.'}</p>
    <div class="clients">${Object.entries(sn).map(([id, x]) => `<button data-client="${id}" class="${S.client === id ? 'on' : ''}">${esc(x.label)}</button>`).join('')}</div>
    <div class="snip-wrap"><pre class="snippet">${esc(cur.code)}</pre><button class="btn sm" data-copy="${esc(cur.code)}" data-what="Setup">Copy</button></div>
    <p class="hint">${cur.hint}</p>
    <p class="hint">Tools the agent gets: ${S.tools.map((t) => `<code>${esc(t.name)}</code>`).join(' · ')}. All are read-only. This link works on this computer only (<code>${esc(location.host)}</code>); hosting it for other machines, phones and apps like ChatGPT needs HTTPS and sign-in, which is the next phase.</p>
  </div>`;
}

/* ------------------------------------------------------------------ data */

function viewData() {
  const r = ready();
  const busy = S.job && S.job.status === 'running';
  const docs = S.state.documents;
  return `<h2>Data</h2>
  <p class="lead">Documents in the <code>documents/</code> folder. Each file has access groups; an agent only sees files whose groups overlap its key's groups. After adding files or changing groups, prepare the data again.</p>
  <div class="panel"><div class="head"><h3>Documents</h3>
    <button class="btn primary" data-run="prepare" ${busy ? 'disabled' : ''}>${r.index ? 'Prepare data again' : 'Prepare data'}</button></div>
    ${docs.length ? `<div class="scroll"><table class="t"><thead><tr><th>File</th><th>Size</th><th>Access groups</th></tr></thead><tbody>
      ${docs.map((d) => `<tr><td>${esc(d.name)}</td><td class="muted">${(d.size / 1024).toFixed(1)} KB</td><td>${d.access.map((g) => `<span class="chip ${g === 'public' ? '' : 'warn'}">${esc(g)}</span>`).join(' ')}</td></tr>`).join('')}
    </tbody></table></div>` : '<p class="muted">No documents yet.</p>'}
    <p class="hint">Prepare data reads every file, splits it into passages, builds the search index and the knowledge graph. The first run downloads the models once and takes several minutes. Change a file's groups in the <a href="/">pipeline explorer</a> (step 0).</p>
  </div>
  <div class="panel"><h3>Add documents</h3>
    <div class="uploader" style="margin-top:8px"><input type="file" id="files" multiple aria-label="Choose files">
      <input type="text" id="upgroups" value="public" aria-label="Groups for the new files" style="width:180px" title="Comma-separated access groups">
      <button class="btn primary" id="upload">Upload</button></div>
    <p class="hint">PDF, Word, PowerPoint, Excel, HTML, Markdown or text. Groups are comma separated, for example <code>hr</code> or <code>sales, public</code>.</p>
  </div>`;
}

/* ------------------------------------------------------------------ keys */

function viewKeys() {
  const rows = S.keys.slice().reverse().map((k) => `<tr class="${k.revoked ? 'revoked' : ''}"><td><b>${esc(k.name)}</b></td>
    <td>${k.groups.map((g) => `<span class="chip ${g === 'public' ? '' : 'warn'}">${esc(g)}</span>`).join(' ')}</td>
    <td class="mono">${esc(k.prefix)}…</td><td class="small muted">${when(k.created)}</td><td class="small muted">${when(k.last_used)}</td><td>${k.calls || 0}</td>
    <td>${k.revoked ? '<span class="chip">revoked</span>' : S.confirmRevoke === k.id ? `<button class="btn sm" data-revoke-yes="${esc(k.id)}" style="border-color:var(--bad);color:var(--bad)">Confirm revoke</button>` : `<button class="btn sm" data-revoke="${esc(k.id)}">Revoke</button>`}</td></tr>`).join('');
  return `<h2>Keys</h2>
  <p class="lead">One key per agent or app. Revoking a key cuts that agent off at its next request; the others keep working. Keys are stored only as hashes in <code>console_data/keys.json</code>.</p>
  <div class="panel">${rows ? `<div class="scroll"><table class="t"><thead><tr><th>Name</th><th>May read</th><th>Key</th><th>Created</th><th>Last used</th><th>Calls</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>` : '<p class="muted">No keys yet. Create one under <button class="btn sm" data-tab="connect">Connect an agent</button>.</p>'}</div>`;
}

/* ------------------------------------------------------------------ try */

const EXAMPLES = {
  ask: { question: 'What are the main themes across the documents?', mode: 'auto' },
  search: { query: 'how are answers checked', top_k: 5 },
  find_entity: { name: 'Qdrant' },
  fetch_passage: { passage_id: '<an id from search or ask>' },
  list_documents: {},
};

function viewTry() {
  const active = S.keys.filter((k) => !k.revoked);
  if (!active.length) return `<h2>Try it</h2><div class="panel empty"><h3>No active key</h3><p>Create a key first; Try it runs tools exactly as an agent with that key would.</p><button class="btn primary" data-tab="connect">Create a key</button></div>`;
  if (!active.some((k) => k.id === S.tryKey)) S.tryKey = active[active.length - 1].id;
  const tool = S.tools.find((t) => t.name === S.tryTool) || S.tools[0];
  const args = S.tryArgs[tool.name] ?? JSON.stringify(EXAMPLES[tool.name] || {}, null, 2);
  return `<h2>Try it</h2>
  <p class="lead">Run any tool as a specific key and see exactly what the agent would receive, including what that key is not allowed to see.</p>
  <div class="grid2">
    <div class="panel" style="margin:0"><div class="field"><label class="lbl" for="trykey">As key</label>
      <select id="trykey">${active.map((k) => `<option value="${esc(k.id)}" ${k.id === S.tryKey ? 'selected' : ''}>${esc(k.name)} (${k.groups.map(esc).join(', ')})</option>`).join('')}</select></div>
      <div class="field"><label class="lbl" for="trytool">Tool</label>
      <select id="trytool">${S.tools.map((t) => `<option ${t.name === tool.name ? 'selected' : ''}>${esc(t.name)}</option>`).join('')}</select></div>
      <p class="tool-desc">${esc(tool.description)}</p>
      <div class="field"><label class="lbl" for="tryargs">Arguments (JSON)</label><textarea id="tryargs" spellcheck="false">${esc(args)}</textarea></div>
      <button class="btn primary" id="tryrun" ${S.job && S.job.status === 'running' ? 'disabled' : ''}>Run</button></div>
    <div class="panel" style="margin:0"><span class="lbl">What the agent receives</span>
      ${S.tryResult ? `<pre class="result" style="margin-top:8px">${esc(S.tryResult)}</pre>` : '<p class="muted small">Run a tool to see its result here.</p>'}</div>
  </div>`;
}

/* ------------------------------------------------------------------ activity */

async function viewActivity() {
  const { calls } = await api('/api/console/activity?limit=100');
  const rows = calls.map((c) => `<tr><td class="small muted" style="white-space:nowrap">${when(c.time)}</td><td>${esc(c.key)}</td><td class="mono">${esc(c.tool)}</td>
    <td class="mono small">${esc(JSON.stringify(c.args)).slice(0, 140)}</td><td>${c.ok ? '<span class="chip ok">ok</span>' : '<span class="chip bad">error</span>'}</td>
    <td class="small">${esc(c.summary)}</td><td class="small muted">${c.seconds}s</td></tr>`).join('');
  return `<h2>Activity</h2>
  <p class="lead">Every tool call, from agents and from Try it, newest first. Kept in <code>console_data/mcp_calls.jsonl</code>.</p>
  <div class="panel"><div class="head"><h3>Recent calls</h3><button class="btn sm" id="refresh">Refresh</button></div>
  ${rows ? `<div class="scroll"><table class="t"><thead><tr><th>Time</th><th>Key</th><th>Tool</th><th>Arguments</th><th></th><th>Result</th><th>Time</th></tr></thead><tbody>${rows}</tbody></table></div>` : '<p class="muted">No calls yet.</p>'}</div>`;
}

/* ------------------------------------------------------------------ render + events */

async function render() {
  renderStatus();
  $$('#tabs [data-tab]').forEach((b) => b.classList.toggle('on', b.dataset.tab === S.tab));
  const V = { connect: viewConnect, data: viewData, keys: viewKeys, try: viewTry, activity: viewActivity };
  try { $('#main').innerHTML = await V[S.tab](); } catch (e) { $('#main').innerHTML = `<div class="panel empty"><h3>Could not load</h3><p>${esc(e.message)}</p></div>`; }
  if (S.tab === 'try') {
    $('#trykey') && $('#trykey').addEventListener('change', (e) => { S.tryKey = e.target.value; });
    $('#trytool') && $('#trytool').addEventListener('change', (e) => { S.tryTool = e.target.value; S.tryResult = null; render(); });
  }
}

function renderJob() {
  const box = $('#job');
  const j = S.job;
  if (!j) { box.hidden = true; return; }
  box.hidden = false;
  const icon = j.status === 'done' ? '<span class="chip ok">done</span>' : j.status === 'error' ? '<span class="chip bad">failed</span>' : '<span class="spin"></span>';
  box.innerHTML = `<h4>${icon} ${esc(j.label)}</h4>${j.error ? `<p class="small" style="color:var(--bad);margin:8px 0 0">${esc(j.error)}</p>` : ''}
    ${(j.log || []).length ? `<pre>${esc(j.log.slice(-10).join('\n'))}</pre>` : '<p class="small muted" style="margin:8px 0 0">Starting… the first run downloads the models, which can take several minutes.</p>'}`;
}

async function runJob(start, label) {
  const { job } = await start;
  S.job = { id: job, label, status: 'running', log: [] }; renderJob(); render();
  let j;
  for (;;) {
    await sleep(800);
    j = await api(`/api/jobs/${job}`);
    S.job = { ...j, label }; renderJob();
    if (j.status === 'done' || j.status === 'error') break;
  }
  setTimeout(() => { if (S.job && S.job.id === j.id) { S.job = null; renderJob(); } }, j.status === 'error' ? 15000 : 3000);
  if (j.status === 'error') throw new Error(j.error || 'Failed');
  return j.result;
}

document.addEventListener('click', async (ev) => {
  const tab = ev.target.closest('[data-tab]');
  if (tab) { S.tab = tab.dataset.tab; S.confirmRevoke = null; render(); return; }
  const c = ev.target.closest('[data-copy]');
  if (c) { copy(c.dataset.copy, c.dataset.what || 'Text'); return; }
  const cl = ev.target.closest('[data-client]');
  if (cl) { S.client = cl.dataset.client; render(); return; }
  if (ev.target.closest('#createkey')) {
    const name = $('#kname').value.trim();
    const groups = $$('#kgroups input:checked').map((i) => i.value);
    if (!name) { toast('Give the key a name first', true); $('#kname').focus(); return; }
    if (!groups.length) { toast('Pick at least one group', true); return; }
    try { S.newKey = await api('/api/console/keys', { json: { name, groups } }); await load(); render(); toast('Key created'); } catch (e) { toast(e.message, true); }
    return;
  }
  const rv = ev.target.closest('[data-revoke]');
  if (rv) { S.confirmRevoke = rv.dataset.revoke; render(); return; }
  const rvy = ev.target.closest('[data-revoke-yes]');
  if (rvy) {
    try { await api(`/api/console/keys/${rvy.dataset.revokeYes}/revoke`, { method: 'POST' }); S.confirmRevoke = null; if (S.newKey && S.newKey.key.id === rvy.dataset.revokeYes) S.newKey = null; await load(); render(); toast('Key revoked'); } catch (e) { toast(e.message, true); }
    return;
  }
  const run = ev.target.closest('[data-run]');
  if (run && !run.disabled) {
    try { await runJob(api(`/api/run/${run.dataset.run}`, { method: 'POST' }), 'Preparing data (parse, index, knowledge graph)'); await load(); render(); toast('Data prepared'); } catch (e) { toast(e.message, true); await load().catch(() => {}); render(); }
    return;
  }
  if (ev.target.closest('#upload')) {
    const files = $('#files').files;
    if (!files.length) { toast('Choose one or more files first', true); return; }
    const fd = new FormData();
    [...files].forEach((f) => fd.append('files', f));
    fd.append('groups', $('#upgroups').value || 'public');
    try { const r = await api('/api/upload', { method: 'POST', body: fd }); await load(); render(); toast(`Uploaded ${r.saved.length} file(s). Now click Prepare data.`); } catch (e) { toast(e.message, true); }
    return;
  }
  if (ev.target.closest('#tryrun')) {
    let args;
    S.tryKey = $('#trykey').value; S.tryTool = $('#trytool').value;
    S.tryArgs[S.tryTool] = $('#tryargs').value;
    try { args = JSON.parse($('#tryargs').value || '{}'); } catch (_) { toast('Arguments are not valid JSON', true); return; }
    S.tryResult = 'Running…'; render();
    try {
      const r = await api('/api/console/try', { json: { key_id: S.tryKey, tool: S.tryTool, args } });
      S.tryResult = JSON.stringify(r.result, null, 2);
    } catch (e) { S.tryResult = `Error: ${e.message}`; }
    await load(); render();
    return;
  }
  if (ev.target.closest('#refresh')) { render(); }
});

(async function init() {
  try { await load(); render(); } catch (e) { $('#main').innerHTML = `<div class="panel empty"><h3>Could not reach the server</h3><p>${esc(e.message)}</p></div>`; }
})();
