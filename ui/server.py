"""Visual interface for the RAG pipeline.

Shows what every step produced (the files in outputs/) and explains it, and lets you run the
steps and ask questions from the browser.

    python rag.py ui            then open http://127.0.0.1:8000

The server only reads the step output files and calls the same functions as the command line,
so what you see is exactly what the pipeline did. It listens on 127.0.0.1 only.
"""
import contextlib
import io
import json
import os
import threading
import time
import traceback
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from rag import config
from rag.config import DOCS_DIR, OUT_DIR

STATIC = Path(__file__).parent / "static"
STEP_FILES = {
    "parse": "1_parsed.json", "chunk": "2_chunks.json", "embed": "3_embeddings.json",
    "index": "4_index.json", "retrieve": "5_retrieval.json", "rerank": "6_rerank.json",
    "answer": "7_answer.json", "eval": "8_eval_results.json",
    "graph": "g5_graph.json", "graph_eval": "g7_graph_eval.json",
}

app = FastAPI(title="RAG pipeline explorer", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")

# One pipeline action at a time: the embedded Qdrant store allows a single writer,
# and the steps read each other's output files.
_lock = threading.Lock()
_jobs: dict[str, dict] = {}


# ---------------------------------------------------------------- helpers

def _can_answer() -> bool:
    from rag.generate import can_answer
    return can_answer()


def _graph_llm() -> bool:
    from rag.graph import llm
    return llm.available()


def _out(name: str):
    path = OUT_DIR / name
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _access_map() -> dict:
    path = DOCS_DIR / "_access.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _save_access_map(m: dict):
    DOCS_DIR.mkdir(exist_ok=True)
    (DOCS_DIR / "_access.json").write_text(json.dumps(m, indent=2), encoding="utf-8")


class _LogLines(io.TextIOBase):
    """Collects printed lines so the browser can show the step's own progress messages."""

    def __init__(self, lines: list):
        self.lines, self._part = lines, ""

    def write(self, s):
        self._part += s
        while "\n" in self._part:
            line, self._part = self._part.split("\n", 1)
            if line.strip():
                self.lines.append(line)
        return len(s)


def _start_job(kind: str, fn) -> dict:
    job = {"id": uuid.uuid4().hex[:10], "kind": kind, "status": "queued", "log": [],
           "result": None, "error": None, "seconds": None}
    _jobs[job["id"]] = job

    def run():
        with _lock:
            job["status"], t0 = "running", time.perf_counter()
            try:
                with contextlib.redirect_stdout(_LogLines(job["log"])):
                    job["result"] = fn()
                job["status"] = "done"
            except SystemExit as e:           # the steps exit with a readable message, e.g. "run the earlier step first"
                job["status"], job["error"] = "error", str(e)
            except Exception as e:            # noqa: BLE001 - surface any failure in the browser
                job["status"], job["error"] = "error", f"{type(e).__name__}: {e}"
                job["log"].extend(traceback.format_exc().splitlines()[-6:])
            job["seconds"] = round(time.perf_counter() - t0, 2)

    threading.Thread(target=run, daemon=True).start()
    return {"job": job["id"]}


# ------------------------------------------------- meaning map (2-D projection)

_proj_cache: dict = {}


def _projection():
    """Project the 1024-number chunk vectors onto their two main directions (PCA) for a 2-D map.
    Cached until 3_embeddings.json changes."""
    path = OUT_DIR / STEP_FILES["embed"]
    if not path.exists():
        return None
    mtime = path.stat().st_mtime
    if _proj_cache.get("mtime") == mtime:
        return _proj_cache
    import numpy as np

    emb = json.loads(path.read_text(encoding="utf-8"))
    ids = [r["chunk_id"] for r in emb["vectors"]]
    X = np.array([r["dense"] for r in emb["vectors"]], dtype=float)
    mean = X.mean(axis=0)
    if len(X) >= 3:
        _, S, Vt = np.linalg.svd(X - mean, full_matrices=False)
        comps = Vt[:2]
        var = (S ** 2) / max(float((S ** 2).sum()), 1e-12)
        explained = [round(float(v), 3) for v in var[:2]]
    else:                                     # too few points for a meaningful projection
        comps = np.zeros((2, X.shape[1]))
        explained = [0.0, 0.0]
    pts = (X - mean) @ comps.T
    _proj_cache.clear()
    _proj_cache.update(mtime=mtime, ids=ids, mean=mean, comps=comps, explained=explained,
                       points={i: [round(float(p[0]), 4), round(float(p[1]), 4)] for i, p in zip(ids, pts)},
                       emb=emb)
    return _proj_cache


def _project_query(question: str):
    proj = _projection()
    if not proj:
        return None
    from rag.models import embed_dense
    import numpy as np

    q = np.array(embed_dense([question])[0], dtype=float)
    p = (q - proj["mean"]) @ proj["comps"].T
    return [round(float(p[0]), 4), round(float(p[1]), 4)]


# ---------------------------------------------------------------- pages

@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/state")
def state():
    access = _access_map()
    docs = []
    if DOCS_DIR.exists():
        for p in sorted(DOCS_DIR.iterdir()):
            if p.is_file() and not p.name.startswith("_"):
                docs.append({"name": p.name, "size": p.stat().st_size, "access": access.get(p.name, ["public"])})
    outputs = {}
    for step, name in STEP_FILES.items():
        path = OUT_DIR / name
        outputs[step] = {"exists": path.exists(), "mtime": path.stat().st_mtime if path.exists() else None}
    chunks = _out(STEP_FILES["chunk"]) or []
    groups = sorted({g for d in docs for g in d["access"]} | {g for c in chunks for g in c.get("access", [])} | {"public"})
    examples = []
    qfile = config.ROOT / "eval_questions.json"
    if qfile.exists():
        try:
            examples = [q["question"] for q in json.loads(qfile.read_text(encoding="utf-8"))][:8]
        except (json.JSONDecodeError, KeyError, TypeError):
            examples = []
    graph_examples = []
    gfile = config.ROOT / "eval_questions_graph.json"
    if gfile.exists():
        try:
            graph_examples = [q["question"] for q in json.loads(gfile.read_text(encoding="utf-8"))
                              if q.get("type") in ("multi-hop", "global")][:6]
        except (json.JSONDecodeError, KeyError, TypeError):
            graph_examples = []
    return {
        "documents": docs,
        "examples": examples,
        "graph_examples": graph_examples,
        "outputs": outputs,
        "groups": groups,
        "has_api_key": bool(os.environ.get("ANTHROPIC_API_KEY")),      # Claude: needed for the RAGAS judge
        "can_answer": _can_answer(),                                   # Claude or a local model
        "answer_model": config.CLAUDE_MODEL if os.environ.get("ANTHROPIC_API_KEY") else (
            config.GRAPH_LLM_MODEL if config.GRAPH_LLM == "openai" else None),
        "graph_llm_available": _graph_llm(),
        "busy": _lock.locked(),
        "config": {
            "embed_model": config.EMBED_MODEL, "embed_dim": config.EMBED_DIM,
            "chunk_max_tokens": config.CHUNK_MAX_TOKENS, "sparse_model": config.SPARSE_MODEL,
            "rerank_model": config.RERANK_MODEL, "candidates_per_retriever": config.CANDIDATES_PER_RETRIEVER,
            "fused_top_k": config.FUSED_TOP_K, "final_top_k": config.FINAL_TOP_K,
            "claude_model": config.CLAUDE_MODEL, "collection": config.COLLECTION,
            "graph_llm": __import__("rag.graph.llm", fromlist=["describe"]).describe(), "graph_store": config.GRAPH_STORE,
            "extract_mode": config.EXTRACT_MODE, "gliner_model": config.GLINER_MODEL,
            "escalate_budget": config.ESCALATE_BUDGET, "escalate_below": config.ESCALATE_BELOW,
            "entity_types": config.ENTITY_TYPES, "relation_types": config.RELATION_TYPES,
            "resolve_match": config.RESOLVE_EMBED_MATCH, "resolve_no_match": config.RESOLVE_EMBED_NO_MATCH,
            "resolve_llm_pairs": config.RESOLVE_MAX_LLM_PAIRS,
            "graph_hops": config.GRAPH_HOPS, "graph_chunks": config.GRAPH_CHUNKS,
        },
    }


# ---------------------------------------------------------------- step outputs

@app.get("/api/step/parse")
def step_parse():
    data = _out(STEP_FILES["parse"])
    if data is None:
        return {"exists": False}
    docs = []
    for d in data:
        md = d.get("markdown", "")
        docs.append({k: d.get(k) for k in ("source", "access", "parser", "pages", "tables", "seconds")}
                    | {"markdown": md[:20000], "markdown_chars": len(md), "truncated": len(md) > 20000})
    return {"exists": True, "documents": docs}


@app.get("/api/step/chunk")
def step_chunk():
    data = _out(STEP_FILES["chunk"])
    if data is None:
        return {"exists": False}
    keep = ("chunk_id", "source", "access", "headings", "pages", "text", "embed_text", "n_tokens")
    return {"exists": True, "chunks": [{k: c.get(k) for k in keep} for c in data[:3000]], "total": len(data)}


@app.get("/api/step/embed")
def step_embed():
    proj = _projection()
    if proj is None:
        return {"exists": False}
    emb = proj["emb"]
    chunks = {c["chunk_id"]: c for c in (_out(STEP_FILES["chunk"]) or [])}
    items = []
    for r in emb["vectors"]:
        c = chunks.get(r["chunk_id"], {})
        items.append({
            "chunk_id": r["chunk_id"], "source": c.get("source"), "headings": c.get("headings", []),
            "snippet": (c.get("text") or "")[:160], "point": proj["points"][r["chunk_id"]],
            "dense_preview": [round(v, 4) for v in r["dense"][:256]],
            "sparse_terms": len(r["sparse"]["indices"]),
        })
    return {"exists": True, "dense_model": emb.get("dense_model"), "dense_dim": emb.get("dense_dim"),
            "sparse_model": emb.get("sparse_model"), "count": emb.get("count"),
            "dense_seconds": emb.get("dense_seconds"), "sparse_seconds": emb.get("sparse_seconds"),
            "explained": proj["explained"], "items": items}


@app.get("/api/step/index")
def step_index():
    data = _out(STEP_FILES["index"])
    if data is None:
        return {"exists": False}
    chunks = _out(STEP_FILES["chunk"]) or []
    per_group, per_source = {}, {}
    for c in chunks:
        for g in c.get("access", []):
            per_group[g] = per_group.get(g, 0) + 1
        per_source[c["source"]] = per_source.get(c["source"], 0) + 1
    return {"exists": True, "index": data, "per_group": per_group, "per_source": per_source}


@app.get("/api/step/eval")
def step_eval():
    data = _out(STEP_FILES["eval"])
    return {"exists": data is not None, **(data or {})}


@app.get("/api/step/graph")
def step_graph():
    """Everything the 'Knowledge graph' screen needs, from g1..g5."""
    g5 = _out("g5_graph.json")
    if g5 is None:
        return {"exists": False}
    g1, g2, g3, g4 = (_out(n) or {} for n in ("g1_extraction.json", "g2_resolution.json",
                                               "g3_communities.json", "g4_summaries.json"))
    examples = [{"chunk_id": c["chunk_id"], "source": c["source"], "entities": c["entities"],
                 "relations": c["relations"], "problems": c["problems"], "extractor": c.get("extractor", "llm"),
                 "escalation": c.get("escalation")}
                for c in g1.get("chunks", [])][:60]
    graph = g5["graph"]
    return {
        "exists": True, "stored_in": g5["stored_in"], "counts": g5["counts"], "sample_paths": g5.get("sample_paths", []),
        "extraction": {"stats": g1.get("stats"), "examples": examples},
        "resolution": {"stats": g2.get("stats"), "links": g2.get("links", [])[:300]},
        "communities": {"stats": g3.get("stats"), "list": g3.get("communities", [])},
        "summaries": {"stats": g4.get("stats"), "reports": g4.get("reports", [])},
        "graph": {"entities": [{k: e[k] for k in ("id", "name", "type", "aliases", "summary", "chunk_ids", "community")}
                               for e in graph["entities"]],
                  "relations": graph["relations"], "communities": graph["communities"]},
    }


@app.get("/api/step/graph_eval")
def step_graph_eval():
    data = _out("g7_graph_eval.json")
    return {"exists": data is not None, **(data or {})}


class GraphAskIn(BaseModel):
    question: str
    mode: str = "auto"
    groups: list[str] | None = None
    sources: list[str] | None = None
    generate: bool = False


@app.post("/api/graph/ask")
def graph_ask_route(body: GraphAskIn):
    question = body.question.strip()
    if not question:
        raise HTTPException(400, "Type a question first.")
    if body.mode not in ("auto", "local", "global", "hybrid"):
        raise HTTPException(400, "Unknown mode")
    if _out("g5_graph.json") is None:
        raise HTTPException(400, "Build the knowledge graph first.")
    if body.generate and not _can_answer():
        raise HTTPException(400, "Writing the answer needs an LLM. Set ANTHROPIC_API_KEY, or GRAPH_LLM=openai for a "
                                 "local model, or run retrieval only.")
    groups = body.groups or ["public"]

    def run():
        from rag.graph.search import ask_graph
        return ask_graph(question, body.mode, groups, body.sources or None, save=True, answer=body.generate)

    return _start_job("graph-ask", run)


@app.get("/api/history")
def history(limit: int = 15):
    path = OUT_DIR / "query_log.jsonl"
    if not path.exists():
        return {"items": []}
    lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    items = []
    for line in reversed(lines):
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        items.append({"time": r.get("time"), "question": r.get("question"), "found": r.get("found"),
                      "answer": (r.get("answer") or "")[:240], "total_s": r.get("timings_s", {}).get("total")})
    return {"items": items}


# ---------------------------------------------------------------- actions

@app.post("/api/run/{step}")
def run_step(step: str):
    def ingest():
        print("[1] Parsing"); from rag.parse import parse_all; parse_all()
        print("[2] Chunking"); from rag.chunk import chunk_all; chunk_all()
        print("[3] Embedding"); from rag.embed import embed_all; embed_all()
        print("[4] Indexing in Qdrant"); from rag.store import build_index; build_index()

    def one(module, fn):
        def go():
            mod = __import__(f"rag.{module}", fromlist=[fn])
            getattr(mod, fn)()
        return go

    actions = {
        "ingest": ingest,
        "parse": one("parse", "parse_all"), "chunk": one("chunk", "chunk_all"),
        "embed": one("embed", "embed_all"), "index": one("store", "build_index"),
        "eval": one("evaluate", "run_eval"),
        "graph": lambda: __import__("rag.graph.build", fromlist=["build_graph"]).build_graph(),
        "graph_eval": lambda: __import__("rag.graph.evaluate", fromlist=["run_graph_eval"]).run_graph_eval(),
    }
    if step not in actions:
        raise HTTPException(404, f"Unknown step: {step}")
    if step in ("eval", "graph_eval") and not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(400, "Evaluation uses Claude as the judge. Set ANTHROPIC_API_KEY in .env first.")
    if step == "graph" and config.EXTRACT_MODE == "llm" and not _graph_llm():
        raise HTTPException(400, "EXTRACT_MODE=llm needs an LLM. Set ANTHROPIC_API_KEY, GRAPH_LLM=openai for a local "
                                 "model, or use EXTRACT_MODE=hybrid / gliner (works without any LLM).")
    return _start_job(step, actions[step])


class AskIn(BaseModel):
    question: str
    groups: list[str] | None = None
    sources: list[str] | None = None
    generate: bool = False


@app.post("/api/ask")
def ask_route(body: AskIn):
    question = body.question.strip()
    if not question:
        raise HTTPException(400, "Type a question first.")
    if body.generate and not _can_answer():
        raise HTTPException(400, "Writing the answer needs an LLM. Set ANTHROPIC_API_KEY, or GRAPH_LLM=openai for a "
                                 "local model, or run retrieval only.")
    groups = body.groups or ["public"]
    sources = body.sources or None

    def run():
        from rag.generate import SYSTEM, format_passages

        if body.generate:
            from rag.generate import ask
            record = ask(question, groups, sources, save=True)
            passages = [dict(c, headings=[]) for c in record["contexts"]]
            answer = {k: record.get(k) for k in ("found", "answer", "citations", "model", "usage")}
            timings = record["timings_s"]
        else:
            from rag.retrieve import retrieve
            ret = retrieve(question, groups, sources, save=True)
            passages, answer, timings = ret["passages"], None, dict(ret["timings_s"])

        retrieval = _out(STEP_FILES["retrieve"]) or {}
        rerank = _out(STEP_FILES["rerank"]) or {}
        chunks = _out(STEP_FILES["chunk"]) or []
        visible = [c for c in chunks if set(c.get("access", [])) & set(groups)
                   and (not sources or c["source"] in sources)]
        prompt = f"<passages>\n{format_passages(rerank.get('passages', passages))}\n</passages>\n\nQuestion: {question}"
        return {
            "question": question, "groups": groups, "sources": sources, "generated": body.generate,
            "visibility": {
                "total": len(chunks), "visible": len(visible),
                "hidden": len(chunks) - len(visible),
                "visible_sources": sorted({c["source"] for c in visible}),
                "hidden_files": len({c["source"] for c in chunks} - {c["source"] for c in visible}),
            },
            "dense_top": retrieval.get("dense_top", []), "bm25_top": retrieval.get("bm25_top", []),
            "rrf_fused": retrieval.get("rrf_fused", []), "final": rerank.get("passages", passages),
            "timings": timings, "answer": answer,
            "prompt_preview": {"system": SYSTEM, "user": prompt},
            "query_point": _project_query(question),
        }

    return _start_job("ask", run)


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Unknown job")
    return job


@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...), groups: str = Form("public")):
    DOCS_DIR.mkdir(exist_ok=True)
    wanted = [g.strip() for g in groups.split(",") if g.strip()] or ["public"]
    access, saved = _access_map(), []
    for f in files:
        name = Path(f.filename or "").name
        if not name or name.startswith("_"):
            raise HTTPException(400, f"Not allowed as a document name: {f.filename!r}")
        (DOCS_DIR / name).write_bytes(await f.read())
        if wanted == ["public"]:
            access.pop(name, None)
        else:
            access[name] = wanted
        saved.append(name)
    _save_access_map(access)
    return {"saved": saved, "access": wanted}


class AccessIn(BaseModel):
    name: str
    groups: list[str]


@app.post("/api/access")
def set_access(body: AccessIn):
    name = Path(body.name).name
    if not (DOCS_DIR / name).is_file():
        raise HTTPException(404, "No such document")
    groups = [g.strip() for g in body.groups if g.strip()] or ["public"]
    access = _access_map()
    if groups == ["public"]:
        access.pop(name, None)
    else:
        access[name] = groups
    _save_access_map(access)
    return {"name": name, "access": groups, "note": "Re-run the preparation steps so the index picks this up."}


def serve(host: str = "127.0.0.1", port: int = 8000):
    import uvicorn

    print(f"RAG pipeline explorer: http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")
