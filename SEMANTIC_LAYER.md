# Semantic layer (branch `semantic-layer`)

The RAG system answers questions from **documents**. The semantic layer answers questions about
**numbers in tables** ("net revenue by region last quarter"), using business definitions that are
written once, reviewed, and tested. A router sends each question to the right side.

```
question ─► router (Claude) ─┬─ documents ─► RAG: Qdrant hybrid search ─► rerank ─► Claude answer + citations
                             └─ data ──────► semantic layer:
                                              catalog + synonyms + verified examples + business rules
                                              ─► Claude picks metric/dimension NAMES (never SQL)
                                              ─► filter values checked (synonyms + RapidFuzz)
                                              ─► compiler writes SQL from the reviewed definitions
                                              ─► DuckDB runs it ─► Claude states the result
```

## In the app (0.5): per workspace, in Postgres

Inside Context Layer each workspace has its own semantic layer, stored in Postgres (`sl_definitions`), not in these
files. The YAML below is the format for **import and export** and the seed of a server's first workspace.

| | |
|---|---|
| Where definitions live | `sl_definitions` per workspace: kind (entity, join, metric, synonym, value_list, verified, instructions), definition, status, owner, source, domain, version |
| History | every change kept in `sl_history` (who, when, the previous definition); the layer version bumps on each change, so planners and caches follow at once |
| Status | `draft` (not used by agents), `approved`, `needs_review` (a check failed; hidden), `retired` |
| Who approves (`semantic/policy.py`) | templates (e.g. GitHub) and certified imports: as declared; structure, synonyms and value lists found in the data: automatically; new metrics and changed formulas: an owner or admin |
| Where it comes from | uploads and connectors (tables -> profile -> drafted entities, joins and metrics), connector templates, YAML import (`POST /api/v1/semantic/import`), dbt-style reviews via export -> git -> import |
| Kept correct | nightly check (`semantic/monitor.py`): every approved metric is run; failures become `needs_review` with the reason and come back when they work again |
| Learns | counting questions no metric matched are recorded; repeated unknown words that look like a metric become synonym drafts (`semantic/learn.py`) |
| Row access | `sl_access_rules`: a group sees only rows whose column has allowed values (e.g. a repository); applied inside every query (`semantic/db.py connect_for`) |
| Data | each workspace's tables (uploads, GitHub activity, Postgres snapshots, fact tables read from documents) in its folder, or a Postgres database attached live; one cached DuckDB per workspace |
| Facts from documents (0.6) | approved facts become `fact_contracts`, `fact_invoices`, `fact_meeting_decisions`, `fact_meeting_actions` (Parquet) with keys of matched database rows, drafted like any table and joinable in DuckDB |

## Folder layout

```
data/                     raw tables (CSV/Excel/Parquet), one file per table - sample sales data
reference/                trusted numbers to reconcile against (finance_monthly_report.csv)
semantic_layer/           THE REVIEWED LAYER (source of truth, changed only by pull request)
  model.yaml              entities, dimensions, measures, joins, descriptions
  metrics.yaml            every business number, defined once (formula, filters, owner, status)
  enrichment.yaml         synonyms (sales -> net_revenue, Bangalore -> Bengaluru), value-list columns
  verified_queries.yaml   checked question -> request pairs (examples for Claude, compiled in CI)
  instructions.md         business rules: fiscal year, default meanings, wording
semantic/                 the code, one module per phase
sl_store/                 built enrichment store (DuckDB); rebuilt by sl-enrich, not committed
tests/                    the review checks CI runs (+ golden SQL files)
```

## The five build phases

| # | Phase | Command | Tools | Output |
|---|---|---|---|---|
| 1 | Profiling | `rag.py sl-profile` | DuckDB (counts, nulls, distinct, min/max, top values), SQL containment checks for joins | `outputs/sl_1_profile.json` |
| 2 | Modelling | `rag.py sl-model` | Pydantic schema, ruamel.yaml, networkx join graph, Claude drafts labels/descriptions | `outputs/sl_2_model.draft.yaml` → reviewed into `semantic_layer/model.yaml` |
| 3 | Metrics | `rag.py sl-metrics [--draft]` | Pydantic, SQLGlot (formulas parse, columns exist), pandas reconciliation vs. finance, Claude proposes drafts | `outputs/sl_3_metrics_check.json` (+ `sl_3_metrics.draft.yaml`) |
| 4 | Enrichment | `rag.py sl-enrich` | DuckDB store (synonyms, value lists, verified queries, instructions), BGE-M3 + Qdrant collection `semantic`, SQLGlot/DuckDB check of every verified query | `sl_store/enrichment.duckdb`, `outputs/sl_4_enrichment.json` |
| 5 | Review | `python -m pytest tests -q` | Git pull requests, pytest, GitHub Actions (`.github/workflows/semantic-layer.yml`), optional pre-commit | merge blocked if a check fails |

Differences from the original plan: enrichment is stored in **DuckDB + Qdrant** instead of
PostgreSQL + pgvector, to stay with the RAG system's stack and need no server.

## Using it

```powershell
.venv\Scripts\python rag.py sl-data          # sample tables (skip if you put your own files in data/)
.venv\Scripts\python rag.py sl-profile
.venv\Scripts\python rag.py sl-model         # only drafts; edit semantic_layer/model.yaml by hand
.venv\Scripts\python rag.py sl-metrics
.venv\Scripts\python rag.py sl-enrich
.venv\Scripts\python rag.py ask "What was the AOV for corporate customers in Bangalore last quarter?"
.venv\Scripts\python rag.py sl-sql '{\"metrics\": [\"net_revenue\"], \"dimensions\": [\"regions.region_name\"]}'
.venv\Scripts\python -m pytest tests -q
```

`ask` and `chat` route automatically; `ask --route docs|data` forces a side.

## Rules the compiler enforces

- **Claude never writes SQL.** It returns a `SemanticQuery` (metric names, dimension references, filters,
  dates). Unknown names are rejected and sent back once for correction.
- **Each metric's filters travel with it** (`SUM(x) FILTER (WHERE status <> 'cancelled')`), so metrics with
  different rules can share one query.
- **No fan-out joins.** Joins are followed only from the "many" side to the "one" side
  (order_items → orders → customers → regions). Asking for `net_revenue` by `products.category` is refused,
  because one order has several products and the total would be inflated; use `product_revenue`.
- **Metrics on different tables** (orders vs. order_items) are computed in separate sub-queries and joined
  on the dimensions (NULL-safe, so the "unknown region" row lines up).
- **Filter values are quoted by SQLGlot**, never pasted into SQL; a test checks an injection attempt returns 0 rows.
- Time grains: `__day`, `__week`, `__month`, `__quarter`, `__year`, `__fiscal_year` (April-March, `FY2025-26`).

## Changing a definition

1. Edit the YAML in `semantic_layer/` on a branch.
2. Run `python -m pytest tests -q`. If a golden SQL file changes on purpose, run with `--update-golden` and review the diff.
3. Open a pull request; the data owner reviews; CI must pass.
4. After merging, run `rag.py sl-enrich` so the store and Qdrant pick up the change.

## Scale notes

- The catalog is small (13 metrics, 11 dimensions), so Claude gets **all of it** and no embedding model is
  loaded per question. Above 150 items, or with `SL_VECTOR_SEARCH=1`, the closest items are retrieved from
  the Qdrant `semantic` collection instead.
- Tables are loaded into in-memory DuckDB (fine for MBs). For large data, use views over Parquet or point
  DuckDB at the warehouse.

## Evaluation (`rag.py sl-eval`)

The gold set `semantic_layer/eval_questions.yaml` holds 60 questions: simple, breakdown, filter, top-N, synonym,
typo, multi-metric, disallowed-split, fiscal-date, unanswerable and documents-vs-data routing. Each data
question carries the request the planner should produce. "Today" is pinned to the set's `as_of` date.

| Metric | How it is scored |
|---|---|
| execution_accuracy | gold request and planner request are both run; same groups and values (deterministic) |
| request_exact (+ metrics/dimensions/filters/dates) | field-by-field comparison with the gold request |
| refusal_accuracy | unanswerable questions are declined, not guessed |
| route_accuracy | documents-vs-data router |
| number_faithfulness | every number in the written answer is in the result rows (rounding, %, lakh/crore/million, totals allowed) |
| efficiency | seconds per question, Claude calls, tokens, compile retries |

```powershell
.venv\Scripts\python rag.py sl-eval --tag baseline
.venv\Scripts\python rag.py sl-eval --tag haiku --planner claude-haiku-5-5
```

Accuracy and cost helpers measured with it:
- `semantic/dates.py`: deterministic date resolution (fiscal years, quarters, halves, "since", "last N days").
- Template answers for one-row results: no model call, so numbers can't be misquoted.
- Number guard: an unfaithful summary is regenerated once, then replaced by the table.
- Plan cache in `sl_store/plan_cache.json`, keyed on question, day, model and a hash of the layer files.
- `semantic/quality.py` data checks (freshness, duplicate keys, orphan rows), reported with every answer.
- Rankings (`limit`) leave out the unknown group, so orphan rows cannot be "the top region".
- Result tables are shown to the summariser with full numbers, not scientific notation.
