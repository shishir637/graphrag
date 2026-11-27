# Architecture (0.9)

```
 Claude / ChatGPT / agents / apps            browsers (admins, members)
              |  /mcp (OAuth or keys)               |  web app + /api/v1
              v                                     v
   +--------------------------  app  (CL_ROLE=api)  --------------------------+
   |  each request runs in one workspace (session, key, token, webhook, link)  |
   |  ask: answerable? -> route (rules) -> search | metrics | graph -> evidence |
   +-------------+-----------------+-------------------+-----------------------+
                 |                 |                   |
          Postgres 17        Qdrant server       shared volumes
   accounts, access, teams,  one collection,     documents, data (Parquet +
   approvals, definitions,   filtered per        manifest), outputs, per-workspace
   graph_* tables, jobs,     workspace,          files under CL_FILES_DIR
   secrets (encrypted);      API key
   row-level security per
   workspace (role cl_app)
                 ^
   +-------------+---------  worker  (CL_ROLE=worker)  ------------------------+
   |  job queue (FOR UPDATE SKIP LOCKED, one job per workspace at a time):      |
   |  prepare documents / data / graph, source syncs, quality runs, nightly      |
   |  metric check; the schedule runs in one process (advisory lock)            |
   +----------------------------------------------------------------------------+
```

- **Workspaces** (`app/db.py`, `rag/wspath.py`): `tenant_id` on every workspace table, FORCEd row-level security,
  `SET LOCAL ROLE cl_app` per transaction; lookups by secret use `system()`. Files: the first workspace uses the
  usual folders, others `CL_FILES_DIR/<id>/...`; definitions: schema `cl_defs` / `cl_defs_<id>`.
- **Access** (`app/access.py`): data sets (labels on files) and connectors (what Claude may use), given through
  Everyone, teams, roles and people. Row rules (`semantic/access.py`) limit metric rows per data set, team or role.
  Delegate keys (`app/delegate.py`) act for an app's end users with signed identities.
- **Documents**: Docling -> passages -> BGE-M3 + BM25 in Qdrant -> re-ranker -> relevance labels, duplicates and
  unrelated passages dropped, neighbouring context (`rag/retrieve.py`).
- **Data**: tables land as Parquet (`app/landing.py`), DuckDB views (`semantic/db.py`), reviewed definitions in the
  definitions store, rule planner and compiler; nightly check (`semantic/monitor.py`).
- **Graph**: spaCy / GLiNER extraction, resolution by meaning, Leiden communities, stored in `graph_*` tables
  (`graph/kg.py`), loaded per workspace and version for answering (`graph/search.py`).
- **Sources** (`sources/`): GitHub, Postgres, MySQL, S3; synced by the worker on a schedule or a webhook.
- **Quality** (`app/quality.py`): deterministic scores after every build, trend on the Quality tab.
- **Secrets** (`app/vault.py`): Fernet with master keys from the environment, a file or a secrets manager; rotation.
