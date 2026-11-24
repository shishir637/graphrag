/* GraphRAG screens (steps 11-13). Loaded before app.js; app.js merges these into its STEPS,
   PHASES, explain(), VIEWS and AFTER tables. Everything here runs only after app.js has loaded. */

const GRAPH_STEPS = [
  { id: 'kg', n: 11, phase: 'graph', title: 'Knowledge graph', short: 'Entities, links, communities', file: 'graph' },
  { id: 'gask', n: 12, phase: 'graph', title: 'Graph answer', short: 'Graph and text, fused' },
  { id: 'geval', n: 13, phase: 'graph', title: 'Graph vs RAG', short: 'Does the graph help?', file: 'graph_eval' },
];
const GRAPH_PHASE = { graph: { label: 'GraphRAG', note: 'Optional layer on the same passages' } };
const TYPE_COLORS = {};
const typeColor = (t) => {
  if (!TYPE_COLORS[t]) TYPE_COLORS[t] = PALETTE[Object.keys(TYPE_COLORS).length % PALETTE.length];
  return TYPE_COLORS[t];
};

function graphExplain(id, c) {
  const E = {
    kg: {
      what: `${c.extract_mode === 'llm' ? `An LLM (${esc(c.graph_llm)}) reads every passage and lists` : `<b>GLiNER2</b>, a small model running on this machine, reads every passage and lists`} the <b>entities</b> in it (${(c.entity_types || []).length} allowed types) and the <b>relationships</b> between them${c.extract_mode === 'hybrid' ? `, each with a confidence score. Only the weakest passages (at most ${Math.round(c.escalate_budget * 100)}% of them) go to the LLM, which reviews and corrects GLiNER's draft` : ''}. <b>Entity resolution</b> then links names of the same thing ("BGE-M3" and "BGE-M3 model"), trying the cheapest check first: exact name, a number check, spelling and acronyms, name similarity, tight groups, and only then the LLM. Links can be reviewed and undone. <b>Leiden</b> groups tightly linked entities into communities, and the LLM writes a short report for each. The result is stored in <b>${c.graph_store === 'age' ? 'Apache AGE (Postgres)' : 'a JSON file'}</b>, and every entity and link keeps the ids of the passages it came from.`,
      why: 'The graph connects facts that sit in different passages, which plain search cannot do. But a graph is only as good as its extraction and resolution: a missed entity or a wrong merge means a wrong answer.',
      watch: ['Click a node to see its description, aliases and the passages it came from.', 'Check the resolution links: a wrong match (two different people joined) is worse than a missed one. Fix either kind in graph_resolution_overrides.json and rebuild from the resolve step.', 'Out-of-schema types and links to entities that were never extracted are dropped or fixed; the count is shown below.', 'Passages marked "reviewed by LLM" were escalated: check what the LLM dropped and added. If many weak passages stay with GLiNER, raise ESCALATE_BUDGET.', 'Re-runs only process new or changed passages.'],
      settings: [['Extraction', c.extract_mode === 'llm' ? `LLM (${c.graph_llm})` : c.extract_mode === 'gliner' || !S.state.graph_llm_available ? `GLiNER2 only (${c.gliner_model})` : `GLiNER2 + LLM review of up to ${Math.round(c.escalate_budget * 100)}%`], ['LLM', c.graph_llm], ['Store', c.graph_store === 'age' ? 'Apache AGE' : 'JSON + NetworkX'], ['Match by similarity at', c.resolve_match], ['Different below', c.resolve_no_match], ['LLM pair budget', c.resolve_llm_pairs], ['Communities', 'Leiden (modularity)']],
    },
    gask: {
      what: `The question's entities are found by name and by meaning, then their neighbourhood (${c.graph_hops} hop) is read from the graph. The passages behind those entities and links are ranked, then <b>fused with the normal hybrid search results</b> using the same RRF, re-ranked by the same re-ranker, and handed to the same answer step, plus a short "graph facts" passage. Overview questions ("main themes…") go to <b>global</b> search over the community reports instead.`,
      why: 'Hybrid can only add candidates: if the graph finds nothing useful, the answer is the same as plain RAG. Answering from summaries alone (local or global without the source passages) is what usually lowers accuracy on fact questions.',
      watch: ['Rows marked "graph" were found only through the graph. If they reach the final five, the graph changed the answer.', 'Try a question that needs two facts from different passages, then a plain fact question, and compare.', 'Ask as public and as hr: entities and summaries built from passages you cannot see are hidden too.'],
      settings: [['Hops', c.graph_hops], ['Graph passages into fusion', c.graph_chunks], ['Fusion', 'RRF, k=60'], ['Router', 'rule-based, no LLM call']],
    },
    geval: {
      what: 'The same questions are answered by plain RAG and by each graph mode, and scored by the same RAGAS judge, overall and <b>per question type</b> (single-hop, multi-hop, global, unanswerable). Questions come from <code>eval_questions_graph.json</code>: the RAG set plus tagged multi-hop and global questions.',
      why: 'An average hides the trade-off. GraphRAG usually helps multi-hop and overview questions and can hurt single-hop ones. The rule: hybrid may not score lower than plain RAG on any type.',
      watch: ['A drop on single-hop questions means the graph pushed good passages out: lower the graph\'s share or fix extraction.', 'No gain on multi-hop means entity resolution or linking misses: check the resolution links and the linked entities.', 'Runs every mode, so it makes many paid calls.'],
      settings: [['Judge', c.claude_model], ['Pass rule', 'hybrid ≥ RAG − 0.02, per type'], ['Modes', 'rag · hybrid · local']],
    },
  };
  return E[id];
}

/* --------------------------------------------------------------- force layout (no library) */

function forceLayout(nodes, edges, W, H) {
  let seed = 7;
  const rnd = () => { seed = (seed * 16807) % 2147483647; return seed / 2147483647; };
  const P = {};
  nodes.forEach((n) => { P[n.id] = { x: W / 2 + (rnd() - 0.5) * W * 0.8, y: H / 2 + (rnd() - 0.5) * H * 0.8, dx: 0, dy: 0 }; });
  const k = Math.sqrt((W * H) / Math.max(1, nodes.length)) * 0.75;
  let t = W / 8;
  for (let it = 0; it < 260; it++) {
    nodes.forEach((a) => { P[a.id].dx = 0; P[a.id].dy = 0; });
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = P[nodes[i].id], b = P[nodes[j].id];
        let dx = a.x - b.x, dy = a.y - b.y;
        const d = Math.max(0.01, Math.hypot(dx, dy));
        const f = (k * k) / d;
        dx = (dx / d) * f; dy = (dy / d) * f;
        a.dx += dx; a.dy += dy; b.dx -= dx; b.dy -= dy;
      }
    }
    edges.forEach((e) => {
      const a = P[e.source], b = P[e.target];
      if (!a || !b) return;
      const dx = a.x - b.x, dy = a.y - b.y;
      const d = Math.max(0.01, Math.hypot(dx, dy));
      const f = (d * d) / k;
      a.dx -= (dx / d) * f; a.dy -= (dy / d) * f; b.dx += (dx / d) * f; b.dy += (dy / d) * f;
    });
    nodes.forEach((n) => {
      const p = P[n.id];
      p.dx += (W / 2 - p.x) * 0.03; p.dy += (H / 2 - p.y) * 0.03;      // gravity keeps unconnected islands in view
      const d = Math.max(0.01, Math.hypot(p.dx, p.dy));
      p.x += (p.dx / d) * Math.min(d, t);
      p.y += (p.dy / d) * Math.min(d, t);
    });
    t *= 0.985;
  }
  // fit the drawing to the frame, leaving room for labels
  const xs = nodes.map((n) => P[n.id].x), ys = nodes.map((n) => P[n.id].y);
  const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  const padX = 90, padY = 40;
  nodes.forEach((n) => {
    const p = P[n.id];
    p.x = x1 > x0 ? padX + ((p.x - x0) / (x1 - x0)) * (W - 2 * padX) : W / 2;
    p.y = y1 > y0 ? padY + ((p.y - y0) / (y1 - y0)) * (H - 2 * padY) : H / 2;
  });
  return P;
}

function graphSvg(nodes, edges, { W = 900, H = 520, sel = null, highlight = null, id = 'kgsvg' } = {}) {
  if (!nodes.length) return '<p class="muted">No entities to draw.</p>';
  const P = forceLayout(nodes, edges, W, H);
  const deg = {};
  edges.forEach((e) => { deg[e.source] = (deg[e.source] || 0) + 1; deg[e.target] = (deg[e.target] || 0) + 1; });
  const near = new Set();
  if (sel) edges.forEach((e) => { if (e.source === sel) near.add(e.target); if (e.target === sel) near.add(e.source); });
  const labelled = new Set([...nodes].sort((a, b) => (deg[b.id] || 0) - (deg[a.id] || 0)).slice(0, 18).map((n) => n.id));
  let svg = '<g>';
  edges.forEach((e) => {
    const a = P[e.source], b = P[e.target];
    if (!a || !b) return;
    const on = sel && (e.source === sel || e.target === sel);
    const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
    svg += `<line style="pointer-events:none" x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}" stroke="${on ? 'var(--accent)' : 'var(--line)'}" stroke-width="${on ? 2.2 : 1 + Math.min(3, (e.weight || 1) * 0.6)}"><title>${esc(e.label || e.type)}</title></line>`;
    if (on) svg += `<text x="${mx}" y="${my - 3}" font-size="10" text-anchor="middle" fill="var(--accent-ink)" style="paint-order:stroke;stroke:var(--panel);stroke-width:3px">${esc(e.type)}</text>`;
  });
  svg += '</g><g>';
  nodes.forEach((n) => {
    const p = P[n.id];
    const r = 5 + Math.min(10, (deg[n.id] || 0) * 1.4);
    const dim = (sel && n.id !== sel && !near.has(n.id)) || (highlight && !highlight.has(n.id));
    const ring = n.id === sel || (highlight && highlight.has(n.id) && n.seed);
    svg += `<g class="gnode" data-node="${esc(n.id)}" tabindex="0" role="button" aria-label="${esc(n.name)}" style="cursor:pointer;opacity:${dim ? 0.25 : 1}">
      <circle cx="${p.x}" cy="${p.y}" r="${r}" fill="${n.color || typeColor(n.type)}" stroke="${ring ? 'var(--ink)' : 'var(--panel)'}" stroke-width="${ring ? 3 : 1.5}"><title>${esc(n.name)} (${esc(n.type)})</title></circle>
      ${labelled.has(n.id) || n.id === sel || near.has(n.id) || (highlight && highlight.has(n.id)) ? `<text x="${p.x}" y="${p.y - r - 4}" font-size="11" text-anchor="middle" fill="var(--ink)" style="paint-order:stroke;stroke:var(--panel);stroke-width:3px;pointer-events:none">${esc(n.name.length > 26 ? n.name.slice(0, 25) + '…' : n.name)}</text>` : ''}
    </g>`;
  });
  svg += '</g>';
  return `<svg id="${id}" viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;background:var(--soft);border-radius:10px" role="img" aria-label="Knowledge graph">${svg}</svg>`;
}

function typeLegend(types) {
  return `<div class="row" style="margin-top:8px">${types.map((t) => `<span class="chip"><span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:${typeColor(t)};margin-right:5px"></span>${esc(t)}</span>`).join('')}</div>`;
}

function stat(v, label) { return `<div class="stat"><b>${v}</b><span>${label}</span></div>`; }

/* --------------------------------------------------------------- views */

const GRAPH_VIEWS = {
  async kg() {
    const d = await step('graph');
    if (!d.exists) {
      return emptyStep('No knowledge graph yet', `Build it from the passages of steps 1–4. This calls the LLM for every passage (${esc(S.state.config.graph_llm)}).`, 'graph');
    }
    await labels();
    const ex = d.extraction.stats || {}, rs = d.resolution.stats || {}, cs = d.communities.stats || {}, ss = d.summaries.stats || {};
    const G = d.graph;
    const typeF = S.sel.kgType || '';
    const term = (S.sel.kgTerm || '').toLowerCase();
    let nodes = G.entities.filter((e) => (!typeF || e.type === typeF) && (!term || e.name.toLowerCase().includes(term) || e.aliases.some((a) => a.toLowerCase().includes(term))));
    let note = '';
    const deg = {};
    G.relations.forEach((r) => { deg[r.source] = (deg[r.source] || 0) + 1; deg[r.target] = (deg[r.target] || 0) + 1; });
    if (nodes.length > 220) { nodes = nodes.sort((a, b) => (deg[b.id] || 0) - (deg[a.id] || 0)).slice(0, 220); note = 'Showing the 220 most connected entities. Filter by type or name to see others.'; }
    const ids = new Set(nodes.map((n) => n.id));
    const edges = G.relations.filter((r) => ids.has(r.source) && ids.has(r.target));
    const types = [...new Set(G.entities.map((e) => e.type))].sort();
    const sel = S.sel.kgNode && ids.has(S.sel.kgNode) ? S.sel.kgNode : null;

    const TIER = { override: 'your override', name_gate: 'number check', fuzzy: 'spelling / acronym', embedding: 'name similarity', cluster: 'tight group', llm: 'LLM' };
    const DEC = { match: 'ok', no_match: '', undecided: 'warn' };
    const links = d.resolution.links.slice().sort((x, y) => ({ match: 0, undecided: 1, no_match: 2 }[x.decision] - { match: 0, undecided: 1, no_match: 2 }[y.decision]));
    const decisions = links.map((x) => `<tr><td class="small">${esc(x.a)}</td><td class="small">${esc(x.b)}</td><td class="tiny muted">${esc(x.type)}</td>
      <td><span class="chip ${DEC[x.decision]}">${esc(x.decision.replace('_', ' '))}</span>${x.applied === false ? ' <span class="chip warn">not applied</span>' : ''}</td>
      <td class="small">${esc(TIER[x.tier] || x.tier)}</td>
      <td class="tiny muted">${esc(x.reason)}${x.blocked_by ? `<div style="color:var(--warn)">blocked: ${esc(x.blocked_by)}</div>` : ''}</td>
      <td class="tiny muted" style="white-space:nowrap">name ${fmt(x.name_sim)} · vector ${fmt(x.embedding_sim)}</td></tr>`).join('');
    const tiers = Object.entries(rs.by_tier || {}).map(([k, v]) => { const [t, dd] = k.split(':'); return `<span class="chip"><b>${esc(TIER[t] || t)}</b>${esc(dd.replace('_', ' '))} ${v}</span>`; }).join('');
    const EXT = { gliner: ['GLiNER2', ''], 'gliner+llm': ['reviewed by LLM', 'acc'], llm: ['LLM', ''] };
    const examples = d.extraction.examples.map((c) => {
      const L = chunkLabels[c.chunk_id];
      const [extLabel, extCls] = EXT[c.extractor] || [c.extractor, ''];
      const esc8 = c.escalation;
      return `<div class="hit gx" data-open style="cursor:pointer;padding:8px 10px">
        <div class="small">${passageMeta({ chunk_id: c.chunk_id, source: c.source })} · <span class="chip ${extCls}">${esc(extLabel)}</span> <b>${c.entities.length}</b> entities · <b>${c.relations.length}</b> links${c.problems.length ? ` · <span class="chip warn">${c.problems.length} dropped or fixed</span>` : ''}
          ${esc8 && !esc8.escalated ? `<span class="tiny muted"> · weak (${esc(esc8.reason)}), over the LLM budget</span>` : ''}</div>
        <div class="more small" style="margin-top:6px">
          ${esc8 ? `<div class="tiny" style="margin-bottom:4px"><b>Escalation:</b> ${esc(esc8.reason)} · rank ${esc8.rank} · ${esc8.escalated ? 'sent to the LLM' : 'not sent (budget)'}</div>` : ''}
          ${L ? `<div class="tiny muted" style="margin-bottom:6px">${esc(L.c.text.slice(0, 400))}${L.c.text.length > 400 ? '…' : ''}</div>` : ''}
          <div class="row">${c.entities.map((e) => `<span class="chip" title="${esc(e.description)}"><span style="color:${typeColor(e.type)};font-weight:700">${esc(e.type)}</span>&nbsp;${esc(e.name)}${e.confidence != null ? `&nbsp;<span class="tiny muted">${fmt(e.confidence)}</span>` : ''}${e.from === 'llm' && c.extractor !== 'llm' ? '&nbsp;<span class="tiny" style="color:var(--ok)">+LLM</span>' : ''}</span>`).join('') || '<span class="tiny muted">no entities</span>'}</div>
          ${c.relations.map((r) => `<div class="tiny" style="margin-top:3px">${esc(r.source)} <b style="color:var(--accent-ink)">-${esc(r.type)}→</b> ${esc(r.target)} <span class="muted">(${r.confidence != null ? fmt(r.confidence) : r.strength + '/10'}) ${esc(r.description)}</span></div>`).join('')}
          ${c.problems.map((p) => `<div class="tiny" style="color:var(--warn)">${esc(p)}</div>`).join('')}
        </div></div>`;
    }).join('');
    const reports = d.summaries.reports.slice().sort((a, b) => a.level - b.level || b.importance - a.importance).map((r) => `<div class="panel" style="margin:10px 0 0;box-shadow:none">
      <div class="head"><h3 style="font-size:15px">${esc(r.title)}</h3><div class="row"><span class="chip"><b>level</b>${r.level}</span><span class="chip"><b>entities</b>${r.size}</span><span class="chip acc"><b>importance</b>${r.importance}/10</span>${r.access_groups.map((g) => `<span class="chip ${g === 'public' ? '' : 'warn'}">${esc(g)}</span>`).join('')}</div></div>
      <p class="small" style="margin:6px 0">${esc(r.summary)}</p>
      <ul class="small" style="margin:0;padding-left:18px">${r.findings.map((f) => `<li><b>${esc(f.summary)}</b> <span class="muted">${esc(f.explanation)}</span></li>`).join('')}</ul></div>`).join('');

    return `<div class="panel"><h3>What the build produced</h3><div class="stats" style="margin-top:10px">
        ${stat(fmt(ex.chunks, 0), `passages read (${fmt(ex.reused_from_cache, 0)} reused from cache)`)}
        ${ex.escalation ? stat(`${fmt(ex.escalation.escalated, 0)} / ${fmt(ex.escalation.eligible, 0)}`, `weak passages reviewed by the LLM / weak passages (budget ${Math.round(ex.escalation.budget * 100)}% of all)`) : ''}
        ${stat(`${fmt(ex.entity_mentions, 0)} → ${fmt(rs.entities, 0)}`, 'entity mentions → entities after resolution')}
        ${stat(fmt(rs.relations, 0), 'relationships (duplicates merged)')}
        ${stat(fmt(cs.communities, 0), `communities in ${fmt(cs.levels, 0)} level(s), modularity ${fmt(cs.modularity_level0)}`)}
        ${stat(fmt(ex.dropped_or_fixed, 0), 'items dropped or fixed (low confidence, outside the schema, or removed on LLM review)')}
        ${stat(fmt((ex.llm_calls || 0) + (rs.llm_pairs || 0) + (ss.entity_summaries_by_llm || 0) + (ss.community_reports || 0), 0), 'LLM calls in the last build')}
      </div><p class="tiny muted" style="margin:10px 0 0">Stored in ${esc(d.stored_in)}: ${Object.entries(d.counts).map(([k, v]) => `${esc(k)} ${fmt(v, 0)}`).join(' · ')}</p></div>

      <div class="panel"><div class="head"><h3>The graph</h3>
        <div class="row"><select id="kgtype" aria-label="Entity type"><option value="">All types</option>${types.map((t) => `<option ${t === typeF ? 'selected' : ''}>${esc(t)}</option>`).join('')}</select>
        <input type="text" id="kgterm" placeholder="Find an entity…" value="${esc(S.sel.kgTerm || '')}" style="width:180px" aria-label="Find an entity"></div></div>
        ${graphSvg(nodes, edges, { sel })}
        ${typeLegend(types)}
        ${note ? `<p class="tiny muted">${note}</p>` : ''}
        <div id="kgdetail" style="margin-top:12px">${sel ? entityDetail(G, sel) : '<p class="small muted">Click a node to see what the graph knows about it and where it came from.</p>'}</div></div>

      <div class="panel"><h3>1 · Extraction, passage by passage</h3><p class="small muted">Click a passage to see what was pulled out of it, with GLiNER2's confidence per entity. <span style="color:var(--ok)">+LLM</span> marks entities the LLM added on review. Hover an entity for its description.</p>${examples || '<p class="muted">Nothing extracted.</p>'}</div>

      <div class="panel"><h3>2 · Entity resolution links</h3>
        <p class="small muted">${fmt(rs.surface_entities, 0)} distinct names (exact duplicates already collapsed) → ${fmt(rs.entities, 0)} entities. ${fmt(rs.pairs_compared, 0)} candidate pairs, each decided by the first check that was sure. ${fmt(rs.llm_pairs, 0)} pair(s) went to the LLM; ${fmt(rs.undecided, 0)} stay undecided (not the same as different)${rs.matches_blocked_by_no_match ? `; ${rs.matches_blocked_by_no_match} match(es) not applied because they would join two names already judged different` : ''}. Clearly different pairs are not listed.</p>
        <div class="row" style="margin:8px 0">${tiers}</div>
        ${decisions ? `<div class="scroll"><table class="t"><thead><tr><th>Name A</th><th>Name B</th><th>Type</th><th>Decision</th><th>Decided by</th><th>Why</th><th>Similarity</th></tr></thead><tbody>${decisions}</tbody></table></div>` : '<p class="muted small">No pairs needed a decision.</p>'}
        <p class="tiny muted" style="margin:8px 0 0">To correct a link, add the pair to <code>graph_resolution_overrides.json</code> under "match" or "no_match", then run <code>python rag.py graph-build --steps resolve,communities,summarize,store</code> (no extraction calls).</p></div>

      <div class="panel"><h3>3 · Community reports</h3><p class="small muted">${fmt(ss.community_reports, 0)} reports. Entity descriptions: ${fmt(ss.entity_summaries_copied, 0)} copied as-is (seen in one passage), ${fmt(ss.entity_summaries_by_llm, 0)} merged by the LLM. A report is only shown to users who can see every passage it was written from.</p>${reports || '<p class="muted small">No community has two or more entities yet.</p>'}</div>`;
  },

  async gask() {
    const t = S.gtrace;
    if (!t) return `<div class="panel empty"><h3>Ask a question through the graph</h3><p>Use the box above. Every mode works without an LLM: you then get the passages and graph facts instead of a written answer, and global search ranks the reports by similarity.</p></div>`;
    await labels();
    const tr = t.graph_trace;
    let html = `<div class="panel"><div class="head"><h3>Route: <span class="chip acc">${esc(t.mode)}</span></h3><span class="small muted">total ${secs(t.timings_s.total)}</span></div>
      ${tr.note ? `<p class="small" style="color:var(--warn)">${esc(tr.note)}</p>` : ''}
      <div class="row">${Object.entries(t.timings_s).map(([k, v]) => `<span class="chip"><b>${esc(k.replace(/_/g, ' '))}</b>${secs(v)}</span>`).join('')}</div></div>`;

    if (tr.local) {
      const L = tr.local;
      const nb = L.neighbourhood;
      const seeds = new Set(L.linked_entities.map((e) => e.id));
      const nodes = nb.entities.map((e) => ({ ...e, seed: seeds.has(e.id) }));
      html += `<div class="panel"><h3>A · Entities in the question</h3>
        ${L.linked_entities.length ? `<div class="row">${L.linked_entities.map((e) => `<span class="chip"><span style="color:${typeColor(e.type)};font-weight:700">${esc(e.type)}</span>&nbsp;<b>${esc(e.name)}</b>&nbsp;<span class="tiny muted">${esc(e.how)}${e.how === 'embedding' ? ' ' + fmt(e.score) : ''}</span></span>`).join('')}</div>` : '<p class="small muted">No entity in the graph matched the question, so the graph adds nothing and hybrid falls back to plain search.</p>'}</div>
        <div class="panel"><h3>B · Their neighbourhood in the graph</h3><p class="small muted">${nb.entities.length} entities and ${nb.relations.length} relationships reached in ${S.state.config.graph_hops} hop(s). Outlined nodes are the question's entities.</p>
        ${graphSvg(nodes, nb.relations, { W: 760, H: 360, highlight: new Set(nodes.map((n) => n.id)), id: 'gsvg' })}</div>
        <div class="panel"><h3>C · Passages the graph points to</h3><p class="small muted">Scored by where the question's entities, their neighbours and the relationships between them were stated.</p>
        ${L.chunk_ranking.length ? L.chunk_ranking.map((c, i) => `<div class="hit" style="display:grid;grid-template-columns:28px 1fr 120px;gap:8px;align-items:center"><span class="muted">${i + 1}</span><span class="small">${chunkLabels[c.chunk_id] ? passageMeta(chunkLabels[c.chunk_id].c) : esc(c.chunk_id)}</span>${bar(c.graph_score, L.chunk_ranking[0].graph_score)}</div>`).join('') : '<p class="small muted">None.</p>'}</div>`;
    }
    if (tr.fusion) {
      const mark = { both: 'acc', graph: 'ok', text: '' };
      html += `<div class="panel"><h3>D · Fusion with the normal hybrid search</h3><p class="small muted">Graph passages and the dense + BM25 candidates merged by RRF, then re-ranked. <b>graph</b> = found only through the graph.</p>
        <div class="scroll"><table class="t"><thead><tr><th>#</th><th>Passage</th><th>Found by</th><th>RRF</th></tr></thead><tbody>
        ${tr.fusion.map((f, i) => `<tr><td class="muted">${i + 1}</td><td class="small">${chunkLabels[f.chunk_id] ? passageMeta(chunkLabels[f.chunk_id].c) : esc(f.chunk_id)}</td><td><span class="chip ${mark[f.from]}">${f.from}</span></td><td class="muted small">${fmt(f.rrf, 4)}</td></tr>`).join('')}
        </tbody></table></div>
        <p class="small" style="margin:10px 0 0">${tr.graph_only_in_final.length ? `<b>${tr.graph_only_in_final.length}</b> passage(s) found only through the graph reached the final set.` : 'No graph-only passage reached the final set: the answer uses the same passages plain RAG would, plus the graph facts.'}</p></div>`;
    }
    if (tr.global) {
      const G = tr.global;
      html += `<div class="panel"><h3>Global search over community reports</h3><p class="small muted">${G.reports_considered} reports read${G.reports_hidden_by_access ? `, ${G.reports_hidden_by_access} hidden because they were written from passages this user cannot see` : ''}. ${G.map_step === 'llm' ? 'The LLM picked these points (map step); the best go to the answer (reduce step).' : 'No LLM: the reports most similar to the question were picked, with their findings.'}</p>
        ${G.points.map((p) => `<div class="hit" style="display:grid;grid-template-columns:70px 1fr 100px;gap:8px;align-items:center"><span class="chip">${esc(p.community)}</span><span class="small">${esc(p.point)}</span>${bar(p.score, 100)}</div>`).join('') || '<p class="small muted">No relevant points.</p>'}</div>`;
    }
    html += `<div class="panel"><h3>E · What the answer step receives</h3>
      ${t.contexts.map((c) => `<div class="hit gx ${t.citations && t.citations.includes(c.chunk_id) ? 'cited' : ''}" data-open style="cursor:pointer;padding:8px 10px">
        <div class="small">${c.chunk_id === 'kg_facts' ? '<span class="chip acc">graph facts</span>' : c.chunk_id.startsWith('report_') ? `<span class="chip acc">community report</span> ${esc(c.chunk_id.slice(7))}` : (chunkLabels[c.chunk_id] ? passageMeta(chunkLabels[c.chunk_id].c) : esc(c.chunk_id))}
        ${c.rerank_score != null ? `<span class="tiny muted"> · re-rank ${fmt(c.rerank_score)}</span>` : ''}${t.citations && t.citations.includes(c.chunk_id) ? ' <span class="chip ok">cited</span>' : ''}</div>
        <div class="more"><pre class="small" style="white-space:pre-wrap;margin:6px 0 0">${esc(c.text)}</pre></div></div>`).join('')}</div>`;
    html += t.answer != null
      ? `<div class="panel"><h3>Answer ${t.found ? '<span class="chip ok">found</span>' : '<span class="chip warn">not found</span>'}</h3><p>${esc(t.answer)}</p>
         <p class="tiny muted">Graph LLM calls for this question: ${t.graph_llm.calls}. Answer model: ${esc(t.model || '')}</p></div>`
      : `<div class="panel"><p class="small muted" style="margin:0">${S.state.can_answer ? 'Retrieval only: tick "Write the answer" to send these passages to the model.' : 'Retrieval only: no LLM is configured, so these passages are the result. Add ANTHROPIC_API_KEY, or GRAPH_LLM=openai for a local model, to get a written answer.'}</p></div>`;
    return html;
  },

  async geval() {
    const d = await step('graph_eval');
    if (!d.exists) return emptyStep('Not compared yet', 'Run the comparison to score plain RAG against the graph modes on the same questions. It makes many paid Claude calls.', S.state.has_api_key ? 'graph_eval' : null);
    const v = d.verdict;
    const modes = Object.keys(d.summary);
    const metrics = Object.keys(d.summary[modes[0]].overall);
    const types = Object.keys(d.summary[modes[0]].by_type);
    const names = { context_recall: 'Context recall', llm_context_precision_with_reference: 'Context precision', faithfulness: 'Faithfulness', answer_relevancy: 'Answer relevancy' };
    const cell = (val, base) => {
      if (val == null) return '<td class="muted">—</td>';
      const diff = base == null ? 0 : val - base;
      const c = diff < -0.02 ? 'var(--bad)' : diff > 0.02 ? 'var(--ok)' : 'var(--ink)';
      return `<td style="color:${c};font-weight:600">${fmt(val)}${base != null && Math.abs(diff) > 0.005 ? `<span class="tiny"> (${diff > 0 ? '+' : ''}${fmt(diff)})</span>` : ''}</td>`;
    };
    const tables = metrics.map((m) => `<h4 style="margin:14px 0 6px">${esc(names[m] || m)}</h4><div class="scroll"><table class="t"><thead><tr><th>Mode</th><th>All</th>${types.map((t) => `<th>${esc(t)}</th>`).join('')}</tr></thead><tbody>
      ${modes.map((mo) => `<tr><td><b>${esc(mo)}</b></td>${cell(d.summary[mo].overall[m], mo === 'rag' ? null : d.summary.rag && d.summary.rag.overall[m])}${types.map((t) => cell(d.summary[mo].by_type[t][m], mo === 'rag' ? null : d.summary.rag && d.summary.rag.by_type[t][m])).join('')}</tr>`).join('')}
      </tbody></table></div>`).join('');
    return `<div class="panel" style="border-color:${v && v.passed ? 'var(--ok)' : 'var(--bad)'}"><h3>${v ? (v.passed ? 'Passed: hybrid does not lose to plain RAG on any question type' : `Failed: ${v.regressions.length} regression(s)`) : 'No verdict (rag or hybrid missing)'}</h3>
      ${v && v.regressions.length ? `<ul class="small">${v.regressions.map((r) => `<li><b>${esc(r.type)}</b> · ${esc(names[r.metric] || r.metric)}: ${fmt(r.rag)} → ${fmt(r.hybrid)} (${fmt(r.delta)})</li>`).join('')}</ul>` : ''}
      <p class="tiny muted" style="margin:6px 0 0">${esc(v ? v.rule : '')} · questions from ${esc(d.questions_file)}</p></div>
      <div class="panel"><h3>Scores by mode and question type</h3><p class="small muted">Differences are against plain RAG: green is better by more than 0.02, red is worse.</p>${tables}
      <div class="row" style="margin-top:12px">${modes.map((mo) => `<span class="chip"><b>${esc(mo)}</b>answered ${fmt(d.summary[mo].answered_rate * 100, 0)}% · ${secs(d.summary[mo].latency_mean_s)} avg</span>`).join('')}</div></div>`;
  },
};

function entityDetail(G, id) {
  const e = G.entities.find((x) => x.id === id);
  if (!e) return '';
  const name = Object.fromEntries(G.entities.map((x) => [x.id, x.name]));
  const rels = G.relations.filter((r) => r.source === id || r.target === id);
  const comm = G.communities.find((c) => c.id === e.community);
  return `<div class="box" style="padding:12px 14px;border:1px solid var(--line);border-radius:10px">
    <div class="row"><b style="font-size:15px">${esc(e.name)}</b><span class="chip"><span style="color:${typeColor(e.type)};font-weight:700">${esc(e.type)}</span></span>
      ${comm ? `<span class="chip"><b>community</b>${esc(comm.title || comm.id)}</span>` : ''}${e.aliases.length ? `<span class="chip"><b>also called</b>${esc(e.aliases.join(', '))}</span>` : ''}</div>
    <p class="small" style="margin:8px 0">${esc(e.summary)}</p>
    <div class="small"><b>Relationships</b>${rels.length ? rels.map((r) => `<div class="tiny" style="margin-top:3px">${r.source === id ? '' : `<a href="#" data-node-link="${esc(r.source)}">${esc(name[r.source])}</a> `}<b style="color:var(--accent-ink)">-${esc(r.type)}→</b>${r.target === id ? '' : ` <a href="#" data-node-link="${esc(r.target)}">${esc(name[r.target])}</a>`} <span class="muted">${esc(r.description)} (seen ${r.weight}×)</span></div>`).join('') : ' <span class="muted">none</span>'}</div>
    <div class="small" style="margin-top:8px"><b>From passages</b><div class="row" style="margin-top:4px">${e.chunk_ids.map((c) => `<span class="chip">${chunkLabels[c] ? passageMeta(chunkLabels[c].c) : esc(c)}</span>`).join('')}</div></div></div>`;
}

const GRAPH_AFTER = {
  kg() {
    const pick = (id) => { S.sel.kgNode = S.sel.kgNode === id ? null : id; render(); };
    $$('#kgsvg [data-node]').forEach((n) => {
      n.addEventListener('click', () => pick(n.dataset.node));
      n.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); pick(n.dataset.node); } });
    });
    $$('[data-node-link]').forEach((a) => a.addEventListener('click', (ev) => { ev.preventDefault(); S.sel.kgNode = a.dataset.nodeLink; S.sel.kgTerm = ''; S.sel.kgType = ''; render(); }));
    const ty = $('#kgtype');
    if (ty) ty.addEventListener('change', () => { S.sel.kgType = ty.value; S.sel.kgNode = null; render(); });
    const tm = $('#kgterm');
    if (tm) tm.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') { S.sel.kgTerm = tm.value.trim(); S.sel.kgNode = null; render(); } });
  },
};

function graphAskBar() {
  const st = S.state;
  const f = S.gform || { q: '', mode: 'auto', groups: ['public'], generate: st.can_answer };
  const ready = st.outputs.graph && st.outputs.graph.exists;
  const modes = [['auto', 'Auto'], ['hybrid', 'Hybrid (graph + text)'], ['local', 'Local (graph only)'], ['global', 'Global (reports)']];
  return `<div class="askbar">
    <div class="q"><input type="text" id="gq" placeholder="Ask a question through the knowledge graph…" value="${esc(f.q)}" aria-label="Question">
      <button class="btn primary" id="gaskbtn" ${ready ? '' : 'disabled title="Build the knowledge graph first"'}>Ask</button></div>
    <div class="opts">
      <div><span class="lbl">Mode</span><div class="row gmode">${modes.map(([v, l]) => `<label class="check"><input type="radio" name="gmode" value="${v}" ${f.mode === v ? 'checked' : ''}>${l}</label>`).join('')}</div></div>
      <div><span class="lbl">Ask as someone in these groups</span><div class="row ggrp">${st.groups.map((g) => `<label class="check"><input type="checkbox" value="${esc(g)}" ${f.groups.includes(g) ? 'checked' : ''}>${esc(g)}</label>`).join('')}</div></div>
      <div><span class="lbl">Answer</span><label class="check"><input type="checkbox" id="ggen" ${f.generate && st.can_answer ? 'checked' : ''} ${st.can_answer ? '' : 'disabled title="No LLM configured: retrieval only"'}>Write the answer${st.can_answer ? ` (${esc(st.answer_model)})` : ''}</label></div>
    </div>
    ${st.graph_examples && st.graph_examples.length ? `<div class="examples"><span class="tiny muted" style="align-self:center">Try:</span>${st.graph_examples.map((x) => `<button data-gexample="${esc(x)}">${esc(x)}</button>`).join('')}</div>` : ''}
    ${ready ? '' : '<p class="small muted" style="margin:10px 0 0">The knowledge graph is not built yet (step 11).</p>'}
  </div>`;
}

async function graphAsk() {
  const q = ($('#gq') || {}).value?.trim();
  if (!q) { toast('Type a question first', true); return; }
  const mode = ($('input[name=gmode]:checked') || {}).value || 'auto';
  const groups = $$('.ggrp input:checked').map((i) => i.value);
  const generate = !!($('#ggen') && $('#ggen').checked);
  S.gform = { q, mode, groups, generate };
  try {
    S.gtrace = await runJob(api('/api/graph/ask', { json: { question: q, mode, groups, generate } }), 'Answering through the graph');
    render();
  } catch (e) { toast(e.message, true); }
}

document.addEventListener('click', (ev) => {
  if (ev.target.closest('#gaskbtn')) { graphAsk(); return; }
  const ex = ev.target.closest('[data-gexample]');
  if (ex) { $('#gq').value = ex.dataset.gexample; $('#gq').focus(); }
});
document.addEventListener('keydown', (ev) => {
  if (ev.key === 'Enter' && ev.target && ev.target.id === 'gq') graphAsk();
});
