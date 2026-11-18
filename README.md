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
