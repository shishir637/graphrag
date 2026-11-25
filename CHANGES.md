# Changes

## 0.5.0: workspaces, Postgres, sources kept up to date on their own

One server, many workspaces. Each workspace connects its sources once; Context Layer then picks changes up by itself
and agents only ask.

**Storage** (everything structured in Postgres; vectors in Qdrant; files per workspace)
- People, sessions, keys, OAuth sign-ins, audit and settings moved from SQLite to Postgres (`app/db.py`,
  `migrations/`). `python -m app.migrate_local` moves a 0.4 installation into one workspace.
- The semantic layer is stored per workspace in Postgres with versions and history; YAML is import/export.
- Document access groups in Postgres; documents, tables and build outputs in the workspace's folder.
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
