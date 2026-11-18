'use strict';
/* RAG pipeline explorer: one page, no build step, no external libraries. */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmt = (n, d = 2) => (n === null || n === undefined || Number.isNaN(Number(n))) ? '—' : Number(n).toLocaleString(undefined, { maximumFractionDigits: d });
const secs = (v) => (v === null || v === undefined) ? '—' : (v > 0 && v < 0.01 ? '<0.01 s' : `${fmt(v)} s`);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const PALETTE = ['#4f46e5', '#0f766e', '#c2410c', '#be185d', '#0369a1', '#65a30d', '#7c3aed', '#b45309', '#475569', '#dc2626'];

const S = { state: null, step: 'docs', cache: {}, trace: null, job: null, sel: {}, history: [], form: null };

/* ------------------------------------------------------------------ steps and their explanations */

const STEPS = [
  { id: 'docs', n: 0, phase: 'prepare', title: 'Your documents', short: 'Files, and who may see them' },
  { id: 'parse', n: 1, phase: 'prepare', title: 'Parse', short: 'Files become clean text', file: 'parse' },
  { id: 'chunk', n: 2, phase: 'prepare', title: 'Chunk', short: 'Text is cut into passages', file: 'chunk' },
  { id: 'embed', n: 3, phase: 'prepare', title: 'Embed', short: 'Passages become numbers', file: 'embed' },
  { id: 'index', n: 4, phase: 'prepare', title: 'Index', short: 'Stored for fast search', file: 'index' },
  { id: 'filter', n: 5, phase: 'ask', title: 'Permission filter', short: 'Only what you may see' },
  { id: 'search', n: 6, phase: 'ask', title: 'Hybrid search', short: 'Meaning and keywords' },
  { id: 'fuse', n: 7, phase: 'ask', title: 'Fusion', short: 'Two lists become one' },
  { id: 'rerank', n: 8, phase: 'ask', title: 'Re-rank', short: 'The best few are picked' },
  { id: 'answer', n: 9, phase: 'ask', title: 'Answer', short: 'Claude answers with citations' },
  { id: 'eval', n: 10, phase: 'check', title: 'Evaluate', short: 'Score each layer', file: 'eval' },
];
const PHASES = {
  prepare: { label: 'Prepare the documents', note: 'Runs once, and again when files change' },
  ask: { label: 'Answer a question', note: 'Runs for every question' },
  check: { label: 'Check the quality', note: 'Runs on a test set' },
};

function explain(id) {
  const c = (S.state && S.state.config) || {};
  const E = {
    docs: {
      what: 'Everything starts with the files in the <code>documents/</code> folder. Each file can be given access groups, for example <b>hr</b>. Files without a group are <b>public</b>. The groups travel with every passage cut from the file, so search can filter on them later.',
      why: 'Permissions set here are enforced at search time. A file tagged with the wrong group can be found by the wrong people.',
      watch: ['After adding files or changing groups, run the preparation steps again. The index is rebuilt from scratch each time.', 'Supported: PDF, Word, PowerPoint, Excel, HTML, Markdown and images (read by Docling), plus plain .txt.'],
      settings: [['Folder', 'documents/'], ['Default group', 'public'], ['Groups file', 'documents/_access.json']],
    },
    parse: {
      what: 'Docling opens each file and turns it into clean text while keeping its structure: headings, tables and page numbers. Plain .txt files are read as they are.',
      why: 'Search can only find what parsing extracted. A table read as jumbled text, or a scanned page with no text layer, is invisible to every later step.',
      watch: ['Check that the preview looks like the document: headings in the right places and tables readable.', 'Very little text from a long PDF usually means a scanned file that needs OCR.'],
      settings: [['Parser', 'Docling (layout-aware)'], ['.txt files', 'read as plain text']],
    },
    chunk: {
      what: `Each document is cut into passages of at most <b>${c.chunk_max_tokens} tokens</b>, following its structure so a passage does not straddle two sections. Before embedding, the heading path is put in front of the passage, so a line like "Monthly: 12 days" still says which section it came from.`,
      why: 'Only a handful of passages ever reach the model. Too small, and a passage loses its context. Too large, and it mixes topics, which makes search less precise.',
      watch: ['Click a block to see the passage and the exact text that gets embedded. The heading prefix is highlighted.', 'Blocks close to the limit may be cut mid-thought; very small ones may not carry enough context.'],
      settings: [['Max size', `${c.chunk_max_tokens} tokens`], ['Counted with', `${c.embed_model} tokenizer`], ['Documents', 'Docling HybridChunker'], ['.txt files', 'recursive splitter, 64-token overlap']],
    },
    embed: {
      what: `Every passage becomes two kinds of vector. A <b>meaning vector</b> from ${c.embed_model}: ${c.embed_dim} numbers, where passages about similar things end up close together even when they use different words. And a <b>keyword vector</b> (BM25) that records which exact words appear.`,
      why: 'Meaning search finds paraphrases ("time off" matches "annual leave"). Keyword search finds exact names, codes and numbers. Each covers the other\'s blind spot.',
      watch: [`The map squeezes ${c.embed_dim} dimensions into two, so treat distances as rough. Passages from the same section should still sit near each other.`, 'The colour grid is the first 256 numbers of the selected passage\'s vector. On its own it means nothing to a person; only the distance between vectors matters.'],
      settings: [['Meaning model', c.embed_model], ['Dimensions', c.embed_dim], ['Keyword model', c.sparse_model]],
    },
    index: {
      what: 'Both vectors and the passage text go into a Qdrant collection on disk. Meaning vectors get an <b>HNSW index</b>, a graph of near neighbours, so a search does not have to compare against every passage. Keyword vectors are weighted by <b>IDF</b>, so rare words count for more. Access groups and file names are indexed so filters are fast.',
      why: 'This is the store every question searches. It is rebuilt from scratch each time the preparation steps run.',
      watch: ['The number of points should equal the number of chunks.', 'If a group you expect is missing below, check the file\'s access groups and run the preparation steps again.'],
      settings: [['Collection', c.collection], ['Meaning index', 'HNSW, cosine, m=16'], ['Keyword weighting', 'IDF'], ['Filter indexes', 'access, source']],
    },
    filter: {
      what: 'Before any search runs, the question is limited to passages whose access groups overlap the asker\'s groups, and to the chosen files if any. The filter is applied <b>inside</b> each search, not afterwards.',
      why: 'Filtering inside the search means hidden passages cannot influence the ranking or slip into the answer. Filtering afterwards could leak through scores or leave you with fewer results.',
      watch: ['Ask the same question as <b>public</b> and then with another group ticked, and compare the results.', 'There is no login yet: the groups are whatever is ticked above. This shows the mechanism, not real security.'],
      settings: [['Filter on', 'access groups, file names'], ['Applied', 'inside the query (pre-filter)']],
    },
    search: {
      what: `Two searches run side by side over the allowed passages, each returning up to ${c.candidates_per_retriever} passages. <b>Meaning search</b> compares the question's vector with every passage's vector (cosine similarity). <b>Keyword search</b> scores exact word matches with BM25.`,
      why: 'They fail in different ways. Meaning search can miss an exact code or name; keyword search misses paraphrases. Running both makes a miss less likely.',
      watch: ['Passages found by both searches are highlighted. They usually end up on top.', 'The two lists score on different scales, so the scores cannot be compared directly. That is why the next step uses positions instead.'],
      settings: [['Each search returns', c.candidates_per_retriever], ['Meaning', 'cosine similarity'], ['Keywords', 'BM25']],
    },
    fuse: {
      what: `<b>Reciprocal Rank Fusion</b> merges the two lists using positions only. A passage earns more for ranking high in a list, and earns from both lists if it appears in both. Up to ${c.fused_top_k} move on.`,
      why: 'Positions can be compared even when scores cannot, so there are no score weights to tune.',
      watch: ['Look for passages that ranked well in only one list: fusion decides whether they survive.', 'Only the top 10 of each search are saved, so a dash means "below 10th, or not found".'],
      settings: [['Method', 'Reciprocal Rank Fusion'], ['Kept', c.fused_top_k]],
    },
    rerank: {
      what: `A cross-encoder (${c.rerank_model}) reads the question and each fused passage (up to ${c.fused_top_k}) <b>together</b> and scores how well the passage answers it. The best ${c.final_top_k} go to Claude.`,
      why: 'Search compares vectors computed in advance: fast, but rough. Reading question and passage together is slower and much more precise, so it is only done on a short list.',
      watch: [`Lines show how each passage moved. Big jumps mean search and the re-ranker disagree, which is worth a look.`, `Only the top ${c.final_top_k} (highlighted) reach Claude. Anything below is never seen by the model.`],
      settings: [['Re-ranker', c.rerank_model], ['In', c.fused_top_k], ['Out', c.final_top_k]],
    },
    answer: {
      what: `Claude (${c.claude_model}) gets only the ${c.final_top_k} passages and strict instructions: answer from them alone, cite the passage IDs, and say "not found" instead of guessing. It replies in a fixed structure (found, answer, citations). Any citation that does not match a retrieved passage is dropped.`,
      why: 'This is where made-up answers are controlled: the model can only use, and only cite, what retrieval handed it.',
      watch: ['Check that each cited passage really supports the answer.', '"Not found" is the correct outcome when the documents do not cover the question.', 'In retrieval-only mode this paid call is skipped, and you see the exact prompt Claude would have received.'],
      settings: [['Model', c.claude_model], ['Passages given', c.final_top_k], ['Output', 'found · answer · citations']],
    },
    eval: {
      what: 'RAGAS runs every question in <code>eval_questions.json</code> through the pipeline and scores each layer separately, using Claude as the judge. Time per stage and tokens per question are recorded too.',
      why: 'The scores tell you which layer to fix: retrieval (recall, precision) or generation (faithfulness, relevancy).',
      watch: ['Questions drafted by Claude from the same documents give optimistic scores. Replace them with 50–100 real questions over time.', 'An evaluation run makes many paid Claude calls.'],
      settings: [['Judge', c.claude_model], ['Questions', 'eval_questions.json']],
    },
  };
  return E[id];
}

/* ------------------------------------------------------------------ data */

async function api(path, opts = {}) {
  const o = { ...opts };
  if (o.json !== undefined) {
    o.method = o.method || 'POST';
    o.headers = { 'content-type': 'application/json' };
    o.body = JSON.stringify(o.json);
    delete o.json;
  }
  const r = await fetch(path, o);
  let data = null;
  try { data = await r.json(); } catch (_) { /* no body */ }
  if (!r.ok) throw new Error((data && data.detail) || `Request failed (${r.status})`);
  return data;
}

async function loadState() { S.state = await api('/api/state'); }
async function step(name) {
  if (!S.cache[name]) S.cache[name] = await api(`/api/step/${name}`);
  return S.cache[name];
}
async function loadHistory() {
  try { S.history = (await api('/api/history')).items; } catch (_) { S.history = []; }
}

async function runJob(start, label) {
  const { job } = await start;
  S.job = { id: job, label, status: 'running', log: [] };
  renderJob();
  let j;
  for (;;) {
    await sleep(600);
    j = await api(`/api/jobs/${job}`);
    S.job = { ...j, label };
    renderJob();
    if (j.status === 'done' || j.status === 'error') break;
  }
  const id = j.id;
  setTimeout(() => { if (S.job && S.job.id === id) { S.job = null; renderJob(); } }, j.status === 'error' ? 12000 : 2200);
  if (j.status === 'error') throw new Error(j.error || 'The step failed');
  return j.result;
}

const RUN_LABELS = { ingest: 'Preparing documents (steps 1–4)', parse: 'Parsing', chunk: 'Chunking', embed: 'Embedding', index: 'Indexing', eval: 'Running the evaluation' };

async function runStep(name) {
  try {
    await runJob(api(`/api/run/${name}`, { method: 'POST' }), RUN_LABELS[name] || name);
    S.cache = {};
    chunkLabels = {};
    await loadState();
    render();
    toast(`${RUN_LABELS[name] || name}: done`);
  } catch (e) { toast(e.message, true); await loadState().catch(() => {}); render(); }
}

async function ask() {
  const q = ($('#q') || {}).value?.trim();
  if (!q) { toast('Type a question first', true); return; }
  const groups = $$('.grp input:checked').map((i) => i.value);
  const sources = $$('.src input:checked').map((i) => i.value);
  const generate = !!($('#gen') && $('#gen').checked);
  S.form = { q, groups, sources, generate };
  try {
    S.trace = await runJob(api('/api/ask', { json: { question: q, groups, sources: sources.length ? sources : null, generate } }), 'Answering your question');
    await loadHistory();
    S.step = 'filter';
    render();
    $('#main').focus();
  } catch (e) { toast(e.message, true); }
}

/* ------------------------------------------------------------------ small UI helpers */

let toastTimer;
function toast(msg, bad = false) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast' + (bad ? ' bad' : '');
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, bad ? 7000 : 3000);
}

function renderJob() {
  const box = $('#job');
  const j = S.job;
  if (!j) { box.hidden = true; return; }
  box.hidden = false;
  const icon = j.status === 'done' ? '<span class="chip ok">done</span>' : j.status === 'error' ? '<span class="chip bad">failed</span>' : '<span class="spin"></span>';
  const log = (j.log || []).slice(-10).join('\n');
  box.innerHTML = `<h4>${icon} ${esc(j.label)}</h4>
    ${j.error ? `<p class="small" style="color:var(--bad);margin:8px 0 0">${esc(j.error)}</p>` : ''}
    ${log ? `<pre>${esc(log)}</pre>` : '<p class="small muted" style="margin:8px 0 0">Starting… the first run loads the models, which can take a minute.</p>'}
    ${j.seconds != null ? `<p class="tiny muted" style="margin:6px 0 0">${secs(j.seconds)}</p>` : ''}`;
}

function bar(value, max, cls = '') {
  const pct = max > 0 ? Math.max(2, Math.min(100, (value / max) * 100)) : 0;
  return `<div class="bar ${cls}"><i style="width:${pct}%"></i></div>`;
}
function normBar(value, min, max, cls = '') {
  const span = max - min;
  const pct = span > 0 ? 6 + ((value - min) / span) * 94 : 100;
  return `<div class="bar ${cls}"><i style="width:${pct}%"></i></div>`;
}

function colorFor(source) {
  const docs = (S.state && S.state.documents) || [];
  const i = docs.findIndex((d) => d.name === source);
  return PALETTE[(i < 0 ? 0 : i) % PALETTE.length];
}

let chunkLabels = {};
async function labels() {
  if (Object.keys(chunkLabels).length) return chunkLabels;
  try {
    const d = await step('chunk');
    if (d.exists) {
      const count = {};
      d.chunks.forEach((c) => { count[c.source] = (count[c.source] || 0) + 1; chunkLabels[c.chunk_id] = { n: count[c.source], c }; });
    }
  } catch (_) { /* fine without labels */ }
  return chunkLabels;
}
function passageMeta(p) {
  const L = chunkLabels[p.chunk_id];
  const heading = (p.headings && p.headings.length) ? p.headings[p.headings.length - 1] : (L && L.c.headings && L.c.headings.length ? L.c.headings[L.c.headings.length - 1] : '');
  const pages = p.pages && p.pages.length ? ` · p.${p.pages.join(',')}` : '';
  return `<span style="color:${colorFor(p.source)};font-weight:600">${esc(p.source)}</span>${L ? ` <span class="chip">passage ${L.n}</span>` : ''}${pages}${heading ? ` · ${esc(heading)}` : ''}`;
}

/* ------------------------------------------------------------------ chrome: status, flow strip, nav */

function done(id) {
  const o = (S.state && S.state.outputs) || {};
  if (id === 'docs') return ((S.state && S.state.documents) || []).length > 0;
  if (['filter', 'search', 'fuse', 'rerank'].includes(id)) return !!S.trace;
  if (id === 'answer') return !!(S.trace && S.trace.answer);
  const s = STEPS.find((x) => x.id === id);
  return !!(s && s.file && o[s.file] && o[s.file].exists);
}

function flowCount(id) {
  const t = S.trace;
  const st = S.state || {};
  const ch = S.cache.chunk;
  switch (id) {
    case 'docs': return `${(st.documents || []).length} files`;
    case 'parse': return done('parse') ? 'done' : 'not run';
    case 'chunk': return ch && ch.exists ? `${ch.total} passages` : (done('chunk') ? 'done' : 'not run');
    case 'embed': return done('embed') ? `${st.config.embed_dim} numbers each` : 'not run';
    case 'index': return done('index') ? 'ready' : 'not built';
    case 'filter': return t ? `${t.visibility.visible} of ${t.visibility.total} visible` : 'ask first';
    case 'search': return t ? `${st.config.candidates_per_retriever} + ${st.config.candidates_per_retriever}` : '—';
    case 'fuse': return t ? `${t.rrf_fused.length} kept` : '—';
    case 'rerank': return t ? `${t.final.length} to Claude` : '—';
    case 'answer': return t ? (t.answer ? (t.answer.found ? 'answered' : 'not found') : 'skipped') : '—';
    case 'eval': return done('eval') ? 'scored' : 'not run';
    default: return '';
  }
}

function renderChrome() {
  const st = S.state;
  const built = st.outputs.index && st.outputs.index.exists;
  $('#status').innerHTML = `
    <span class="pill ${built ? 'ok' : 'warn'}"><span class="dot"></span>${built ? 'Index ready' : 'Index not built yet'}</span>
    <span class="pill ${st.has_api_key ? 'ok' : 'warn'}"><span class="dot"></span>${st.has_api_key ? 'Claude connected' : 'No Claude key: retrieval only'}</span>
    <span class="pill">${esc(st.config.embed_model)} · Qdrant</span>`;

  let flow = '';
  let prev = null;
  STEPS.forEach((s) => {
    if (prev && prev !== s.phase) flow += '<span class="gap"></span>';
    else if (prev) flow += '<span class="arrow">→</span>';
    flow += `<button class="node ${s.phase} ${S.step === s.id ? 'on' : ''}" data-go="${s.id}">
      <div class="n">${s.n}</div><div class="t">${esc(s.title)}</div><div class="c">${esc(flowCount(s.id))}</div></button>`;
    prev = s.phase;
  });
  $('#flow').innerHTML = flow;

  let nav = '';
  let ph = null;
  STEPS.forEach((s) => {
    if (s.phase !== ph) { ph = s.phase; nav += `<div class="phase ${ph}">${PHASES[ph].label}<small>${PHASES[ph].note}</small></div>`; }
    nav += `<button class="navi ${S.step === s.id ? 'on' : ''} ${done(s.id) ? 'done' : ''}" data-go="${s.id}">
      <span class="num">${done(s.id) && S.step !== s.id ? '✓' : s.n}</span>
      <span><div class="nt">${esc(s.title)}</div><div class="ns">${esc(s.short)}</div></span></button>`;
  });
  $('#nav').innerHTML = nav;
}

/* ------------------------------------------------------------------ main panel */

function explainBlock(s) {
  const e = explain(s.id);
  return `<div class="panel">
    <div class="head"><div><div class="kicker ${s.phase}">Step ${s.n} · ${PHASES[s.phase].label}</div><h2>${esc(s.title)}</h2></div>
      <div class="row">${actionButtons(s)}</div></div>
    <div class="explain">
      <div><h4>What happens</h4><p>${e.what}</p><h4>Why it matters</h4><p>${e.why}</p></div>
      <div class="box"><h4>What to look for</h4><ul>${e.watch.map((w) => `<li>${w}</li>`).join('')}</ul></div>
    </div>
    <div class="settings">${e.settings.map(([k, v]) => `<span class="chip"><b>${esc(k)}</b>${esc(v)}</span>`).join('')}</div>
  </div>`;
}

function actionButtons(s) {
  const busy = S.job && S.job.status === 'running' ? 'disabled' : '';
  if (s.phase === 'prepare') {
    const single = s.file ? `<button class="btn sm" data-run="${s.file}" ${busy}>Run this step</button>` : '';
    return `${single}<button class="btn sm primary" data-run="ingest" ${busy}>Run steps 1–4</button>`;
  }
  if (s.id === 'eval') {
    return `<button class="btn sm primary" data-run="eval" ${busy} ${S.state.has_api_key ? '' : 'disabled title="Needs ANTHROPIC_API_KEY"'}>Run evaluation</button>`;
  }
  return '';
}

function navButtons(s) {
  const i = STEPS.findIndex((x) => x.id === s.id);
  const p = STEPS[i - 1];
  const n = STEPS[i + 1];
  return `<div class="navbtns">
    ${p ? `<button class="btn" data-go="${p.id}">← ${esc(p.title)}</button>` : '<span></span>'}
    ${n ? `<button class="btn primary" data-go="${n.id}">Next: ${esc(n.title)} →</button>` : '<span></span>'}</div>`;
}

function askBar() {
  const st = S.state;
  const f = S.form || { q: '', groups: ['public'], sources: [], generate: st.has_api_key };
  const ready = st.outputs.index && st.outputs.index.exists;
  return `<div class="askbar">
    <div class="q"><input type="text" id="q" placeholder="Ask a question about your documents…" value="${esc(f.q)}" aria-label="Question">
      <button class="btn primary" id="askbtn" ${ready ? '' : 'disabled title="Build the index first (steps 1–4)"'}>Ask</button></div>
    <div class="opts">
      <div><span class="lbl">Ask as someone in these groups</span><div class="row grp">${st.groups.map((g) => `<label class="check"><input type="checkbox" value="${esc(g)}" ${f.groups.includes(g) ? 'checked' : ''}>${esc(g)}</label>`).join('')}</div></div>
      <div><span class="lbl">Only search these files (optional)</span><div class="row src">${st.documents.map((d) => `<label class="check"><input type="checkbox" value="${esc(d.name)}" ${f.sources.includes(d.name) ? 'checked' : ''}>${esc(d.name)}</label>`).join('') || '<span class="small muted">No documents yet</span>'}</div></div>
      <div><span class="lbl">Step 9</span><label class="check"><input type="checkbox" id="gen" ${f.generate && st.has_api_key ? 'checked' : ''} ${st.has_api_key ? '' : 'disabled'}>Write the answer with Claude (paid API call)</label>
        ${st.has_api_key ? '' : '<span class="tiny muted">Add ANTHROPIC_API_KEY to .env to enable</span>'}</div>
    </div>
    ${st.examples && st.examples.length ? `<div class="examples"><span class="tiny muted" style="align-self:center">Try:</span>${st.examples.map((x) => `<button data-example="${esc(x)}">${esc(x)}</button>`).join('')}</div>` : ''}
    ${ready ? '' : '<p class="small muted" style="margin:10px 0 0">The index is not built yet. Run steps 1–4 first.</p>'}
  </div>`;
}

function emptyStep(title, body, runName) {
  const busy = S.job && S.job.status === 'running' ? 'disabled' : '';
  return `<div class="panel empty"><h3>${esc(title)}</h3><p>${body}</p>
    ${runName ? `<button class="btn primary" data-run="${runName}" ${busy}>${runName === 'ingest' ? 'Run steps 1–4' : 'Run this step'}</button>` : ''}</div>`;
}

async function render() {
  renderChrome();
  const s = STEPS.find((x) => x.id === S.step);
  const main = $('#main');
  let html = '';
  if (s.phase === 'ask') html += askBar();
  html += explainBlock(s);
  main.innerHTML = html + '<div id="viz"><div class="panel empty"><span class="spin"></span></div></div>';
  let viz = '';
  try {
    viz = await VIEWS[s.id]();
  } catch (e) {
    viz = `<div class="panel empty"><h3>Could not load this step</h3><p>${esc(e.message)}</p></div>`;
  }
  if (S.step !== s.id) return;              // the user moved on while this was loading
  $('#viz').innerHTML = viz + navButtons(s);
  if (AFTER[s.id]) AFTER[s.id]();
  renderChrome();
}

/* ------------------------------------------------------------------ step views */

const VIEWS = {
  async docs() {
    const docs = S.state.documents;
    const rows = docs.map((d) => `<tr><td><span style="color:${colorFor(d.name)};font-weight:600">${esc(d.name)}</span></td>
      <td class="muted">${fmt(d.size / 1024, 1)} KB</td>
      <td><div class="row"><input type="text" value="${esc(d.access.join(', '))}" data-access="${esc(d.name)}" aria-label="Groups for ${esc(d.name)}" style="width:200px">
      <button class="btn sm" data-saveaccess="${esc(d.name)}">Save</button></div></td></tr>`).join('');
    return `<div class="panel"><h3>Files in documents/</h3>
      ${docs.length ? `<div class="scroll"><table class="t"><thead><tr><th>File</th><th>Size</th><th>Access groups (comma separated)</th></tr></thead><tbody>${rows}</tbody></table></div>` : '<p class="muted">No documents yet. Add some below.</p>'}
      <h3 style="margin-top:18px">Add documents</h3>
      <div class="uploader" style="margin-top:8px"><input type="file" id="files" multiple aria-label="Choose files">
        <input type="text" id="upgroups" value="public" aria-label="Groups for the new files" style="width:170px" title="Access groups for these files">
        <button class="btn primary" id="upload">Upload</button>
        <span class="tiny muted">Files go into documents/. Then run steps 1–4.</span></div></div>`;
  },

  async parse() {
    const d = await step('parse');
    if (!d.exists) return emptyStep('Nothing parsed yet', 'Run the parse step to turn your files into text.', 'parse');
    return d.documents.map((doc) => `<div class="panel">
      <div class="head"><h3 style="color:${colorFor(doc.source)}">${esc(doc.source)}</h3>
        <div class="row"><span class="chip acc">${esc(doc.parser)}</span>
        ${doc.pages != null ? `<span class="chip"><b>pages</b>${doc.pages}</span>` : ''}
        ${doc.tables != null ? `<span class="chip"><b>tables</b>${doc.tables}</span>` : ''}
        <span class="chip"><b>characters</b>${fmt(doc.markdown_chars, 0)}</span>
        <span class="chip"><b>time</b>${secs(doc.seconds)}</span>
        ${(doc.access || []).map((g) => `<span class="chip ${g === 'public' ? '' : 'warn'}">${esc(g)}</span>`).join('')}</div></div>
      <div class="md">${renderMarkdown(doc.markdown)}</div>
      ${doc.truncated ? '<p class="tiny muted" style="margin-top:6px">Preview shows the first 20,000 characters.</p>' : ''}</div>`).join('');
  },

  async chunk() {
    const d = await step('chunk');
    if (!d.exists) return emptyStep('No passages yet', 'Run the chunk step after parsing.', 'chunk');
    await labels();
    const lim = S.state.config.chunk_max_tokens;
    const toks = d.chunks.map((c) => c.n_tokens || 0);
    const avg = toks.reduce((a, b) => a + b, 0) / Math.max(1, toks.length);
    const bySource = {};
    d.chunks.forEach((c, i) => { (bySource[c.source] = bySource[c.source] || []).push({ ...c, i }); });
    if (!S.sel.chunk || !d.chunks.find((c) => c.chunk_id === S.sel.chunk)) S.sel.chunk = d.chunks[0] && d.chunks[0].chunk_id;
    const strips = Object.entries(bySource).map(([src, cs]) => `<h4 style="color:${colorFor(src)}">${esc(src)} <span class="muted small">· ${cs.length} passage${cs.length === 1 ? '' : 's'}</span></h4>
      <div class="docstrip">${cs.map((c, k) => {
        const fill = Math.min(1, (c.n_tokens || 0) / lim);
        return `<button class="cblock ${S.sel.chunk === c.chunk_id ? 'sel' : ''}" data-chunk="${c.chunk_id}" title="${esc((c.headings || []).join(' › ') || 'passage')} · ${c.n_tokens} tokens"
          style="width:${Math.max(26, Math.round(fill * 96))}px;background:color-mix(in srgb, ${colorFor(src)} ${Math.round(15 + fill * 45)}%, transparent)">${k + 1}</button>`;
      }).join('')}</div>`).join('');
    return `<div class="panel"><div class="stats">
        <div class="stat"><b>${fmt(d.total, 0)}</b><span>passages</span></div>
        <div class="stat"><b>${fmt(avg, 0)}</b><span>average tokens</span></div>
        <div class="stat"><b>${fmt(Math.max(0, ...toks), 0)}</b><span>largest (limit ${lim})</span></div>
        <div class="stat"><b>${Object.keys(bySource).length}</b><span>documents</span></div></div>
      <h3>Size of each passage</h3><p class="small muted">Each block is one passage; wider and darker means closer to the ${lim}-token limit. Click one to read it.</p>
      ${histogram(toks, lim)}
      ${strips}</div>
      <div class="panel" id="chunkdetail">${chunkDetail(d.chunks.find((c) => c.chunk_id === S.sel.chunk), lim)}</div>`;
  },

  async embed() {
    const d = await step('embed');
    if (!d.exists) return emptyStep('No vectors yet', 'Run the embed step after chunking.', 'embed');
    await labels();
    if (!S.sel.embed || !d.items.find((x) => x.chunk_id === S.sel.embed)) S.sel.embed = d.items[0] && d.items[0].chunk_id;
    const sel = d.items.find((x) => x.chunk_id === S.sel.embed);
    return `<div class="panel"><div class="stats">
        <div class="stat"><b>${fmt(d.count, 0)}</b><span>passages embedded</span></div>
        <div class="stat"><b>${fmt(d.dense_dim, 0)}</b><span>numbers per meaning vector</span></div>
        <div class="stat"><b>${secs(d.dense_seconds)}</b><span>meaning vectors</span></div>
        <div class="stat"><b>${secs(d.sparse_seconds)}</b><span>keyword vectors</span></div></div>
      <div class="cols"><div><h3>Meaning map</h3>
        <p class="small muted">Every dot is a passage. Dots close together have similar meaning. The ${d.dense_dim} numbers are squeezed onto two axes, which keep ${fmt(((d.explained[0] || 0) + (d.explained[1] || 0)) * 100, 0)}% of the spread. Click a dot.</p>
        ${meaningMap(d.items, { selected: S.sel.embed, query: S.trace && S.trace.query_point, highlight: S.trace ? S.trace.final.map((p) => p.chunk_id) : [] })}
        ${legend(d.items)}</div>
        <div><h3>Selected passage</h3>${sel ? `<p class="small">${passageMeta({ ...sel, pages: [] })}</p>
          <div class="passage" style="max-height:120px">${esc(sel.snippet)}…</div>
          <h4 style="margin-top:14px">Its meaning vector (first 256 of ${d.dense_dim} numbers)</h4>${fingerprint(sel.dense_preview)}
          <p class="tiny muted" style="margin-top:6px">Blue is negative, orange is positive. Nobody reads these directly; search compares whole vectors.</p>
          <h4 style="margin-top:12px">Its keyword vector</h4><p class="small"><b>${sel.sparse_terms}</b> distinct words recorded, each weighted by how often it appears.</p>` : ''}</div></div></div>`;
  },

  async index() {
    const d = await step('index');
    if (!d.exists) return emptyStep('Index not built yet', 'Run the index step after embedding.', 'index');
    const ix = d.index;
    const maxG = Math.max(1, ...Object.values(d.per_group));
    const maxS = Math.max(1, ...Object.values(d.per_source));
    return `<div class="panel"><div class="stats">
        <div class="stat"><b>${fmt(ix.points, 0)}</b><span>points stored</span></div>
        <div class="stat"><b>${esc(ix.status.replace('CollectionStatus.', '').toLowerCase())}</b><span>collection status</span></div>
        <div class="stat"><b>${esc(ix.dense.index)}</b><span>meaning index (${esc(ix.dense.distance)})</span></div>
        <div class="stat"><b>${esc(ix.sparse.modifier)}</b><span>keyword weighting</span></div></div>
      <p class="small muted">Stored at <code>${esc(ix.storage)}</code> in collection <code>${esc(ix.collection)}</code>. Filter indexes: ${ix.payload_indexes.map((x) => `<code>${esc(x)}</code>`).join(', ')}.</p>
      <div class="cols"><div><h3>Passages per access group</h3>${Object.entries(d.per_group).map(([g, n]) => `<div style="margin:8px 0"><div class="row small"><span class="chip ${g === 'public' ? '' : 'warn'}">${esc(g)}</span><span class="muted">${n}</span></div>${bar(n, maxG)}</div>`).join('')}</div>
      <div><h3>Passages per file</h3>${Object.entries(d.per_source).map(([s, n]) => `<div style="margin:8px 0"><div class="row small"><span style="color:${colorFor(s)};font-weight:600">${esc(s)}</span><span class="muted">${n}</span></div>${bar(n, maxS)}</div>`).join('')}</div></div></div>`;
  },

  async filter() {
    const t = S.trace;
    if (!t) return noTrace();
    const v = t.visibility;
    const pct = v.total ? (v.visible / v.total) * 100 : 0;
    return `<div class="panel"><h3>Question</h3><p style="font-size:16px">“${esc(t.question)}”</p>
      <div class="row" style="margin-bottom:14px"><span class="small muted">Asked as:</span>${t.groups.map((g) => `<span class="chip acc">${esc(g)}</span>`).join('')}
        ${t.sources ? `<span class="small muted">· only in:</span>${t.sources.map((s) => `<span class="chip">${esc(s)}</span>`).join('')}` : ''}</div>
      <div class="stats"><div class="stat"><b>${v.total}</b><span>passages in the index</span></div>
        <div class="stat"><b style="color:var(--ok)">${v.visible}</b><span>allowed for this question</span></div>
        <div class="stat"><b style="color:var(--muted)">${v.hidden}</b><span>hidden${v.hidden_files ? ` (${v.hidden_files} file${v.hidden_files > 1 ? 's' : ''})` : ''}</span></div></div>
      <div class="tokbar" style="height:16px"><i style="width:${pct}%;background:var(--ok)"></i></div>
      <p class="small muted" style="margin-top:8px">Green is what both searches may look at. The hidden passages never take part in ranking.</p>
      <h4 style="margin-top:12px">Files that can be searched</h4><div class="row" style="margin-top:6px">${v.visible_sources.map((s) => `<span class="chip" style="color:${colorFor(s)}">${esc(s)}</span>`).join('') || '<span class="small muted">None: nothing can be found for this question.</span>'}</div></div>`;
  },

  async search() {
    const t = S.trace;
    if (!t) return noTrace();
    await labels();
    const denseIds = new Set(t.dense_top.map((h) => h.chunk_id));
    const bmIds = new Set(t.bm25_top.map((h) => h.chunk_id));
    const list = (hits, other, label) => {
      const sc = hits.map((h) => h.score);
      const mn = Math.min(...sc); const mx = Math.max(...sc);
      return `<div><h3>${label}</h3><p class="tiny muted">Top ${hits.length} shown (each search returns up to ${S.state.config.candidates_per_retriever}).</p>
        ${hits.map((h, i) => `<div class="hit ${other.has(h.chunk_id) ? 'both' : ''}" data-open><span class="rank">${i + 1}</span><div>
          <div class="meta">${passageMeta(h)}${other.has(h.chunk_id) ? '<span class="chip acc">in both</span>' : ''}</div>
          <div class="txt">${esc(h.text)}</div><div class="row tiny muted" style="gap:8px">score ${fmt(h.score, 3)}</div>${normBar(h.score, mn, mx)}</div></div>`).join('') || '<p class="muted small">Nothing found.</p>'}</div>`;
    };
    const both = [...denseIds].filter((x) => bmIds.has(x)).length;
    let map = '';
    try {
      const e = await step('embed');
      if (e.exists && t.query_point) {
        map = `<div class="panel"><h3>Where your question lands</h3><p class="small muted">The star is your question on the meaning map. Ringed dots are the top 10 meaning-search results.</p>
          ${meaningMap(e.items, { query: t.query_point, highlight: t.dense_top.map((h) => h.chunk_id) })}${legend(e.items)}</div>`;
      }
    } catch (_) { /* map is optional */ }
    return `<div class="panel"><p class="small"><b>${both}</b> passage${both === 1 ? '' : 's'} appear in both top-10 lists (highlighted).</p>
      <div class="cols">${list(t.dense_top, bmIds, 'Meaning search (dense)')}${list(t.bm25_top, denseIds, 'Keyword search (BM25)')}</div></div>${map}`;
  },

  async fuse() {
    const t = S.trace;
    if (!t) return noTrace();
    await labels();
    const dr = Object.fromEntries(t.dense_top.map((h, i) => [h.chunk_id, i + 1]));
    const br = Object.fromEntries(t.bm25_top.map((h, i) => [h.chunk_id, i + 1]));
    const mx = Math.max(...t.rrf_fused.map((h) => h.rrf_score), 0);
    const rows = t.rrf_fused.map((h, i) => `<tr><td><b>${i + 1}</b></td>
      <td>${dr[h.chunk_id] ? `<span class="chip acc">#${dr[h.chunk_id]}</span>` : '<span class="muted">—</span>'}</td>
      <td>${br[h.chunk_id] ? `<span class="chip acc">#${br[h.chunk_id]}</span>` : '<span class="muted">—</span>'}</td>
      <td style="min-width:120px">${fmt(h.rrf_score, 4)}${bar(h.rrf_score, mx)}</td>
      <td><div class="meta small">${passageMeta(h)}</div><div class="small" style="display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden">${esc(h.text)}</div></td></tr>`).join('');
    return `<div class="panel"><h3>Fused list (${t.rrf_fused.length} passages)</h3>
      <p class="small muted">Position in each search, then the fused score. A dash means the passage was below 10th in that search, or not found by it.</p>
      <div class="scroll"><table class="t"><thead><tr><th>Fused</th><th>Meaning</th><th>Keywords</th><th>Fused score</th><th>Passage</th></tr></thead><tbody>${rows}</tbody></table></div></div>`;
  },

  async rerank() {
    const t = S.trace;
    if (!t) return noTrace();
    await labels();
    const final = t.final;
    const sc = final.map((p) => p.rerank_score ?? 0);
    const all = t.rrf_fused.map((h) => h.rerank_score ?? 0);
    const mn = Math.min(...all, ...sc); const mx = Math.max(...all, ...sc);
    return `<div class="panel"><h3>How the order changed</h3>
      <p class="small muted">Left: order after fusion. Right: order after the re-ranker read each passage with the question. The top ${final.length} on the right are sent to Claude.</p>
      ${slope(t.rrf_fused, final.length)}</div>
      <div class="panel"><h3>Sent to Claude (${final.length})</h3>
      ${final.map((p, i) => `<div class="hit" data-open><span class="rank">${i + 1}</span><div><div class="meta">${passageMeta(p)}</div>
        <div class="txt">${esc(p.text)}</div><div class="tiny muted">re-rank score ${fmt(p.rerank_score, 3)}</div>${normBar(p.rerank_score ?? 0, mn, mx, 'ok')}</div></div>`).join('')}</div>`;
  },

  async answer() {
    const t = S.trace;
    if (!t) return noTrace();
    await labels();
    const a = t.answer;
    const cited = new Set(a ? a.citations : []);
    const tm = t.timings || {};
    const parts = [['Embed question', tm.embed_query], ['Search', tm.search], ['Re-rank', tm.rerank], ['Claude', tm.generation]].filter(([, v]) => v != null);
    const total = parts.reduce((s, [, v]) => s + v, 0) || 1;
    let off = 0;
    const water = parts.map(([k, v]) => { const left = (off / total) * 100; off += v; return `<span>${k}</span><div class="w"><i style="left:${left}%;width:${Math.max(0.8, (v / total) * 100)}%"></i></div><span class="muted">${secs(v)}</span>`; }).join('');
    const answerHtml = a
      ? `<div class="answer ${a.found ? '' : 'no'}"><div class="row" style="margin-bottom:6px"><span class="chip ${a.found ? 'ok' : 'warn'}">${a.found ? 'found in the documents' : 'not found in the documents'}</span>
          ${(a.citations || []).map((c) => `<span class="chip">cites ${esc(chunkLabels[c] ? `${chunkLabels[c].c.source} passage ${chunkLabels[c].n}` : c)}</span>`).join('')}</div>${esc(a.answer)}</div>
        <p class="tiny muted" style="margin-top:8px">${esc(a.model || '')} · ${fmt(a.usage && a.usage.input_tokens, 0)} tokens in, ${fmt(a.usage && a.usage.output_tokens, 0)} out</p>`
      : `<div class="answer no"><b>Claude was skipped (retrieval only).</b> Below is exactly what Claude would have received. Tick “Write the answer with Claude” and ask again to get an answer.</div>`;
    return `<div class="panel"><h3>Answer</h3><p class="small muted" style="margin-top:2px">“${esc(t.question)}”</p>${answerHtml}
        <details style="margin-top:12px" ${a ? '' : 'open'}><summary>Show the prompt sent to Claude</summary>
          <h4 style="margin-top:10px">Instructions (system prompt)</h4><pre>${esc(t.prompt_preview.system)}</pre>
          <h4>Message (passages and question)</h4><pre>${esc(t.prompt_preview.user)}</pre></details></div>
      <div class="panel"><h3>Passages Claude ${a ? 'used' : 'would get'}</h3>${a ? '<p class="small muted">Green passages are the ones the answer cites.</p>' : ''}
        ${t.final.map((p, i) => `<div class="hit ${cited.has(p.chunk_id) ? 'cited' : ''}" data-open><span class="rank">${i + 1}</span><div><div class="meta">${passageMeta(p)}${cited.has(p.chunk_id) ? '<span class="chip ok">cited</span>' : ''}</div><div class="txt">${esc(p.text)}</div></div></div>`).join('')}</div>
      <div class="panel"><h3>Where the time went</h3><div class="waterfall" style="margin-top:10px">${water}</div>
        <p class="tiny muted" style="margin-top:8px">The first question after starting also loads the models, so it is slower.</p></div>
      ${S.history.length ? `<div class="panel"><h3>Recent questions with answers</h3>${S.history.map((h) => `<div class="hit"><span class="rank">${h.found ? '✓' : '–'}</span><div><div class="meta">${esc(h.time || '')} · ${fmt(h.total_s)} s</div><div class="small"><b>${esc(h.question)}</b></div><div class="txt small">${esc(h.answer)}</div></div></div>`).join('')}</div>` : ''}`;
  },

  async eval() {
    const d = await step('eval');
    if (!d.exists) {
      return emptyStep('No evaluation yet', S.state.has_api_key ? 'Run the evaluation to score retrieval and answers on eval_questions.json. It makes paid Claude calls.' : 'The evaluation uses Claude as the judge. Add ANTHROPIC_API_KEY to .env, restart, then run it here or with <code>python rag.py eval</code>.', S.state.has_api_key ? 'eval' : null);
    }
    const s = d.summary;
    const how = s.how_to_read || {};
    const names = { context_recall: 'Context recall', llm_context_precision_with_reference: 'Context precision', faithfulness: 'Faithfulness', answer_relevancy: 'Answer relevancy' };
    const layer = { context_recall: 'retrieval', llm_context_precision_with_reference: 'retrieval', faithfulness: 'generation', answer_relevancy: 'generation' };
    const color = (v) => (v == null ? 'var(--faint)' : v >= 0.85 ? 'var(--ok)' : v >= 0.6 ? 'var(--warn)' : 'var(--bad)');
    const cards = Object.entries(s.quality).map(([k, v]) => `<div class="stat"><span>${esc(layer[k] || '')}</span><b style="color:${color(v)}">${fmt(v, 2)}</b>
      <div class="gauge"><i style="width:${(v || 0) * 100}%;background:${color(v)}"></i></div><span><b style="font-size:13px;display:inline;color:var(--ink)">${esc(names[k] || k)}</b>: ${esc(how[k] || '')}</span></div>`).join('');
    const lat = s.efficiency.latency_s;
    const metrics = Object.keys(s.quality);
    const rows = d.per_question.map((q) => `<tr><td class="small"><b>${esc(q.question)}</b><div class="muted tiny">${esc((q.answer || '').slice(0, 160))}</div></td>
      <td>${q.found ? '<span class="chip ok">yes</span>' : '<span class="chip warn">no</span>'}</td>
      ${metrics.map((m) => { const v = q.scores[m]; return `<td><span class="score" style="background:color-mix(in srgb, ${color(v)} 18%, transparent);color:${color(v)}">${fmt(v, 2)}</span></td>`; }).join('')}
      <td class="muted small" style="white-space:nowrap">${secs(q.timings_s.total)}</td></tr>`).join('');
    return `<div class="panel"><h3>Quality on ${s.n_questions} questions</h3><div class="stats" style="margin-top:10px">${cards}</div></div>
      <div class="panel"><h3>Speed and cost</h3><div class="stats" style="margin-top:10px">
        <div class="stat"><b>${fmt(s.efficiency.answered_rate * 100, 0)}%</b><span>questions answered (not “not found”)</span></div>
        <div class="stat"><b>${fmt(lat.total.p50)} s</b><span>typical total time (max ${fmt(lat.total.max)} s)</span></div>
        <div class="stat"><b>${fmt(lat.retrieval_total.p50)} s</b><span>typical retrieval time</span></div>
        <div class="stat"><b>${fmt(s.efficiency.avg_input_tokens, 0)}</b><span>average tokens sent to Claude</span></div></div></div>
      <div class="panel"><h3>Each question</h3><div class="scroll"><table class="t"><thead><tr><th>Question</th><th>Found</th>${metrics.map((m) => `<th>${esc(names[m] || m)}</th>`).join('')}<th>Time</th></tr></thead><tbody>${rows}</tbody></table></div></div>`;
  },
};

function noTrace() {
  return `<div class="panel empty"><h3>Ask a question to see this step</h3><p>Use the box above. Retrieval works without a Claude key; only the final answer needs one.</p></div>`;
}

const AFTER = {
  chunk() {
    $$('[data-chunk]').forEach((b) => b.addEventListener('click', async () => {
      S.sel.chunk = b.dataset.chunk;
      const d = await step('chunk');
      $$('[data-chunk]').forEach((x) => x.classList.toggle('sel', x.dataset.chunk === S.sel.chunk));
      $('#chunkdetail').innerHTML = chunkDetail(d.chunks.find((c) => c.chunk_id === S.sel.chunk), S.state.config.chunk_max_tokens);
      $('#chunkdetail').scrollIntoView({ block: 'nearest' });
    }));
  },
  embed() { bindMap(true); },
  search() { bindMap(false); },
};

/* ------------------------------------------------------------------ visual building blocks */

function renderMarkdown(md) {
  const out = [];
  let tbl = [];
  const flush = () => { if (tbl.length) { out.push(`<div class="tbl">${esc(tbl.join('\n'))}</div>`); tbl = []; } };
  (md || '').split('\n').forEach((line) => {
    if (/^\s*\|/.test(line)) { tbl.push(line); return; }
    flush();
    const m = line.match(/^(#{1,6})\s+(.*)$/);
    if (m) out.push(`<div class="h h${Math.min(3, m[1].length)}">${esc(m[2])}</div>`);
    else if (line.trim()) out.push(`<p>${esc(line)}</p>`);
  });
  flush();
  return out.join('') || '<p class="muted">No text was extracted.</p>';
}

function chunkDetail(c, lim) {
  if (!c) return '<p class="muted">Select a passage.</p>';
  const L = chunkLabels[c.chunk_id];
  let embedHtml = esc(c.embed_text);
  if (c.embed_text && c.text && c.embed_text !== c.text && c.embed_text.endsWith(c.text)) {
    embedHtml = `<mark>${esc(c.embed_text.slice(0, c.embed_text.length - c.text.length))}</mark>${esc(c.text)}`;
  }
  return `<div class="head"><div><h3>${esc(c.source)}${L ? ` · passage ${L.n}` : ''}</h3>
      <div class="crumbs">${(c.headings || []).map((h) => `<span>${esc(h)}</span>`).join('') || '<span>no heading</span>'}</div></div>
      <div class="row"><span class="chip"><b>tokens</b>${c.n_tokens} / ${lim}</span>${c.pages && c.pages.length ? `<span class="chip"><b>pages</b>${c.pages.join(', ')}</span>` : ''}
      ${(c.access || []).map((g) => `<span class="chip ${g === 'public' ? '' : 'warn'}">${esc(g)}</span>`).join('')}<span class="chip mono">${esc(c.chunk_id)}</span></div></div>
    <div class="tokbar"><i style="width:${Math.min(100, (c.n_tokens / lim) * 100)}%"></i></div>
    <div class="cols" style="margin-top:12px"><div><h4>Passage text (what Claude would read)</h4><div class="passage">${esc(c.text)}</div></div>
      <div><h4>Text that gets embedded</h4><div class="passage">${embedHtml}</div>
      <p class="tiny muted" style="margin-top:6px">${embedHtml.startsWith('<mark>') ? 'The highlighted heading path is added so the passage keeps its context.' : 'Same as the passage text.'}</p></div></div>`;
}

function histogram(values, lim) {
  if (!values.length) return '';
  const bins = 8;
  const top = Math.max(lim, ...values);
  const w = top / bins;
  const counts = Array(bins).fill(0);
  values.forEach((v) => { counts[Math.min(bins - 1, Math.floor(v / w))] += 1; });
  const mx = Math.max(...counts);
  const W = 520; const H = 110; const bw = W / bins;
  const rects = counts.map((c, i) => {
    const h = mx ? (c / mx) * (H - 44) : 0;
    return `<rect x="${i * bw + 3}" y="${H - 20 - h}" width="${bw - 6}" height="${h}" rx="3" fill="var(--accent)" opacity=".75"><title>${Math.round(i * w)}–${Math.round((i + 1) * w)} tokens: ${c}</title></rect>
      ${c ? `<text x="${i * bw + bw / 2}" y="${H - 24 - h}" text-anchor="middle">${c}</text>` : ''}
      <text x="${i * bw + bw / 2}" y="${H - 5}" text-anchor="middle">${Math.round((i + 1) * w)}</text>`;
  }).join('');
  return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;max-width:560px;margin:4px 0 12px" role="img" aria-label="Histogram of passage sizes in tokens">${rects}</svg>`;
}

let mapItems = [];
function meaningMap(items, opts = {}) {
  mapItems = items;
  if (!items.length) return '<p class="muted small">No passages to plot.</p>';
  const W = 560; const H = 380; const pad = 26;
  const pts = items.map((i) => i.point);
  if (opts.query) pts.push(opts.query);
  const xs = pts.map((p) => p[0]); const ys = pts.map((p) => p[1]);
  const x0 = Math.min(...xs); const x1 = Math.max(...xs); const y0 = Math.min(...ys); const y1 = Math.max(...ys);
  const sx = (x) => (x1 - x0 ? pad + ((x - x0) / (x1 - x0)) * (W - 2 * pad) : W / 2);
  const sy = (y) => (y1 - y0 ? H - pad - ((y - y0) / (y1 - y0)) * (H - 2 * pad) : H / 2);
  const hl = new Set(opts.highlight || []);
  const dots = items.map((it) => {
    const cx = sx(it.point[0]); const cy = sy(it.point[1]);
    const sel = opts.selected === it.chunk_id;
    return `${hl.has(it.chunk_id) ? `<circle cx="${cx}" cy="${cy}" r="10" fill="none" stroke="var(--ink)" stroke-width="1.5" opacity=".55"/>` : ''}
      <circle class="pt" data-id="${it.chunk_id}" cx="${cx}" cy="${cy}" r="${sel ? 7.5 : 5.5}" fill="${colorFor(it.source)}" stroke="${sel ? 'var(--ink)' : 'var(--panel)'}" stroke-width="${sel ? 2.5 : 1}" style="cursor:pointer"/>`;
  }).join('');
  let star = '';
  if (opts.query) {
    const cx = sx(opts.query[0]); const cy = sy(opts.query[1]);
    const p = [];
    for (let k = 0; k < 10; k += 1) { const r = k % 2 ? 5 : 12; const a = (Math.PI / 5) * k - Math.PI / 2; p.push(`${cx + r * Math.cos(a)},${cy + r * Math.sin(a)}`); }
    star = `<polygon points="${p.join(' ')}" fill="#facc15" stroke="#854d0e" stroke-width="1.5"><title>Your question</title></polygon>`;
  }
  return `<svg class="map" id="map" viewBox="0 0 ${W} ${H}" role="img" aria-label="Meaning map of passages">${dots}${star}</svg>`;
}

function legend(items) {
  const srcs = [...new Set(items.map((i) => i.source))];
  return `<div class="legend">${srcs.map((s) => `<span><i style="background:${colorFor(s)}"></i>${esc(s)}</span>`).join('')}
    ${S.trace && S.trace.query_point ? '<span><i style="background:#facc15;border-radius:2px"></i>your question</span>' : ''}</div>`;
}

function bindMap(selectable) {
  let tip = $('.tip');
  if (!tip) { tip = document.createElement('div'); tip.className = 'tip'; tip.hidden = true; document.body.appendChild(tip); }
  $$('#map .pt').forEach((c) => {
    const it = mapItems.find((x) => x.chunk_id === c.dataset.id);
    c.addEventListener('mousemove', (ev) => {
      tip.hidden = false;
      tip.style.left = `${ev.clientX + 14}px`; tip.style.top = `${ev.clientY + 14}px`;
      const L = chunkLabels[it.chunk_id];
      tip.innerHTML = `<b>${esc(it.source)}${L ? ` · passage ${L.n}` : ''}</b><br>${esc((it.headings || []).join(' › '))}<br>${esc(it.snippet.slice(0, 120))}…`;
    });
    c.addEventListener('mouseleave', () => { tip.hidden = true; });
    if (selectable) c.addEventListener('click', () => { S.sel.embed = it.chunk_id; tip.hidden = true; render(); });
  });
}

function fingerprint(values) {
  const mx = Math.max(...values.map((v) => Math.abs(v)), 1e-9);
  return `<div class="fp" role="img" aria-label="Colour grid of vector values">${values.map((v) => {
    const a = Math.min(1, Math.abs(v) / mx);
    const col = v >= 0 ? `rgba(234, 88, 12, ${0.12 + a * 0.88})` : `rgba(37, 99, 235, ${0.12 + a * 0.88})`;
    return `<i style="background:${col}" title="${fmt(v, 4)}"></i>`;
  }).join('')}</div>`;
}

function slope(fused, keep) {
  const after = [...fused].sort((a, b) => (b.rerank_score ?? -1e9) - (a.rerank_score ?? -1e9));
  const pos = Object.fromEntries(after.map((h, i) => [h.chunk_id, i]));
  const n = fused.length;
  const rowH = 24; const W = 640; const H = n * rowH + 30; const lx = 200; const rx = W - 200;
  const name = (h) => {
    const L = chunkLabels[h.chunk_id];
    const s = L ? `${h.source} · ${L.n}` : h.source;
    return s.length > 26 ? `${s.slice(0, 25)}…` : s;
  };
  let svg = `<text x="${lx}" y="14" text-anchor="end" style="font-weight:600">after fusion</text><text x="${rx}" y="14" style="font-weight:600">after re-ranking</text>`;
  if (keep < n) svg += `<rect x="${rx - 8}" y="${22}" width="${200}" height="${keep * rowH}" rx="6" fill="var(--ok-bg)"/>`;
  fused.forEach((h, i) => {
    const y1 = 30 + i * rowH + 6; const y2 = 30 + pos[h.chunk_id] * rowH + 6;
    const kept = pos[h.chunk_id] < keep;
    const color = kept ? 'var(--ok)' : 'var(--faint)';
    svg += `<line x1="${lx + 8}" y1="${y1}" x2="${rx - 12}" y2="${y2}" stroke="${color}" stroke-width="${kept ? 2 : 1}" opacity="${kept ? 0.9 : 0.45}"/>
      <circle cx="${lx + 8}" cy="${y1}" r="3.5" fill="${colorFor(h.source)}"/><circle cx="${rx - 12}" cy="${y2}" r="3.5" fill="${colorFor(h.source)}"/>
      <text x="${lx}" y="${y1 + 4}" text-anchor="end">${i + 1}. ${esc(name(h))}</text>
      <text x="${rx}" y="${y2 + 4}" style="${kept ? 'fill:var(--ink);font-weight:600' : ''}">${pos[h.chunk_id] + 1}. ${esc(name(h))}</text>`;
  });
  return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;max-width:760px" role="img" aria-label="Order before and after re-ranking">${svg}</svg>`;
}

/* ------------------------------------------------------------------ events */

document.addEventListener('click', async (ev) => {
  const go = ev.target.closest('[data-go]');
  if (go) { S.step = go.dataset.go; render(); window.scrollTo({ top: 0 }); return; }
  const run = ev.target.closest('[data-run]');
  if (run && !run.disabled) { runStep(run.dataset.run); return; }
  if (ev.target.closest('#askbtn')) { ask(); return; }
  const ex = ev.target.closest('[data-example]');
  if (ex) { $('#q').value = ex.dataset.example; $('#q').focus(); return; }
  const open = ev.target.closest('[data-open]');
  if (open && !ev.target.closest('button, a, input')) { open.classList.toggle('open'); return; }
  const save = ev.target.closest('[data-saveaccess]');
  if (save) {
    const name = save.dataset.saveaccess;
    const input = $$('[data-access]').find((i) => i.dataset.access === name);
    try {
      await api('/api/access', { json: { name, groups: input.value.split(',').map((x) => x.trim()).filter(Boolean) } });
      await loadState(); toast(`Saved. Run steps 1–4 so the index uses the new groups.`); render();
    } catch (e) { toast(e.message, true); }
    return;
  }
  if (ev.target.closest('#upload')) {
    const files = $('#files').files;
    if (!files.length) { toast('Choose one or more files first', true); return; }
    const fd = new FormData();
    [...files].forEach((f) => fd.append('files', f));
    fd.append('groups', $('#upgroups').value || 'public');
    try {
      const r = await api('/api/upload', { method: 'POST', body: fd });
      await loadState(); toast(`Uploaded ${r.saved.length} file(s). Now run steps 1–4.`); render();
    } catch (e) { toast(e.message, true); }
  }
});

document.addEventListener('keydown', (ev) => {
  if (ev.key === 'Enter' && ev.target && ev.target.id === 'q') ask();
});

(async function init() {
  try {
    await loadState();
    await loadHistory();
    render();
  } catch (e) {
    $('#main').innerHTML = `<div class="panel empty"><h3>Could not reach the server</h3><p>${esc(e.message)}</p></div>`;
  }
})();
