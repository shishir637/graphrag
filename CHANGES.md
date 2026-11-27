# Changes

## 0.7.0: target architecture: landing zone, system of record, rebuildable engines

Every piece of data has one home: raw files in the **landing zone** (`data/` tables as Parquet, `documents/`
originals), everything the system knows and decides in **Postgres**, and **derived engines** (DuckDB views, fact
tables, the in-memory graph, the Qdrant index) that can be dropped and rebuilt from the first two. Answering still
runs no AI model. Existing workspaces are upgraded automatically at start (`app/upgrade.py`, idempotent).

**Landing zone and manifest** (`app/storage.py`, `app/landing.py`, migration `011_landing.sql`)
- All reads and writes of table files and original documents go through one storage interface (`Store`, with
  `LocalStore` over the existing workspace folders and Docker volumes). An S3-compatible store can be added later
  without touching callers (`CL_STORE`; only `local` exists).
- Tables are Parquet (zstd) only. CSV uploads are converted with the column names and types DuckDB inferred from
  them before, so metric definitions keep working; Excel, JSON and JSON Lines keep their cleaning rules.
- Every landing writes a row in `landing_files`: kind, name, path, source (web, upload link, MCP, connector,
  sample, backfill), connector, who pushed it, groups, sha256, bytes, rows and schema (tables). Replaced and removed
  files keep their rows (`replaced_at`). Landing an identical file changes nothing and starts no rebuild.
- The Postgres connector reads with a server-side cursor and writes Parquet in batches (pyarrow, zstd), so big
  tables never have to fit in memory; incremental merges by primary key run in DuckDB on the Parquet files. The
  2,000,000-row cap is gone (`max_rows` is optional; `batch_rows` sets the batch size). Connectors can hand over
  Parquet bytes or a Parquet file (`Table.parquet`, `Table.parquet_path`); CSV still works.
- Upgrade: CSV tables in each workspace become Parquet (originals kept in `data/_uploads/`), and every file already
  there gets a manifest row.

**DuckDB views instead of copies** (`semantic/db.py`)
- The workspace's DuckDB holds only views over the Parquet files (live database sources were views already):
  building it takes milliseconds and no memory, and it can never disagree with the files. Row access rules still
  shadow the views per caller.
- Each DuckDB has a memory limit (`CL_DUCKDB_MEMORY`, default 1GB) and spills to `<workspace>/duckdb_tmp`.
- Profiling and every metric give the same results as the old in-memory copies (tested on the shipped tables and
  the Lumora test set). Benchmark, 3M-row table (`experiments/bench_duckdb_views.py`): build 106 ms -> 1.7 ms,
  memory after build 126 MiB -> 0 MiB, a group-by query 72 ms -> 96 ms.

**Passages in Postgres** (`rag/passages.py`, migration `012_passages.sql`)
- The chunking step writes the passages to the `passages` table (text, headings, pages, access, text hash,
  position) instead of `outputs/2_chunks.json`; unchanged passages are not touched, and a version
  (`passages_state`) moves only when something changed, so caches reload exactly when needed.
- Every reader goes through the repository: search, embedding, indexing, neighbours, the "answerable?" check, the
  graph (access, search, build, extraction), facts and matching, `fetch_passage`. The command line keeps the file.
- The search index keeps only ids and filter fields (chunk id, workspace, access, source, headings): retrieval reads
  the candidates' texts from Postgres in one query. Measured on 20,000 passages, top 50
  (`experiments/bench_passage_texts.py`): 28.0 ms with the text in the payload, 18.5 ms with ids + Postgres.
  Indexes built before 0.7 still hold the text; it is simply not read, and points are rewritten as passages change.
- Upgrade: each workspace's `2_chunks.json` moves into the table (kept as `2_chunks.json.pre-0.7`).

**One graph store** (`graph/kg.py`, migration `013_graph.sql`)
- The graph build writes plain Postgres tables in one transaction and moves `graph_state.version`:
  `kg_entities`, `kg_relations` (with the passages behind each link), `kg_entity_passages`, `kg_communities`.
  Every field of the build survives the round trip (per-passage evidence included), and `_graph_build.json` is no
  longer written inside a workspace.
- Search loads the tables into an in-memory cache (a NetworkX graph plus name and passage indexes) per workspace and
  version, and reloads when a build moves the version. Every access check is unchanged: a link or entity counts only
  when a visible passage supports it. The graph explorer reads the same tables.
- Apache AGE is out of the product path: `graph/store.py`, `graph/query.py` and `graph/evaluate.py` (the Cypher copy
  and the LLM GraphRAG research CLI) moved to `experiments/graph_age/`; nothing in the product imports them.
- Upgrade: each workspace's `_graph_build.json` moves into the tables (kept as `_graph_build.json.pre-0.7`).

**Metric catalog in memory** (`semantic/enrich.py`)
- The catalog the planner and agents use (synonyms, known values, instructions) is built in memory from the
  semantic layer in Postgres and cached by the layer's state version, which every definition or value-list change
  moves; on the command line it is built from the YAML files and tables, cached by their modification times.
- Nothing is written to `sl_store/enrichment.duckdb` any more (inside a workspace it was already unused since 0.5);
  the plan cache moved to `outputs/sl_plan_cache.json`. The `metric_store` Docker volume is gone (an existing one is
  simply left unused; `docker volume rm <project>_metric_store` removes it).

## 0.6.0: accuracy: facts, checks, an inbox, "answerable?" and a grounding check

No AI model is added: everything below is rules, regular expressions, RapidFuzz and the local models already used.

**Facts and document cards** (`facts/`)
- Each document gets a kind (contract, invoice, meeting notes, policy, other) from rules; optional local GLiNER
  zero-shot (`CL_FACTS_ZERO_SHOT=1`) when the rules are unsure.
- Typed facts are read with provenance (document, passage id, exact quote and span, extractor, confidence):
  contracts (parties, effective date, term, notice period, fee, payment terms, governing law, auto-renewal,
  liability), invoices (number, date, vendor, amount, tax), meeting notes (date, attendees, decisions, action items
  with owner and due date). Amounts (lakh/crore), dates and durations are normalised.
- Approved facts become tables in the workspace (`fact_contracts`, `fact_invoices`, `fact_meeting_decisions`,
  `fact_meeting_actions`) that the data build drafts like any table, so metrics and DuckDB joins work on them;
  row access follows the documents' groups.
- One card per document (kind, title, extractive summary, sections, key facts, decisions, actions): MCP
  `get_doc_card`; `search_documents` and `ask` return the cards of the top documents.

**Checks before facts go live**
- Quote check (the quote must be in its passage, normalised) or the fact is rejected.
- Constraints in the workspace's ontology (required fields, ranges, amount > 0, two parties, tax <= amount, an
  action due after its meeting): violations go to review with the reason.
- Parties and vendors are matched to database rows (an id in the passage, or names without legal suffixes,
  RapidFuzz): 92+ linked, 75-92 to the owner. Fact tables carry the matched keys, so a PDF invoice joins with the
  vendor master.
- Agents, cards and fact tables only use approved facts.

**Owner inbox**: uncertain facts (with quote and source), name matches, drafted metrics and proposed graph types in
one list; approve, reject or correct; decisions are kept on rebuilds. API `/api/v1/inbox`, admin MCP `review_inbox`,
web Knowledge -> Inbox.

**"Answerable?" check**: before gathering evidence, rules decide whether the workspace can answer (metric, entity,
fact or document coverage; unknown names). If not: `found: false`, route `not_answerable`, the reason and clarifying
questions, no retrieval. Mixed questions are split into parts with their own routes. `CL_ANSWERABLE=0` turns it off.

**Grounding check**: MCP `check_answer(question, answer, evidence_ids?)` marks each sentence of a draft supported,
partly supported, unsupported or without checkable claims, by matching its numbers, dates, names and quotes to the
caller's evidence (passages in their groups, approved metric rows and facts). The agent guidance asks agents to use it.

**Docs**: 0.5 keeps passages and the graph build as files in each workspace's folder (Postgres holds what people
define or decide, Qdrant the vectors); README and the Docker notes now say so.

## 0.5.0: workspaces, Postgres, sources kept up to date on their own

One server, many workspaces. Each workspace connects its sources once; Context Layer then picks changes up by itself
and agents only ask.

**Storage** (everything structured in Postgres; vectors in Qdrant; files per workspace)
- People, sessions, keys, OAuth sign-ins, audit and settings moved from SQLite to Postgres (`app/db.py`,
  `migrations/`). `python -m app.migrate_local` moves a 0.4 installation into one workspace.
- The semantic layer is stored per workspace in Postgres with versions and history; YAML is import/export.
- Document access groups in Postgres; documents, tables and build outputs (passages `2_chunks.json`, embeddings,
  the graph build `_graph_build.json`) stay as files in the workspace's folder.
- Qdrant runs as a server shared by all workspaces (tenant key on every point and search).
- Secrets (connector tokens, database passwords, the AI extraction key) are encrypted per workspace (`CL_SECRET_KEY`).
- Docker: `db` (Postgres 17 + Apache AGE + pgvector) and `qdrant` services.

**Workspaces**
- A person has one account and a role and access groups per workspace; switch from the account menu.
  `CL_SIGNUP=open` lets anyone create a workspace; default: owners add people.
- Row-level security in Postgres (forced; the app runs as a role without superuser rights) keeps workspaces apart
  even if a query forgets its filter. Caches are kept per workspace.
- Keys have a scope: agent (asks questions) or admin (also sets the workspace up). MCP lists only the tools a key may use.

**Picked up on their own**
- Connectors: GitHub (code and docs as documents, one access group per repository; commits, pull requests,
  issues, CI runs and contributors as tables with a ready-made engineering semantic layer of 19 metrics) and
  Postgres (snapshot, incremental with an `updated_column`, or live). Synced on a schedule and on signed webhooks
  (`/hooks/github/<id>`); only what changed is fetched and rebuilt. Admin keys can connect sources from the chat
  (`connect_source`, `list_sources`, `sync_source`).
- Jobs are queued in Postgres and run by workers in any app process: one job per workspace at a time, workspaces in
  parallel. Questions no longer wait for a build: indexes are updated in place.
- The search index is incremental (only new or changed passages are written) and passage ids are content-based.
- Nightly, every approved metric is run; a broken one is taken out (`needs_review`) with the reason and comes back
  once it works. Questions no metric matched become synonym suggestions for an owner.

**Knowledge-graph ontology per workspace** (`graph/ontology.py`)
- Entity types (with parent types), relation types (with domain and range) and extraction rules live per workspace
  in Postgres, versioned, with history and the same approval policy as metrics. Extraction uses the approved
  ontology; a change re-extracts only passages extracted with the old one.
- The first workspace starts with `graph_layer/schema.yaml`, new ones with generic types. Every semantic entity
  becomes an entity type and every join a relation type, so graph and data share one vocabulary (reviewed types of
  the same name are kept). Connecting GitHub adds a software ontology (Repository, Service, Module, PullRequest...).
- Names that only fit a generic type and share a head word ("Leave Policy", "Travel Policy"...) are proposed as new
  types with examples; owners approve them (API or the `review_ontology` admin tool).
- Rows of lookup tables (customers, regions, teams...) become graph entities from the workspace's semantic model
  instead of the hard-coded demo tables; their joins become relations.
- API: `GET/PUT /api/v1/ontology`, `GET /api/v1/ontology/export.yaml`, `POST /api/v1/ontology/{kind}/{name}/status`.

**Security**
- Graph evidence is kept per passage: link sentences, entity descriptions and aliases are built at answer time only
  from passages the caller can see (before, text merged from all passages could show once any passage was visible).
  The admin graph explorer applies the same to viewers.
- Delegate keys (scope `delegate`) for apps acting for their own end users (e.g. CodeVerse): each request names the
  end user with signed headers (HMAC-SHA256, 5 minutes, one-time) or a short-lived HS256 token, signed with the key's
  signing secret (shown once). The call gets that user's groups only (`PUT /api/v1/external-users/{id}`), never the
  key's; no or bad identity is refused. Events record the key and the end user.
- Keys can expire and can be limited to IP ranges (CIDR); behind a proxy set `CL_TRUST_PROXY=1`.
- Qdrant requires an API key (`CL_QDRANT_API_KEY`) and stays inside the Docker network.
- Activity is kept for `event_retention_days` (default 90, deleted nightly); questions can be stored as a hash and
  length instead of text (`PUT /api/v1/settings/privacy`).

**Getting started guide**
- Opens on a person's first sign-in to a workspace (dismissible; "Getting started" in the menu reopens it). Owners and
  admins: name the workspace, connect a source (or try sample data), watch the build, review drafts, connect an AI
  app (MCP link, Claude, ChatGPT, Claude Code and Cursor snippets, key scopes), ask a test question. Viewers: what
  they can see, connect their own Claude or ChatGPT, ask a first question.
- Every step is ticked from the workspace's real state; the Overview checklist uses the same facts.
- API: `GET /api/v1/guide`, `POST /api/v1/guide/dismiss`, `POST /api/v1/guide/reset`, `POST /api/v1/guide/sample`.

**Answers**
- Metrics respect row access rules (e.g. only the caller's repositories), in numbers and in known values.
- The router returns an evidence plan: a "why" question about a metric also gets the number, and a number question
  with "explain", "policy" or "how is ... calculated" also gets the passages.

## Unreleased (folded into 0.5.0): evidence quality

What an agent receives is now graded, so it can tell real evidence from loosely related text.

- Each passage carries `relevance` (0–1, from the re-ranker) and an `evidence` label: strong, partial or weak.
  Every answer has a top-level `evidence` (strong / partial / weak / none, or `exact` for metric results).
- Passages the re-ranker judges unrelated (relevance below 0.02, `CL_MIN_RELEVANCE`) are no longer returned.
  Before, a question the sources do not cover still got five passages and `found: true`. Now it gets
  `evidence: none`, `found: false` and a note.
- Near-identical passages (e.g. the same paragraph in two versions of a file) take one slot, not several.
- The top three passages include the text just before and after them when it belongs to the same section and
  the caller may see it, so a list or table split by the chunker arrives whole. Passages are no longer cut at
  2,000 characters (limit 4,000).
- Each passage says when its source file was last updated. The guidance tells agents to prefer the newer
  source when passages disagree, and not to state facts only weak passages support.
- Repeat questions: the same question from callers with the same groups and capabilities returns the stored
  evidence (`cached: true`) for up to 10 minutes. This is an exact match only, because a similarity cache
  would confuse "revenue in 2024" with "revenue in 2025". Any rebuild, access change or new day clears it.

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
