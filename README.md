# Small RAG system (Docling · BGE-M3 · Qdrant · Claude · RAGAS)

Drop files into `documents/`, run one command, ask questions. Every step writes its output
to one file in `outputs/` so you can inspect exactly what each layer did.

## Folder layout

```
documents/            your files (.pdf .docx .pptx .xlsx .html .md .txt, images)
  _access.json        optional permissions: {"salary_bands.txt": ["hr"]}  (default group: "public")
outputs/              one file per step (see below)
qdrant_data/          the Qdrant vector database (embedded, no server needed)
rag/                  one module per layer
ui/                   visual interface (python rag.py ui)
rag.py                command line
eval_questions.json   test set for RAGAS
```

## Layers → files

| # | Layer | Tool | Output file |
|---|-------|------|-------------|
| 1 | Parsing | Docling (headings, tables, pages, OCR) | `outputs/1_parsed.json` |
| 2 | Chunking | Docling HybridChunker (BGE-M3 tokenizer, 512 tok) / RecursiveCharacterTextSplitter for .txt | `outputs/2_chunks.json` |
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

## Visual interface

```powershell
.venv\Scripts\python rag.py ui                             # then open http://127.0.0.1:8000
```

A browser view of the whole pipeline. Each step has a short explanation (what happens, why it
matters, what to look for, the settings it uses) and a visual of what that step actually produced:

| Step | What you see |
|------|--------------|
| 0 Documents | Files in `documents/`, their access groups (editable), upload |
| 1 Parse | The extracted text per file, with headings and tables, pages and timing |
| 2 Chunk | Every passage as a block sized by tokens; click one to compare the passage with the text that gets embedded |
| 3 Embed | A "meaning map" of all passages (vectors projected to 2-D) and a colour fingerprint of each vector |
| 4 Index | Points stored, index settings, passages per access group and per file |
| 5 Permission filter | How many passages the asker may see, and which files |
| 6 Hybrid search | Meaning and keyword results side by side, overlaps highlighted, and where the question lands on the map |
| 7 Fusion | The fused list with each passage's position in both searches |
| 8 Re-rank | How the order changed, and the passages sent to Claude |
| 9 Answer | The answer with its citations, the exact prompt, and where the time went |
| 10 Evaluate | RAGAS scores per layer, speed and tokens, and every question |

- **Retrieval-only mode:** without `ANTHROPIC_API_KEY`, steps 1–8 still work and step 9 shows the
  prompt Claude would receive. Untick "Write the answer with Claude" to skip the paid call even with a key.
- The interface reads the files in `outputs/` and calls the same functions as the command line, so it
  shows exactly what the pipeline did. Steps run one at a time; don't run `rag.py` commands in another
  window while it is open (embedded Qdrant allows one process).
- It listens on `127.0.0.1` only and has no login: the access groups are whatever you tick, so it shows
  how permission filtering works rather than enforcing security.

## GraphRAG (optional layer on the same passages)

LLM-based GraphRAG built on top of this pipeline. It reads `outputs/2_chunks.json`, so run `ingest` first.

```
python rag.py graph-build                      # g1..g5 below
python rag.py graph-ask "question" --mode auto # auto | hybrid | local | global
python rag.py graph-eval                       # plain RAG vs graph modes, per question type
```

| Step | File | What happens |
|---|---|---|
| g1 extract | `rag/graph/extract.py`, `rag/graph/gliner_extract.py` | Entities and relationships per chunk, from a closed list of types with descriptions (`config.py`). `EXTRACT_MODE=hybrid` (default): **GLiNER2** (`fastino/gliner2.5-base-v1`, Apache 2.0, runs locally) reads every chunk with a confidence per item; the weakest chunks (two+ entities but no relationship, then lowest confidence, then long chunks where nothing was found) go to the LLM for review, up to `ESCALATE_BUDGET` (20%) of chunks. `gliner`: no LLM calls. `llm`: the LLM reads every chunk. Cached by chunk hash; out-of-schema items are fixed or dropped. |
| g2 resolve | `rag/graph/resolve.py` | Entity resolution, cheapest check first (design taken from [agentic-graphrag](https://github.com/ontogr/agentic-graphrag), Apache 2.0): your overrides → exact normalised name → number check ("GPT" vs "GPT-4", "v2" vs "v3" never match) → spelling, acronyms ("BAAI"), name + generic words ("Qdrant vector database") → name-vector similarity → tight groups → LLM for what is left, within a pair budget. Matches are reversible **links**, each with its tier and reason; "uncertain" stays undecided, and a no-match blocks any weaker match that would join the two names through a third. Fix links in `graph_resolution_overrides.json` (see the `.example`). |
| g3 communities | `rag/graph/communities.py` | Leiden (NetworkX, BSD, modularity), split again for large communities (hierarchy). |
| g4 summarize | `rag/graph/summarize.py` | Entity seen once keeps its description (no LLM); seen several times gets one merged description. Each community gets a report. |
| g5 store | `rag/graph/store.py` | Apache AGE (Postgres) as storage only. `Entity`, `Chunk`, `Community` nodes; `RELATED`, `MENTIONS`, `IN_COMMUNITY`, `CHILD_OF` edges. Chunk ids match Qdrant. `GRAPH_STORE=json` uses a JSON file + NetworkX instead. |
| g6 search | `rag/graph/search.py` | **hybrid** (default): the graph's chunks are RRF-fused with the existing dense + BM25 candidates, re-ranked by the same re-ranker, answered by the same `generate()`, plus a short "graph facts" passage. **local**: graph chunks only. **global**: map-reduce over community reports. **auto**: rule-based router. |
| g7 eval | `rag/graph/evaluate.py` | Same RAGAS judge for every mode, overall and per question type. Pass rule: hybrid ≥ RAG − 0.02 on every type. Questions: `eval_questions_graph.json` (the RAG set tagged, plus multi-hop and global questions). |

**Why hybrid and not summaries alone:** answering fact questions from LLM summaries instead of the source passages loses detail, which is the usual reason GraphRAG scores below plain RAG on single-hop questions. Hybrid only adds candidates, so if the graph finds nothing the answer is the same as plain RAG.

**Access:** entities, relationships and reports keep the chunk ids they came from. Search filters on the asker's groups before anything reaches the LLM. A merged summary or community report is used only if the user can see every chunk it was written from; otherwise the per-chunk descriptions they can see are used.

**Extraction without the LLM:** the first `graph-build` downloads the GLiNER2 model (~0.8 GB) from Hugging Face; after that extraction runs offline on the CPU. Set `EXTRACT_MODE=gliner` for no extraction LLM calls at all, or `EXTRACT_MODE=llm` for the original behaviour.

**LLM choice:** Claude by default. For a fully open-source setup, point it at any OpenAI-compatible server (Ollama, vLLM):
`GRAPH_LLM=openai GRAPH_LLM_MODEL=qwen2.5:14b GRAPH_LLM_BASE_URL=http://localhost:11434/v1`

**Apache AGE setup** (Postgres 16, once):
```
git clone -b PG16 https://github.com/apache/age && cd age && make && sudo make install
psql -c "CREATE ROLE clp LOGIN SUPERUSER PASSWORD 'clp'" -c "CREATE DATABASE graphrag OWNER clp"
psql -d graphrag -c "CREATE EXTENSION age"
```
Set `GRAPH_DSN` in `.env` if your connection differs from `postgresql://clp:clp@127.0.0.1:5432/graphrag`. AGE has no Windows build; on Windows use Docker (`apache/age` image) or `GRAPH_STORE=json`.

Resolution rules have unit tests that need no models: `python -m pytest tests -q`.

The visual interface has three more screens (11–13): the interactive graph with every extraction and merge decision, a graph answer trace, and the RAG vs GraphRAG comparison.

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

- Windows Application Control on this PC blocks the newest `pyarrow` and `xxhash` wheels; `requirements.txt` pins versions that load.
- The embedded Qdrant (`qdrant_data/`) allows one process at a time. To run a server instead:
  `docker run -p 6333:6333 qdrant/qdrant` and change `client()` in `rag/store.py` to `QdrantClient(url="http://localhost:6333")`.
- Models run on CPU; the first run downloads BGE-M3 (~2.2 GB) and the reranker.
