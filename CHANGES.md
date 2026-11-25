# Changes

## 0.4.0 (25 Nov 2026): optional AI extraction for the knowledge graph

A third graph quality, **ai**, next to fast (spaCy) and accurate (GLiNER + spaCy). An AI model reads each
passage and returns entities with **named links** (leads, located in, owns...) instead of "named in the same
sentence". Kanakavalli's test on the 30 hand-labelled passages: link recall 0.11 for GLiNER vs 0.75 for Claude Haiku.

- Off by default. Used only while building the graph, never for answering; answering still runs no AI model.
- Providers (Settings → AI extraction): **Claude** (own API key), any **OpenAI-compatible** API (own key), or a
  **local model** such as Ollama (no key; passages stay on the company network). Plain HTTPS calls: no AI SDK
  added to the requirements.
- Many passages at a time (8 by default for cloud providers, 2 for local), retries with back-off, and a cache per
  passage + model, so rebuilds only pay for new or changed passages.
- A passage whose call fails falls back to spaCy, so one bad call never stops a build; if every call fails the
  build stops with the error. Settings shows tokens used in the last build. "Test connection" checks key and model.
- The key is stored in the server's secrets file, shown masked, and never written to the activity log.
  Changes to the provider are audited.
- The build log says where passages are sent. Settings warns when the provider is outside the network.
- Docker: `host.docker.internal` reaches a model running on the same machine (also on Linux).
- `python -m experiments.extract_compare` compares fast, accurate and ai on the hand-labelled passages: entity and
  link accuracy and real build time per passage.

## 0.3.0 (25 Nov 2026): use it from Claude or ChatGPT, add files from the chat

People use Context Layer inside their own Claude or ChatGPT: the AI comes from their plan, Context Layer
supplies the governed context.

**Sign in from the AI app** (`app/oauth.py`)
- Add Context Layer as a custom connector with only its MCP link; a Context Layer sign-in page opens, then
  "Allow <app>?". OAuth 2.1 as the MCP specification asks: protected-resource and server metadata, PKCE (S256),
  dynamic client registration and client ID metadata documents, `iss` in the redirect, refresh tokens rotated
  (a reused one signs the app out), tokens bound to the address they were issued for.
- Signed-in people act as themselves: viewers see their access groups (set on the Access page), owners and
  admins see every group and may add files. Disabling a person signs out their apps. Agents → Signed-in apps
  lists connected apps with Disconnect.
- Agent keys keep working unchanged.

**Add files from the chat**
- `add_document`: a file attached in ChatGPT (passed straight to the tool), the text of a file (Claude), or
  a small file as base64. CSV, Excel and Parquet become data tables with draft metrics; everything else
  becomes searchable documents and knowledge-graph facts. Shared only with the uploader's own groups.
- `create_upload_link`: a one-time page (30 minutes) for large or binary files.
- `build_status`: queued / running / done, then what the files became (passages, graph size, draft metrics).
- `approve_metric`: owners and admins approve a draft metric from the chat.
- The knowledge graph forgets passages from deleted or edited documents (before, their old entities stayed).
- Unchanged documents are not parsed again on each rebuild.
- Uploads queue one background rebuild (documents, then the graph if it is in use; data tables) instead of
  failing while another job runs.


## 0.2.0 (25 Nov 2026): no AI model inside

Context Layer no longer calls Claude (or any language model). It is the context layer only: it finds the
evidence and serves it over MCP; the agent that connects (Claude, ChatGPT, Cursor, Copilot, a company bot)
writes the answer. There is no API key to set.

**Answering**
- `ask` returns evidence: passages with highlighted sentences, graph links with the sentence behind each,
  or metric rows with the SQL. A rule-based router (`app/router.py`) picks documents, metrics, connections
  or overview; callers can force a mode.
- Retrieval adds knowledge-graph candidates before re-ranking and keeps one passage per entity the
  question names (comparisons, multi-part questions).
- New MCP tools: `describe_data` (metrics, dimensions with values, synonyms, business rules, examples),
  `find_connections`, `explore_entity`. `query_metrics` now takes metric and dimension names filled in by
  the agent (or a plain question).

**Metrics**
- A rule-based planner (`semantic/rule_planner.py`) turns plain questions into metric requests: typo- and
  plural-tolerant matching, join-aware column choice, fan-out swaps, ranking, dates, refusals.
- Tables can be CSV, Excel (every sheet), JSON (nested fields flattened), JSON Lines or Parquet
  (`semantic/ingest.py`).
- New tables get draft definitions from the data profile (`semantic/auto_model.py`): entities, joins,
  count/total/average metrics and synonyms. Agents see only approved metrics; admins approve on the Metrics page.

**Knowledge graph**
- Built without an LLM (`graph/nlp_extract.py`): GLiNER + spaCy entities (accurate) or spaCy only (fast);
  links from co-occurrence in a sentence and from section topics (the approach of Microsoft's LazyGraphRAG);
  optional GLiNER2 typed relations. Entity merging is rule-based.
- Answering reads the graph build file (`graph/search.py`), so it works when the graph database is down;
  Apache AGE keeps a copy for Cypher.

**Documents**
- JSON / JSON Lines documents are searchable (one block per record); CSV documents go through Docling.
- Embedding is incremental: unchanged passages keep their vectors.

**Quality page**: free, deterministic benchmarks (no judge model): retrieval recall/MRR/nDCG on gold
passages, metric execution accuracy on gold requests, entity and link extraction on hand-labelled passages.

**Removed from the product path**: `anthropic`, RAGAS and the LangChain LLM wrappers (now in
`requirements-research.txt` for comparisons only). Settings shows the MCP link instead of an API key.
