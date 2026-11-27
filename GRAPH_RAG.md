# Graph RAG

**In the product (0.7):** the knowledge graph of each workspace is built without a language model (spaCy + GLiNER
guided by the workspace's ontology, rules for links, RapidFuzz merging, Leiden communities) and stored in plain
Postgres tables (`graph/kg.py`, migration `013_graph.sql`: `kg_entities`, `kg_relations`, `kg_entity_passages`,
`kg_communities`, `graph_state`). Each build replaces the workspace's graph in one transaction and moves its version;
answering loads the tables into an in-memory graph (NetworkX plus name and passage indexes, `graph/search.py`)
and reloads when the version moves. A link or entity is only shown when a passage behind it is visible to the
caller. Entity and community vectors stay in Qdrant. Apache AGE is no longer part of the product.

**Research history (below):** the first version, on branch `graph-rag`, stored the graph in PostgreSQL + Apache AGE
and answered with Claude. That code now lives in `experiments/graph_age/` (store, query, evaluate) and is never
imported by the product.

```
chunks ─► extract (Claude Haiku, typed entities + relations, cached per chunk) ─┐
tables ─► map rows to entities, foreign keys to relations (no LLM) ─────────────┤
                                                                                ▼
          merge duplicates (RapidFuzz + BGE-M3, Claude for unclear pairs) ─► weighted edges
          ─► Leiden communities (igraph + leidenalg, 2 levels) ─► Postgres + AGE  /  Qdrant vectors
question ─► router ─┬ lookup       ─► vector RAG
                    ├ connections  ─► local search: entities ─► AGE edges via graph ids ─► source chunks ─► rerank ─► Claude
                    ├ overview     ─► global search: communities, summaries written on first use (cached), map-reduce
                    └ sales numbers─► semantic layer
```

## Setup and commands

```powershell
docker compose -f experiments/docker-compose.graph.yml up -d   # research only: Postgres 17 + Apache AGE 1.7 on :5455
.venv\Scripts\python rag.py gr-extract --regions "Kerala,Tamil Nadu" --out outputs/gr_extract_south.jsonl
.venv\Scripts\python rag.py gr-build outputs/gr_extract_south.jsonl          # add --reuse to skip merging
.venv\Scripts\python rag.py gr-ask "Which dynasty founded by Sultan Quli ruled Telangana?" --mode local
.venv\Scripts\python rag.py gr-eval questions|run|score
```

The traditional extractors (spaCy, GLiNER) run in a separate environment outside OneDrive (`requirements-graph.txt`).

## Extraction comparison (30 hand-labelled chunks, `graph_layer/gold_extraction.json`)

| Extractor | Typed entity F1 | Relation recall | Relation precision (adjusted) |
|---|---|---|---|
| spaCy + dependency rules | 0.46 | 0.10 | 0.88 |
| GLiNER + dependency rules | 0.67 | 0.11 | 0.81 |
| Claude Haiku | 0.82 | 0.75 | n/a (raw 0.44) |
| Claude Opus | 0.85 | 0.87 | 0.97 |

"Adjusted" = the hand labels miss real relations; unmatched predictions were judged by hand.

## Notes

- Graph walks read AGE's edge tables through their start/end indexes using the seeds' graph ids:
  0.13 s instead of ~50 s for the equivalent property-matching Cypher.
- AGE graph names need 3+ characters; `count` is a reserved word in Cypher.
- Results: `outputs/gr_3_graph.json` (graph), `outputs/gr_5_eval_compare.json` (graph vs vector).
