# Plan: add-on features on top of `product` (v0.8.0 → 0.9.0)

Branch `add_on_features` from `shishir/product` (`653d267`). Code is ported from `shishir/dev` (v0.7.0, `6b89fa6`)
where it exists, adapted to the product access model. One commit per item; nothing pushed.

## Ground rules that shape every item

- **Words.** In product, "connectors" means *what Claude may use* (documents, data, graph, upload; `app/access.py`).
  Outside sources are therefore **Sources** everywhere a person sees them (UI, API `/api/v1/sources`), and the
  Python package is `sources/` (not `connectors/`) so the two never get mixed up in code either.
- **Groups = data sets.** Product already stores data sets as the access labels on files (`engine.access_map()`,
  Qdrant payload `access`). Dev's "groups" map 1:1 onto data sets; nothing new is needed to filter by them.
- **Storage order.** Items 1–4 come before the move to Postgres (item 5). New tables in items 1–4 are written
  against `core.db()` / `core.tx()` with `?` placeholders and JSON in TEXT columns, so item 5 can switch the
  backend underneath (dev's `app/db.py` already accepts `?` and returns sqlite-like rows).
- **Tests.** His suite runs on SQLite + embedded Qdrant. From item 5 on, Postgres tests need a throwaway
  database: my own container `cl-addon-testdb` (postgres:17, port 5498) — not the shared `cl-test-db` on 5499,
  which the dev-branch session empties per test. Tests that need it skip when it isn't there.
- MCP tool names, parameters and response shapes stay; only fields are added.

## Items, files, conflicts

| # | Item | Files (new / changed) | Conflicts and decisions |
|---|------|-----------------------|-------------------------|
| 1 | Sources: GitHub, Postgres, MySQL, S3 folders; schedule + signed webhooks; into a chosen data set | new `sources/{base,sql_base,files_base,github,postgres,mysql,s3,manage}.py`, `sources/templates/github.yaml`, `app/scheduler.py`, `app/vault.py` (minimal Fernet); `app/main.py` (routes `/api/v1/sources*`, `/hooks/{kind}/{id}`), `app/engine.py` (save files with a source tag), `web/src/pages/DataPage.tsx` (Sources section), tests `tests/test_sources.py` | dev's GitHub gives each repo its own group; here the admin picks **one data set** per source (default: a new data set named after the source). Dev lands files in its landing zone + registry; here documents go to `documents/sources/<id>/…` through `engine.save_document` (his `_access.json`), tables through `engine.save_table`. Dev's `semantic.repo.import_layer` does not exist in product: SQL tables are profiled by his auto-model on the next "prepare data". Row rules from sources wait for item 8. Jobs use his in-process queue (`jobs.enqueue`) until item 14. |
| 2 | Quality tab (deterministic scores after every build, trend) | `app/quality.py` (runs + history table), `app/engine.py` (`rebuild` records a run), `app/main.py` (`/api/v1/quality*`), `web/src/pages/Quality.tsx` back in the admin menu (`admin.tsx`, `App.tsx`), gold files: existing `eval_questions.json`, `semantic_layer/eval_questions.yaml`, `graph_layer/gold_extraction.json`, Kestrel questions in `demo/kestrel/` | His `engine.quality()` and `eval_*` jobs exist; they are reused, not replaced. Retrieval metrics (recall/MRR/nDCG) come from `rag/retrieval_eval.py`; no step calls a model (no RAGAS/LLM judge). |
| 3 | Better search results | `rag/retrieve.py` (relevance + label, `CL_MIN_RELEVANCE`, near-duplicate drop, same-section neighbours), `app/engine.py` (exact-match ask cache, invalidated by any build/upload) | Keep his RRF fusion and `ensure_coverage`; add fields to each passage (`relevance`, `match`) without removing any. |
| 4 | "Can this be answered?" + mixed-question split | new `app/answerable.py`, `app/planner.py` (from dev), `app/engine.py` `ask()` adds `answerable` / `parts` | His `app/router.py` keeps routing; the planner only adds the check and splits "X and how many Y" into parts. |
| 5 | Accounts and access in Postgres | new `app/db.py` (pool, `?` → `%s`, rows), `migrations/001_core.sql` …; `app/core.py`, `access.py`, `teams.py`, `directory.py`, `approvals.py`, `doc_requests.py`, `sharing.py`, `oauth.py` (SQL dialect: `executescript`, `INSERT OR …`, `lastrowid`), new `app/migrate_local.py` (idempotent copy from `var/app.db`) | SQLite-isms in his modules (`executescript`, `rowcount` after `INSERT OR IGNORE`, `lastrowid`). Tests keep a SQLite fallback? **No** — one backend; tests use the test Postgres. His `cl_defs` stays where it is (same database). |
| 6 | Workspaces per company (tenant_id + RLS FORCE, non-superuser role) | all tables from item 5 + `cl_defs` tables get `tenant_id`; `app/db.py` tenant scope; sign-up creates a workspace; per-workspace files (`rag/wspath.py`), Qdrant filter, defstore per tenant; `app/main.py` resolves the workspace per request | Biggest item. His company admin = workspace owner. Existing install → workspace `default` on first start. |
| 7 | Graph leak fix | `graph/build.py`, `graph/search.py`, `graph/access.py` (dev `a6cb7a7`) | His entity resolution by meaning keeps working; evidence is filtered per passage. |
| 8 | Row rules for numbers | `semantic/access.py`, `semantic/compiler.py`, `app/access.py` + Access tab (rules per data set/team/role) | Dev's rules are keyed by group; here by data set, team or role. |
| 9 | Nightly metric check | `semantic/monitor.py`, scheduler entry, Approvals tab section | Uses his approvals UI; `needs_review` metrics hidden from agents. |
| 10 | Keys for apps (delegate) | `app/delegate.py`, migration 007 fields (expiry, IP allow-list), Users/Settings UI | Delegated end users map to his access model through data sets/roles given in the token. |
| 11 | Qdrant as its own server | `rag/store.py`, `rag/config.py`, compose | Keep "answers keep working during a rebuild": incremental upserts into the live collection (old points deleted only after new ones are in). |
| 12 | Graph in Postgres + NetworkX cache | `graph/kg.py`, migration 013, drop AGE from product path (`graph/store.py`, `graph/query.py`) | His ontology/resolution write through the new store. |
| 13 | Tables as Parquet + DuckDB views | `app/storage.py`, `app/landing.py`, `semantic/db.py`, `semantic/ingest.py` | Source tables (item 1) switch to Parquet here. |
| 14 | API and worker containers | `app/worker.py`, `app/jobs.py` (Postgres queue), scheduler advisory lock, `CL_ROLE`, compose | His in-process `LOCK` semantics move to the job table. |
| 15 | Secrets key management (last) | `app/vault.py` (providers, MultiFernet rotation, `*_FILE`), production start check | Item 1's minimal vault grows into this. |

Docs: `CHANGES.md` (0.9.0), README / GETTING-STARTED / ARCHITECTURE as items land.
