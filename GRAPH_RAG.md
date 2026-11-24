# Graph RAG (branch `graph-rag`)

A knowledge graph built from the documents (plus the sales tables), stored in PostgreSQL + Apache AGE,
used to answer connection (multi-hop) and big-picture questions next to the vector RAG and the semantic layer.

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
docker compose -f docker-compose.graph.yml up -d          # Postgres 17 + Apache AGE 1.7 on localhost:5455
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
