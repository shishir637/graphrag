# Architecture (0.7)

Every piece of data has exactly one home. There are three layers, and the third can always be rebuilt from the
first two.

```
 sources ──► 1. LANDING ZONE (files)          2. SYSTEM OF RECORD (Postgres)          3. DERIVED ENGINES
             data/<table>.parquet  ─────────► landing_files (manifest)       ─────► DuckDB views over the Parquet
             documents/<original>             passages, facts, doc cards              fact tables (Parquet projection
                                              semantic layer, ontology                 of approved facts)
                                              knowledge graph (kg_* tables)            in-memory graph (NetworkX)
                                              identity & access, secrets               Qdrant hybrid index
                                              inbox, jobs, audit                       metric catalog (in memory)
```

## 1. Landing zone: raw data, kept as received

One folder per workspace on the file volume (`CL_FILES_DIR/<workspace>/`, the `app_data` Docker volume):

| Folder | Holds |
|---|---|
| `data/` | tables, one Parquet file (zstd) per table: uploads (CSV converted with DuckDB's inferred types; Excel, JSON, JSON Lines cleaned), connector snapshots (Postgres, MySQL, GitHub activity), fact tables |
| `data/_uploads/` | original table uploads, and pre-0.7 CSV copies |
| `documents/` | original documents (uploads, upload links, agents' `add_document`, GitHub and S3 syncs) |
| `archive/` | optional: audit events older than the retention period, one Parquet file per month |

Every file landing in `data/` or `documents/` writes a row in the **landing manifest** (`landing_files`): kind,
name, path, source, connector, who pushed it, groups, sha256, bytes, rows and schema. A replaced or removed file
keeps its row (`replaced_at`), so the manifest is the history of what arrived. Landing an identical file changes
nothing.

All file access goes through `app/storage.py` (`Store`: `put`, `put_file`, `get`, `open`, `list`, `delete`,
`exists`, `stat`, `path_for_duckdb`, `scratch`). `LocalStore` implements it over the local folders.

### Switching to an S3-compatible store later

1. Implement `Store` for S3 (e.g. `S3Store` with boto3): `put`/`put_file` upload objects under
   `<workspace>/<area>/<name>`, `get`/`open` download, `list` uses `list_objects_v2`, `exists`/`stat` use
   `head_object`, `scratch` stays a local temporary file, and `path_for_duckdb` returns `s3://bucket/<key>`.
2. Return it from `store()` when `CL_STORE=s3` (with the bucket and credentials as settings; credentials through
   `*_FILE` or the secrets manager).
3. In `semantic/db.py`, load DuckDB's `httpfs` extension and create an S3 secret before the views are created; the
   views then read `s3://...` paths.
No caller changes: every reader and writer already goes through `store()`.

## 2. System of record: Postgres

Everything the system knows and decides, per workspace, with `tenant_id` and **forced row-level security** on every
table (the app connects as a non-superuser role, `cl_app`, so a missing filter in code still returns nothing from
another workspace). Migrations are numbered SQL files in `migrations/`, applied once at start under an advisory lock.

| Area | Tables |
|---|---|
| Landing manifest | `landing_files` |
| Documents | `documents` (groups, source), `passages` (+ `passages_state` version) |
| Facts | `facts`, `fact_links`, `doc_cards` |
| Semantic layer | `sl_definitions` (+ `sl_history`, `sl_state` version), `sl_value_lists`, `sl_access_rules` |
| Ontology | `onto_definitions` (+ `onto_history`, `onto_state`) |
| Knowledge graph | `kg_entities`, `kg_relations`, `kg_entity_passages`, `kg_communities` (+ `graph_state` version) |
| Work | `jobs` (queue), `connectors` (config, state), `events` (audit), `settings`, `sl_gaps` (unanswered questions) |

### Identity, access and secrets

| Tables | Protection |
|---|---|
| `users` | scrypt password hash |
| `memberships` | role (owner, admin, viewer) and groups per workspace |
| `sessions`, `agents` (keys), `oauth_tokens`, `upload_links` | only SHA-256 hashes of the tokens are stored |
| `external_users`, `used_signatures` | end users of delegate keys and one-time signatures (no secrets) |
| `secrets` | connector tokens and passwords, Fernet-encrypted (`app/vault.py`) |
| `sl_access_rules`, `events` | row access rules for metrics; the audit log |

The Fernet master key is **not** stored in the database: it comes from `CL_SECRET_KEY` (several keys, newest
first, for rotation), `CL_SECRET_KEY_FILE` (a mounted Docker / Kubernetes secret) or `CL_SECRET_KEY_PROVIDER` (AWS
Secrets Manager, GCP Secret Manager, Azure Key Vault, OpenBao / HashiCorp Vault, or your own function).
`python -m app.vault rotate` re-encrypts every secret with the newest key. With `CL_ENV=production` the app refuses
to start without a configured key. `CL_DATABASE_URL`, `CL_DB_PASSWORD` and `QDRANT_API_KEY` can be read from files
(`NAME_FILE`).

## 3. Derived engines: rebuildable, never the source

| Engine | Built from | Kept fresh by |
|---|---|---|
| DuckDB (`semantic/db.py`) | views over the Parquet files; live SQL sources attached read-only | rebuilt when a file changes (cheap: views only); memory limit + spill folder per workspace |
| Fact tables (`facts/store.py`) | approved facts in Postgres | rewritten after facts change |
| In-memory graph (`graph/search.py`) | the `kg_*` tables | reloaded when `graph_state.version` moves |
| Metric catalog (`semantic/enrich.py`) | the semantic layer in Postgres | rebuilt when `sl_state.version` moves |
| Qdrant hybrid index (`rag/store.py`) | passages in Postgres (ids and filter fields only; texts are read from Postgres) | incremental upserts by text hash |

Any of them can be deleted and rebuilt ("Prepare documents", "Prepare data", "Build the graph"), so they can never
disagree with the source.

## Processes

One image, two roles (`CL_ROLE`): `api` (web app, REST, MCP) and `worker` (job workers and the scheduler);
`all` runs both in one process. Questions never run a language model: rule-based planning (`app/planner.py`),
hybrid search, the graph, and metrics compiled to SQL; the calling agent writes the answer and can check it with
`check_answer`.

## Upgrading an existing install

At every start `app/upgrade.py` brings existing workspaces to this layout, idempotently and under an advisory lock:
CSV tables become Parquet (originals kept), every landed file gets a manifest row, `2_chunks.json` moves into
`passages` and `_graph_build.json` into the graph tables (both files kept with a `.pre-0.7` suffix). The database
image is now the official `postgres:17`; the old Apache AGE copies are no longer used and can be removed once the
upgrade has run: `docker compose exec app python -m app.upgrade drop-age`.
