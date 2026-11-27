# Changes

## 0.9.0 (27 Nov 2026): add-on features on top of the admin app

Fifteen add-ons from the 0.5-0.7 line, ported onto 0.8's admin app (teams, data sets, roles, approvals, layers,
check_answer, the AI-proposed ontology, entity resolution by meaning and the Kestrel demo all stay as they were).
No AI model runs at question time; MCP tool names, parameters and response shapes are unchanged (fields only added).

- **Sources** (Data tab -> Sources): GitHub, Postgres, MySQL and S3-compatible folders connected once and kept up
  to date on a schedule (15 minutes to a day) and, for GitHub, on a signed webhook (`/hooks/github/<id>`,
  HMAC-SHA256). Each source puts its files into one data set you choose (default: a new one named after it), so
  who sees them is set on the Access tab. GitHub brings code and docs as documents and its activity as tables with
  a ready-made engineering layer (median time to merge, open bugs ...). SQL sources copy tables as Parquet (batched,
  incremental by an updated-at column) or read them live through DuckDB. Tokens and connection strings are stored
  encrypted and never shown again. ("Connectors" keeps its meaning: what Claude may use for a person.)
- **Quality tab**: after every build the app scores itself with gold answers and no AI model: document search
  (recall@1/5, all@5, MRR, nDCG@10 on gold passages), metric questions (the rule planner against gold requests,
  including new Kestrel gold questions when the demo is loaded) and graph extraction (entity F1, link recall on
  hand-labelled passages). Latest scores, the change since the last run and a trend line per suite; "Score after
  every build" can be turned off.
- **Better search results**: every passage has a relevance (0-1) and a label (strong / partial / weak); unrelated
  passages (below `CL_MIN_RELEVANCE`, 0.02) and near-duplicates are dropped; the top passages get the text just
  before and after them in the same section; `source_updated` helps the agent prefer the newest source. The same
  question asked again (same access, same data) is answered from a cache until anything is rebuilt.
- **"Can this be answered?"** before any search: a rule-based check (a known metric, a graph entity, how much of the
  question's wording the documents and data know, names nothing mentions). If not, the answer says what is not
  known and suggests clarifying questions instead of guessing. A question that mixes a number and a policy is
  answered part by part. `CL_ANSWERABLE=0` turns it off.
- **Accounts and access in Postgres**: people, sessions, keys, activity, settings, the access tables, teams, the
  directory, approvals, chats, sources and quality runs move from `var/app.db` to Postgres (`CL_DATABASE_URL`;
  Docker sets it). An existing `app.db` is copied in once at the first start (idempotent; the file is left as it
  is). Without `CL_DATABASE_URL` the app still runs on SQLite.
- **Separate workspaces per company**: every workspace table has a tenant id and a Postgres row-level security
  policy (FORCEd; the app works as the non-superuser role `cl_app`). `CL_SIGNUP=open` lets a company sign up
  (`POST /api/v1/signup`): it gets its own workspace with the person as owner (company admin). The existing install
  is the first workspace and keeps its folders; other workspaces' files are under `CL_FILES_DIR/<id>`, their
  definitions in a schema of their own (`cl_defs_<id>`).
- **Graph leak fix**: link sentences, entity descriptions, names and aliases come only from passages the caller may
  see (evidence kept per passage), in find_connections, explore_entity, overview and the graph explorer.
- **Row rules for numbers** (Access tab -> Row rules): metrics count only the rows a data set, team or role may see
  (e.g. region managers see their region). Rows joined to hidden rows are hidden too, so totals never include them.
- **Nightly metric check**: every approved metric is run on the current data each night; one that fails (a renamed
  or dropped column) is taken out (status needs_review), hidden from agents and shown on the Approvals tab with
  the reason, and approved again when it works again.
- **Keys for apps** (Settings -> Keys for apps): a key that acts for an app's own users (e.g. CodeVerse) proves on
  every call which user it acts for (one-time HMAC-signed headers or a 15-minute token) and gets only that user's
  data sets and roles, never more than the key has. Any key can expire and be limited to IP ranges.
- **Qdrant as its own server** (`QDRANT_URL`, `QDRANT_API_KEY`; a `qdrant` service in Docker, not published): one
  shared collection filtered per workspace; builds write only new or changed passages and remove the ones that are
  gone, so answers keep working during a rebuild. An existing index is moved at the first start without
  re-embedding. Without `QDRANT_URL` the embedded index works as before.
- **Graph in Postgres tables** (`graph_*`) with an in-memory graph (and a NetworkX view) per workspace and version.
  Apache AGE is no longer used: Docker uses the official `postgres:17` image; `python -m app.upgrade drop-age`
  removes AGE's leftovers from an older database.
- **Tables as Parquet**: every table that arrives is stored as Parquet with a manifest (`data/_manifest.json`:
  source, who, rows, columns, hash) and read by DuckDB through views, so nothing is copied into memory. Older CSV
  tables are converted at start in Docker (`CL_LANDING_BACKFILL=1`, or `python -m app.landing backfill`).
- **API and worker containers**: on Postgres the job queue is a table; `CL_ROLE=api` answers only, the `worker`
  service (`python -m app.worker`) runs builds, syncs, quality runs and the nightly check; one process schedules
  (advisory lock).
- **Secrets key management**: the key that encrypts stored secrets comes from `CL_SECRET_KEY` (several keys for
  rotation), `CL_SECRET_KEY_FILE` or a secrets manager (`CL_SECRET_KEY_PROVIDER`: aws, gcp, azure, vault, your own
  module); `python -m app.vault rotate` re-encrypts with the newest key; with `CL_ENV=production` the app refuses to
  start without a key. `*_FILE` variants for the database URL and password, the Qdrant key, the definitions DSN.
- Upgrade from 0.8 with Docker: add `CL_QDRANT_API_KEY` (and ideally `CL_SECRET_KEY`) to `.env`, then
  `docker compose up -d --build`. The first start copies `app.db`, moves the search index and the graph, and
  converts CSV tables. Then `docker compose exec app python -m app.upgrade drop-age` once.
- Tests: the whole suite runs on SQLite and, with `CL_TEST_DATABASE_URL`, on Postgres (row-level security on);
  Qdrant server tests use `CL_TEST_QDRANT_URL`.

## 0.8.0 (27 Nov 2026): admin app with data and access

- **Personal changes to the team or company, with approval.** From chat a person asks Claude to share their own
  rule, synonym or metric, or a file they added, with their team or the company (tool publish_change, kind
  metric/rule/synonym/file, to company/team/team:<name>). Company admins, and team admins for their team, do it
  directly after a yes; anyone else creates a request. Team requests go to that team's admins (and admins above it,
  and company admins); company requests to company admins. New Approvals tab (with a count) to approve or decline,
  and approved/declined history. my_changes tells the person what happened. Approving a file moves it into the
  data set and prepares it again.
- **Claude is told when to use the tools:** the server instructions and the ask tool name the organisation and its
  teams and say to use Context Layer for every question about it before answering from memory or the web.

- **Tabs for admins:** Users, Data, Access, Activity, Settings (Settings for company admins). Members see one page:
  their team, their link, and what Claude can see and use for them.
- **Access model** (app/access.py, tables ac_* in var/app.db): *data sets* label files (Everyone = public, one per
  team, and any you add); *connectors* are what Claude may use (documents, metrics and tables, knowledge graph,
  adding files); *roles* bundle data sets and connectors. A person gets the Everyone role + their team's access
  (and the teams above it) + what is given to them directly (roles, data sets, connectors). Claude and ChatGPT
  sign-ins use exactly this.
- **Users:** add or edit a person with team, what they manage (nothing, team admin of some teams, company admin)
  and data access (same as team, or team + extra). Import from a CSV, new sign-up links, set team admins and copy
  team links from the Teams list. Team admins do the same for their teams, giving only access their team has.
- **Data:** files by data set, who can see each data set, upload into data sets, change a file's data sets, delete,
  prepare for Claude; data tables. Team admins add files to their team's data set and see what their team sees.
- **Access:** teams (data sets, roles, extra connectors), roles, data sets, and "check what someone can see" with
  the reason for each item.
- **Activity:** decisions first (team and company change requests, new metrics), then what happened in plain words.

## 0.7.0 (27 Nov 2026): three levels of users, approvals, safer sign-in

- **Teams with admins.** Teams form a tree (Engineering > Data Engineering). A company admin (owner or admin)
  adds teams and names team admins; a team admin looks after their team and the teams under it: people (add,
  invite, remove), admins of teams below, the team link and its changes.
- **Team layers and team links.** Each team has its own layer of rules, words and metrics, read by its members
  (and the teams under it) between the company layer and their own. Team link: `/mcp/teams/<team>`, for members
  plus anyone the team admin allows.
- **Approvals.** Changes still go to a person's own layer first. Asking Claude to change a team or the company
  layer, without the right to do it directly, creates a request; the team's admins (or company admins) approve or
  decline it on the new Approvals page. Approved changes are recorded as asked by the person, approved by the admin.
- **The web app is one page for now** (the older admin pages are still in web/src/pages, not shown). Admins:
  Users (add user with team, the teams they administer and role; change team, team-admin-of and role inline;
  remove) and Teams (add, nest, delete). A person is in one team and can be admin of several. Team admins: the
  same page limited to their teams; they add and remove people there. Everyone else: their team and their link.
- **Sign-in hardening.** New passwords: 15+ characters and checked against known breaches (Have I Been Pwned range
  API, only 5 hash characters leave; if offline it does not block). Passwords stored with scrypt N=2^17 and
  upgraded at the next sign-in. Web sessions end after 2 hours without use and after 24 hours at most
  (`CL_SESSION_IDLE_MIN`). Sign out everywhere (optionally also disconnects Claude/ChatGPT).

## 0.6.0 (26 Nov 2026): company layer, personal layers and sharing from chat

- **Business context from chat.** Anyone connected through Claude or ChatGPT can add or change metrics, business
  rules and synonyms, and add notes with files. Each change is asked first: the question says whether the
  company or the person defines the item and how a number changes. No approval step; every change is recorded
  with the person's name and can be undone (`my_changes`, `undo_change`, `who_changed`).
- **Layers.** One company layer for everyone; each person's changes go to their own layer and only change their
  own answers. Publishers (owner, admins, publishers) can move a change into the company layer (`publish_change`).
- **Sharing from chat.** `share_my_layer` gives a person their own MCP link, for everyone or named people or
  teams. Others add the link in Claude, sign in on the Context Layer page and get answers with that layer;
  inside the company connector, `use_layer` does the same.
- **Definitions in Postgres** (schema `cl_defs` in the graph database): layers, definitions, shares, history.
  semantic_layer/*.yaml stays as a readable copy of the company layer; on first start the YAML becomes the
  company layer. Accounts and the directory stay in var/app.db (SQLite) for now.
- **Admin console:** Directory page (people, teams, CSV import, invite links) and Layers page (layers, clashes,
  locked metrics, history with undo, export). New role: publisher. New accounts get a temporary password that
  must be changed at first sign-in.
- **Demo company:** Kestrel Labs, a made-up 124-person software company with matching data, documents and
  directory. Settings -> Demo company loads it as the company layer (old company layer saved in var/backups).
- `scripts/add_account.py` adds an account with a temporary password written to a file.
- **Grounding check** (`check_answer` MCP tool): every sentence of the AI's draft answer is checked against the
  cited passages and metric results with the existing re-ranker, plus a numbers/dates/names check. No AI model.
- **Entity resolution by meaning**: near-spellings and acronyms merge only when the passages that name them agree
  (BGE-M3 passage vectors, already computed); look-alike names used in unrelated passages stay apart.
- **AI-proposed ontology**: Knowledge graph page -> Ontology. A model (a free local one through Ollama by default:
  `docker compose --profile localai up -d ollama`) proposes entity and link types with real examples from the
  documents; an admin approves; the types are stored in Postgres (history, undo) and replace the hand-written list.
- Fixes: the metric check no longer needs the sample company's finance report; an empty verified-query list no
  longer stops the metric catalog build.

## 0.5.1 (25 Nov 2026): uploads from Claude / ChatGPT no longer slow everything down

Found while testing the connector from Claude.

- **Answers keep working during a rebuild.** Searches no longer wait for document or graph builds (they timed
  out behind the Cloudflare tunnel as "Bad Gateway"). The search index is built into a second collection and
  switched in when complete; files the answers read are written atomically. Metric questions wait at most 45 s,
  and only while metric tables are being rebuilt.
- **An upload only processes the new file.** The graph step reads only the uploaded files' passages (before,
  it could re-read every passage with GLiNER: an hour on a laptop). If the graph was never built for most
  passages, uploads skip it and say so; build it once from the Graph page. Documents parsed before an update
  are reused instead of parsed again.
- **build_status shows progress**: the steps (reading, embedding, index, graph, passages done) with times, and
  how long it has been running.
- **Cleaner graph names**: names no longer run across lines of a record ("Tomas Rivera" instead of
  "Tomas Rivera / lumora_incidents"), also for graphs extracted before (fixed when merging).
- **Search models load at start-up** in the background, so the first question is not slow.
- Fixed a test that failed on a cookie detail (`test_chat.py`, guests).


## 0.5.0 (25 Nov 2026): Chat page with each person's own Claude API key

A **Chat** page inside Context Layer. People sign in to Context Layer as usual, add their own Claude API key once
(the key icon; billed to their own Claude Console account) and chat. Claude answers with the same tools agents get
over MCP, with the person's own access groups, and cites passages (numbered chips that open the passage).
Files dropped into the chat are added to Context Layer (search index, graph, metric drafts) in the background, and
small PDFs and text files are also shown to Claude directly in that turn so it can use them at once.

- Keys are stored encrypted (master key in the secrets file, not the database) and never sent back to the browser.
  The key is checked with a free call before it is saved.
- Owners can let people without a key use a company key, and choose who may add files from the chat
  (Settings → Chat).
- Chats are private to each person. Failed turns keep the message with the error and are not replayed.
- Viewers now land on Chat; owners and admins still land on Overview.
- Claude.ai / Pro / Max sign-in is not offered: Anthropic does not allow it in third-party apps.

Code: `app/chat.py`, `app/main.py` (`/api/v1/chat*`, `/api/v1/settings/chat`), `web/src/pages/Chat.tsx`,
`web/src/pages/Settings.tsx`. Tests: `tests/test_chat.py` (fake Claude, no key or network needed).

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
