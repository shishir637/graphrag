# Context Layer

Governed company knowledge for AI agents. Connect documents and data tables once; give each AI agent
(Claude, ChatGPT, Cursor, Copilot, your own bots) its own key and one MCP link. Context Layer finds the
evidence - passages with sources, numbers computed from reviewed definitions, knowledge-graph links - and
the agent writes the answer. **No AI model runs inside Context Layer and no API key is needed**: search,
re-ranking, entity extraction and metric planning all run locally on CPU.
One server hosts many **workspaces** (companies, teams, products). Each workspace connects its sources once
(uploads, GitHub, a Postgres database) and they are then **picked up on their own**: synced on a schedule or a
webhook, indexed incrementally, metrics drafted and checked every night. Workspaces never see each other's data
(Postgres row-level security, a workspace filter on every search).

**New here? Follow [GETTING-STARTED.md](GETTING-STARTED.md).** Who it is for: [PRODUCT.md](PRODUCT.md). What changed: [CHANGES.md](CHANGES.md).

## Use it from Claude or ChatGPT

People keep using their own Claude or ChatGPT (the AI and its cost stay on their plan) and add Context Layer
as a connector with just the MCP link. A Context Layer sign-in page opens (email and password, then Allow);
from then on they see what their access groups allow. Owners and admins can also attach files in the chat
and ask to add them: Context Layer builds the search index, knowledge graph and draft metrics in the
background and answers from them once `build_status` says ready.

- **Claude**: Settings → Connectors → Add custom connector → paste `https://<public name>/mcp`.
- **ChatGPT**: developer mode → create an app with the same link, OAuth sign-in. ChatGPT passes attached
  files to `add_document` directly; in Claude the model passes the file's text, or offers a one-time upload
  link (`create_upload_link`) for large or binary files.
- Both connect from the internet, so they need the public link (Cloudflare Tunnel, see below). The admin
  web app stays private; only `/mcp`, the sign-in pages and upload links are published.
- Agent keys (`clk_...`, made under Agents) keep working for bots, Cursor, Claude Code and scripts.

## Install (self-hosted, Docker)

```bash
cp .env.example .env          # set CL_DB_PASSWORD (long random) and CL_SECRET_KEY (see the file)
docker compose up -d          # app + Postgres + Qdrant; the first run downloads ~5 GB of local models
```

Open http://localhost:8080, create the owner account, then follow the checklist on the Overview page:
upload and prepare documents, optionally add data tables (CSV, Excel, JSON, JSON Lines, Parquet) and build
the knowledge graph, then connect your first agent. Agents connect to `http://<server>:8080/mcp` with their key.

- To serve other machines: put HTTPS in front (Caddy, nginx, your load balancer), set `CL_BIND=0.0.0.0`
  and `CL_PUBLIC_HOSTS=contextlayer.yourcompany.com` in `.env`.
- Everything stays in the Docker volumes; there is no outbound traffic except model downloads on first use.
- Backups: the volumes `db_data` (Postgres: workspaces, people, keys, audit, semantic layer, ontology, document
  groups, facts and document cards, passages, jobs, connectors, encrypted secrets, graph copy) and `app_data` (each
  workspace's landing zone: documents, data tables as Parquet, and build outputs such as embeddings and the graph
  build `_graph_build.json`); keep `CL_SECRET_KEY` with them.
  `qdrant_data` can be rebuilt from them ("Prepare documents").
- Coming from 0.4 (one workspace, SQLite): `docker compose run --rm app python -m app.migrate_local var/app.db`.

## Run without Docker (development, Windows/macOS/Linux)

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt     # Windows: .venv\Scripts\...
docker compose up -d db qdrant                                        # Postgres + Qdrant (publish their ports, or
set CL_DATABASE_URL=postgresql://rag:<pw>@127.0.0.1:5432/contextlayer QDRANT_URL=http://127.0.0.1:6333)
python -m app                                                         # http://127.0.0.1:8080
```
Tests need a throwaway Postgres (`CL_TEST_DATABASE_URL`, default `127.0.0.1:5499`) and optionally Qdrant
(`CL_TEST_QDRANT_URL`, default `127.0.0.1:6399`). Answering reads the graph build file; Apache AGE keeps a copy
per workspace for Cypher queries.

The web app is prebuilt in `web/dist`. To change it: `cd web && npm install && npm run dev` (port 5173,
forwards the API to 8080), then `npm run build`.

| Part | Where |
|---|---|
| Web app (React + TypeScript) | `web/src` (pages in `web/src/pages`) |
| API, sign-in, workspaces, roles, agents, audit | `app/main.py`, `app/core.py`, `app/db.py` (Postgres, row-level security; schema in `migrations/`) |
| Where things are stored | Postgres: everything people define or decide (workspaces, keys, semantic layer, ontology, document groups, facts, cards, inbox decisions, jobs, audit). Workspace folder (`CL_FILES_DIR/<workspace>/`): files and build outputs (passages, embeddings, graph build). Qdrant: search vectors. |
| Facts, cards, inbox, grounding | `facts/` (kinds, extraction, checks, matching to database rows, inbox), `app/answerable.py`, `app/grounding.py` |
| Background jobs, scheduler | `app/jobs.py` (queue in Postgres), `app/scheduler.py`, `semantic/monitor.py`, `semantic/learn.py` |
| Connectors | `connectors/` (GitHub with an engineering semantic layer, Postgres snapshot/live), secrets in `app/vault.py` |
| MCP server for agents | `app/mcp_server.py` (read: ask, search_documents, describe_data, query_metrics, list_metrics, find_connections, explore_entity, list_documents, fetch_passage, build_status; write: add_document, create_upload_link; admin keys: approve_metric, connect_source, list_sources, sync_source). Each key sees only the tools it may use. |
| Sign in from Claude / ChatGPT (OAuth 2.1, PKCE, DCR, client metadata documents) | `app/oauth.py` |
| Routing without a model (evidence plan: primary + secondary sources) | `app/router.py` |
| Documents | `rag/` (parse incl. JSON records, chunk, incremental embed, hybrid + graph retrieval, re-rank, coverage) |
| Metrics | `semantic/` (`repo.py` definitions in Postgres with history, `policy.py` approvals, `access.py` row access, `db.py` DuckDB per workspace, `auto_model.py` drafts, `rule_planner.py`); `semantic_layer/` is the first workspace's seed and the YAML import/export format |
| Knowledge graph | `graph/nlp_extract.py` (GLiNER/spaCy), `graph/build.py`, `graph/search.py`, `graph/access.py` |
| Benchmarks | `python -m rag.retrieval_eval`, `python -m semantic.rule_eval`, `python -m graph.eval_extraction` |
| Tests | `python -m pytest tests -q` |

Research tools that call Claude (RAGAS, the LLM planner, LLM graph extraction) are kept for comparison
only; install `requirements-research.txt` to use them. They are not part of the product or its image.

---

# The engines

# Small RAG system (Docling · BGE-M3 · Qdrant · Claude · RAGAS)

Drop files into `documents/`, run one command, ask questions. Every step writes its output
to one file in `outputs/` so you can inspect exactly what each layer did.

> **Branch `semantic-layer`:** adds a semantic layer for questions about numbers in tables, with a router
> that sends each question to the documents (RAG) or the data (semantic layer). See [SEMANTIC_LAYER.md](SEMANTIC_LAYER.md).

## Folder layout

```
documents/            your files (.pdf .docx .pptx .xlsx .html .md .txt, images)
  _access.json        optional permissions: {"salary_bands.txt": ["hr"]}  (default group: "public")
outputs/              one file per step (see below)
qdrant_data/          the Qdrant vector database (embedded, no server needed)
rag/                  one module per layer
rag.py                command line
eval_questions.json   test set for RAGAS
```

## Layers → files

| # | Layer | Tool | Output file |
|---|-------|------|-------------|
| 1 | Parsing | Docling (headings, tables, pages, OCR) | `outputs/1_parsed.json` |
| 2 | Chunking | Docling HybridChunker (BGE-M3 tokenizer, 512 tok) / RecursiveCharacterTextSplitter for .txt | Postgres `passages` (app) / `outputs/2_chunks.json` (command line) |
| 3 | Embedding | BGE-M3 dense (1024-d) + BM25 sparse | `outputs/3_embeddings.json` |
| 4 | Vector store | Qdrant (HNSW dense + IDF sparse, payload indexes) | `outputs/4_index.json` |
| 5+8+9 | Retrieval | Qdrant: dense + BM25 with permission filter **inside** the query, fused with RRF | `outputs/5_retrieval.json` |
| 10 | Re-ranking | bge-reranker-v2-m3 (top 20 → top 5) | `outputs/6_rerank.json` |
| 6 | Generation | Claude (`claude-opus-5-5`), structured output with citations, "not found" when unsupported | `outputs/7_answer.json` |
| 11 | Observability (light) | every query, passages, answer, timings appended | `outputs/query_log.jsonl` |
| 7 | Evaluation | RAGAS (Claude as judge) + latency/token stats | `outputs/8_eval_results.json` |

## Setup (once)

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
$env:ANTHROPIC_API_KEY = "sk-ant-..."     # needed for answering and evaluation only
```

## Use

```powershell
.venv\Scripts\python rag.py ingest                         # steps 1-4 (re-run after adding documents)
.venv\Scripts\python rag.py ask "How many days of annual leave do I get?"
.venv\Scripts\python rag.py ask "What are the salary bands?" --groups hr,public
.venv\Scripts\python rag.py chat
.venv\Scripts\python rag.py draft-questions -n 20          # Claude drafts a test set from your chunks
.venv\Scripts\python rag.py eval                           # RAGAS scores -> outputs/8_eval_results.json
```

## What RAGAS measures (and what it doesn't)

RAGAS measures **quality**, split by layer so you know what to fix:

- **context_recall**: did retrieval find the passages that contain the reference answer? Low → chunking, hybrid search, top-k.
- **context_precision**: are the relevant passages ranked near the top? Low → reranker, top-k.
- **faithfulness**: is every claim in the answer supported by the passages? Low → prompt or generation.
- **answer_relevancy**: does the answer address the question? Low → prompt or generation.

RAGAS does **not** measure speed or cost. `rag.py eval` therefore also records **efficiency**:
latency per stage (retrieval, rerank, generation, total; mean, p50 and max), average tokens per question, and the share of questions answered.

Replace the drafted questions with 50–100 real ones over time. Scores on Claude-drafted questions tend to be optimistic.

## Notes

- Windows Application Control on this PC blocks some newer wheels (`pyarrow`, `xxhash`, `grpcio`, ...); `requirements.txt` pins versions that load.
- Keep `.venv` **outside OneDrive** if you can: on-access scanning of its many small files makes imports very slow (e.g. `import anthropic` 45-77 s).
- The embedded Qdrant (`qdrant_data/`) allows one process at a time. To run a server instead:
  `docker run -p 6333:6333 qdrant/qdrant` and change `client()` in `rag/store.py` to `QdrantClient(url="http://localhost:6333")`.
- Models run on CPU; the first run downloads BGE-M3 (~2.2 GB) and the reranker.
