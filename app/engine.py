"""Adapter between the app and the three engines (documents RAG, semantic layer, knowledge graph).
The engines stay usable on their own from rag.py; this module only reads their files and calls
their entry points, adding access control and the per-agent capabilities."""
import json
import re
import shutil
import threading
import time
from pathlib import Path

import yaml

from rag.config import DOCLING_EXTS, DOCS_DIR, OUT_DIR, PLAINTEXT_EXTS, RECORD_EXTS
from semantic import config as SC

DOC_EXTS = DOCLING_EXTS | PLAINTEXT_EXTS | RECORD_EXTS
ACCESS_FILE = DOCS_DIR / "_access.json"
_GROUP_RE = re.compile(r"^(layer:[a-z0-9:_-]{1,60}|[a-z0-9][a-z0-9_-]{0,39})$")   # layer:<id> = a person's layer
_SAFE_NAME = re.compile(r"[^\w.\- ()]+")


def _json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def iso(ts: float | None) -> str | None:
    from datetime import datetime, timezone
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def clean_groups(groups) -> list[str]:
    out = sorted({g.strip().lower() for g in groups if str(g).strip()})
    bad = [g for g in out if not _GROUP_RE.match(g)]
    if bad:
        raise ValueError(f"Group names use lowercase letters, digits, '-' or '_' (not: {', '.join(bad)}).")
    return out


def safe_filename(name: str) -> str:
    name = Path(name).name
    name = _SAFE_NAME.sub("_", name).strip(" .")
    if not name or name.startswith("_"):
        raise ValueError("That file name can't be used.")
    return name[:150]


# ------------------------------------------------------------------ documents

def access_map() -> dict:
    return _json(ACCESS_FILE, {}) or {}


def _save_access(m: dict):
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = ACCESS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(m, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(ACCESS_FILE)


def _chunk_stats() -> tuple[dict, float | None]:
    p = OUT_DIR / "2_chunks.json"
    chunks = _json(p, []) or []
    per = {}
    for c in chunks:
        per[c["source"]] = per.get(c["source"], 0) + 1
    index_time = _mtime(OUT_DIR / "4_index.json")
    return per, index_time


def documents() -> list[dict]:
    amap = access_map()
    per, index_time = _chunk_stats()
    out = []
    if DOCS_DIR.exists():
        for p in sorted(DOCS_DIR.iterdir(), key=lambda x: x.name.lower()):
            if not p.is_file() or p.name.startswith(("_", ".")) or p.suffix.lower() not in DOC_EXTS:
                continue
            m = p.stat().st_mtime
            if p.name not in per:
                status = "new"
            elif index_time is None or m > index_time:
                status = "changed"
            else:
                status = "ready"
            out.append({"name": p.name, "size": p.stat().st_size, "modified": iso(m), "groups": amap.get(p.name, ["public"]),
                        "passages": per.get(p.name, 0), "status": status})
    return out


def save_document(filename: str, data: bytes, groups: list[str]) -> dict:
    name = safe_filename(filename)
    if Path(name).suffix.lower() not in DOC_EXTS:
        raise ValueError(f"{name}: unsupported type. Use one of {', '.join(sorted(DOC_EXTS))}.")
    groups = clean_groups(groups) or ["public"]
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / name).write_bytes(data)
    m = access_map()
    m[name] = groups
    _save_access(m)
    return {"name": name, "groups": groups}


def set_document_groups(name: str, groups: list[str]) -> list[str]:
    if not (DOCS_DIR / safe_filename(name)).exists():
        raise KeyError(name)
    groups = clean_groups(groups)
    if not groups:
        raise ValueError("A document needs at least one group.")
    m = access_map()
    m[name] = groups
    _save_access(m)
    return groups


def delete_document(name: str):
    p = DOCS_DIR / safe_filename(name)
    if not p.exists():
        raise KeyError(name)
    p.unlink()
    m = access_map()
    m.pop(p.name, None)
    _save_access(m)


def all_groups() -> list[str]:
    g = {"public"}
    for d in documents():
        g.update(d["groups"])
    return sorted(g)


# ------------------------------------------------------------------ data tables and metrics

def tables() -> list[dict]:
    prof = _json(SC.OUT_DIR / "sl_1_profile.json", {}) or {}
    pt = prof.get("tables", {}) if isinstance(prof, dict) else {}
    ptables = pt if isinstance(pt, dict) else {t.get("name") or t.get("table"): t for t in pt}
    out = []
    from app.landing import manifest
    man = manifest()
    if SC.DATA_DIR.exists():
        for p in sorted(SC.DATA_DIR.iterdir()):
            if p.suffix.lower() in SC.TABLE_EXTS and not p.name.startswith((".", "_")):
                t = ptables.get(p.stem, {})
                m = man.get(p.stem) or {}
                out.append({"name": p.name, "table": p.stem, "size": p.stat().st_size, "modified": iso(p.stat().st_mtime),
                            "rows": t.get("rows") or t.get("row_count") or m.get("rows"),
                            "columns": len(t.get("columns", []) or []) or len(m.get("columns") or []),
                            "format": p.suffix.lstrip("."), "source": m.get("source"), "landed": m.get("landed")})
    return out


def save_table(filename: str, data: bytes) -> list[str]:
    """CSV, Excel (every sheet), JSON / JSON Lines (nested fields flattened) or Parquet -> tables."""
    from semantic.ingest import ACCEPTED, save_upload
    name = safe_filename(filename)
    if Path(name).suffix.lower() not in ACCEPTED:
        raise ValueError(f"{name}: use CSV, Excel (.xlsx), JSON, JSON Lines or Parquet.")
    SC.DATA_DIR.mkdir(parents=True, exist_ok=True)
    return save_upload(name, data)


def metrics() -> dict:
    def load(path):
        from semantic.yamlio import _plain, read
        try:
            return _plain(read(path)) or {}
        except (OSError, ValueError, yaml.YAMLError, RuntimeError):
            return {}
    m, model = load(SC.METRICS_FILE), load(SC.MODEL_FILE)
    dims = []
    for ent in model.get("entities", []) or []:
        for d in ent.get("dimensions", []) or []:
            dims.append({"name": f"{ent.get('name')}.{d.get('name')}", "label": d.get("label") or d.get("name"),
                         "description": d.get("description", "")})
    keep = ("name", "label", "description", "type", "format", "owner", "status", "expression", "filters")
    return {"metrics": [{k: x.get(k) for k in keep} for x in m.get("metrics", []) or []], "dimensions": dims,
            "reviewed_in": "semantic_layer/ (changed by pull request, checked by tests)"}


# ------------------------------------------------------------------ knowledge graph

def graph_overview() -> dict | None:
    rep = _json(OUT_DIR / "gr_3_graph.json")
    if not rep:
        return None
    return {k: rep.get(k) for k in ("nodes", "edges", "entities", "relations", "chunks", "entities_by_type",
                                    "relations_by_type", "isolated_entities", "top_degree")} | \
           {"built": iso(_mtime(OUT_DIR / "gr_3_graph.json"))}


def graph_explore(q: str = "", limit: int = 120, groups: list[str] | None = None) -> dict:
    """Entities and relations for the explorer, from the merged graph the build saved. groups: only what passages
    of these groups support (names, descriptions and link sentences included); None: everything (company admins)."""
    from graph.access import visible
    from graph.search import _seen, ent_view, rel_evidence
    from graph import kg
    g = (kg.load() if kg.in_db() else None) or _json(OUT_DIR / "_graph_build.json")
    if not g:
        return {"entities": [], "relations": [], "total": 0}
    ents = [e for e in g.get("entities", []) if groups is None or _seen(e.get("chunk_ids"), groups, visible)]
    shown = {e["id"] for e in ents}
    rels = [r for r in g.get("relations", []) if r["source_id"] in shown and r["target_id"] in shown and
            (groups is None or _seen(r.get("chunk_ids"), groups, visible))]
    views = {e["id"]: ent_view(e, groups, visible) for e in ents}
    if q:
        ql = q.lower()
        hit = [e for e in ents if ql in views[e["id"]]["name"].lower()]
        ids = {e["id"] for e in hit[:20]}
        near = {r["target_id"] for r in rels if r["source_id"] in ids} | {r["source_id"] for r in rels if r["target_id"] in ids}
        keep = ids | near
    else:
        keep = {e["id"] for e in sorted(ents, key=lambda e: -e.get("degree", 0))[:limit]}
    by_id = {e["id"]: e for e in ents}
    keep = set(list(keep)[: limit * 2])
    return {
        "total": len(ents),
        "entities": [{"id": i, "name": views[i]["name"], "type": by_id[i]["type"], "degree": by_id[i].get("degree", 0),
                      "description": views[i]["description"][:400],
                      "community": by_id[i].get("community_l0"), "passages": len(by_id[i].get("chunk_ids", []) or [])}
                     for i in keep if i in by_id],
        "relations": [{"source": r["source_id"], "target": r["target_id"], "type": r["type"], "weight": r.get("weight", 1),
                       "description": rel_evidence(r, groups, visible)}
                      for r in rels if r["source_id"] in keep and r["target_id"] in keep][:600],
    }


def postgres_ok() -> tuple[bool, str]:
    """The app database (accounts, access, the graph tables). Apache AGE is no longer used (0.9)."""
    try:
        from app import db as D
        if not D.postgres():
            return False, "not configured (CL_DATABASE_URL): accounts are in var/app.db, the graph in a file"
        with D.pg_tx() as c:
            c.execute("SELECT 1")
        return True, "connected"
    except Exception as e:                                     # noqa: BLE001 - reported to the admin as text
        return False, f"{type(e).__name__}: {str(e).splitlines()[0][:160]}"


# ------------------------------------------------------------------ readiness

def status() -> dict:
    docs = documents()
    pg_ok, pg_msg = postgres_ok()
    graph = graph_overview()
    return {
        "documents": {"count": len(docs), "ready": sum(d["status"] == "ready" for d in docs),
                      "pending": sum(d["status"] != "ready" for d in docs),
                      "passages": sum(d["passages"] for d in docs), "indexed": iso(_chunk_stats()[1])},
        "data": {"tables": len(tables()), "metrics": len(metrics()["metrics"]), "ready": SC.STORE_DB.exists(),
                 "prepared": iso(_mtime(SC.STORE_DB))},
        "graph": {"ready": graph_ready(), "built": graph.get("built") if graph else None,
                  "entities": graph.get("entities") if graph else 0, "relations": graph.get("relations") if graph else 0},
        "postgres": {"ok": pg_ok, "message": pg_msg},
    }


# ------------------------------------------------------------------ jobs (run in the background by app.jobs)

def prepare_documents():
    print("[1/4] Reading documents"); from rag.parse import parse_all; parse_all()
    print("[2/4] Splitting into passages"); from rag.chunk import chunk_all; chunk_all()
    print("[3/4] Embedding"); from rag.embed import embed_all; embed_all()
    print("[4/4] Building the search index"); from rag.store import build_index; build_index()
    from graph.access import chunk_access
    chunk_access()


DATA_IDLE = threading.Event()        # cleared while metric tables are rebuilt (metric questions wait briefly)
DATA_IDLE.set()


def prepare_data():
    DATA_IDLE.clear()
    try:
        _prepare_data()
    finally:
        DATA_IDLE.set()


def _prepare_data():
    print("[1/4] Profiling tables"); from semantic.profile import profile_all; profile_all()
    print("[2/4] Drafting definitions for new tables (review them on the Metrics page)")
    from semantic.auto_model import draft
    d = draft()
    print(f"  new: {len(d['entities'])} tables, {len(d['metrics'])} draft metrics, {len(d['joins'])} joins")
    print("[3/4] Checking metric definitions"); from semantic.metrics import check_metrics; check_metrics()
    print("[4/4] Building the metric catalog"); from semantic.enrich import build_enrichment; build_enrichment(embed=False)


GRAPH_MODES = {"fast": "spaCy, no AI model", "accurate": "GLiNER + spaCy, no AI model",
               "ai": "AI extraction (Settings → AI extraction)"}


def graph_mode() -> str:
    import os
    from app import core
    m = os.environ.get("CL_GRAPH_MODE") or core.get_setting("graph_mode", "accurate") or "accurate"
    return m if m in GRAPH_MODES else "accurate"


def build_graph():
    """Entities and links (spaCy, GLiNER + spaCy, or optional AI extraction), merging, Leiden communities."""
    if not (OUT_DIR / "2_chunks.json").exists():
        raise RuntimeError("Prepare the documents first: the graph is built from their passages.")
    mode = graph_mode()
    if mode == "ai":
        from graph.extract_ai import problems
        bad = problems()
        if bad:
            raise RuntimeError("AI extraction is not set up: " + " ".join(bad))
    out = OUT_DIR / f"gr_extract_{mode}.jsonl"
    print(f"[1/2] Finding entities and links ({GRAPH_MODES[mode]})")
    from graph.nlp_extract import extract_all
    extract_all(out, mode=mode, resume=True)
    print("[2/2] Merging duplicates, finding communities, storing the graph")
    from graph.build import build
    build([str(out)])


def eval_documents():
    from rag.retrieval_eval import run
    run(tag="app")


def eval_data():
    from semantic.rule_eval import run
    run("app")


def eval_graph():
    from graph.eval_extraction import main
    from graph.nlp_extract import extract_all
    import json as _j
    gold = _j.loads((Path(__file__).resolve().parent.parent / "graph_layer" / "gold_extraction.json").read_text(encoding="utf-8"))
    ids = [k for k in gold if not k.startswith("_")]
    out = OUT_DIR / "gr_extract_gold.jsonl"
    extract_all(out, mode=graph_mode(), ids=ids, resume=False)
    main([str(out)])


# ------------------------------------------------------------------ uploads from a chat (Claude, ChatGPT) or an upload link

TABLE_FIRST = {".csv", ".xlsx", ".xls", ".parquet"}       # go to data tables (metrics) unless kind="document"
TEXT_EXTS = {".txt", ".md", ".csv", ".json", ".jsonl", ".ndjson", ".html", ".htm"}


def ingest_upload(filename: str, data: bytes, groups: list[str], kind: str = "auto") -> dict:
    """Save one uploaded file as a document (search + graph) or a data table (metrics). Does not rebuild."""
    from semantic.ingest import ACCEPTED
    name = safe_filename(filename)
    ext = Path(name).suffix.lower()
    if kind not in ("auto", "document", "table"):
        raise ValueError("kind must be auto, document or table")
    if kind == "auto":
        kind = "table" if ext in TABLE_FIRST else "document"
    if not data:
        raise ValueError(f"{name}: the file is empty.")
    if kind == "table":
        if ext not in ACCEPTED:
            raise ValueError(f"{name}: data tables must be CSV, Excel, JSON, JSON Lines or Parquet.")
        return {"file": name, "kind": "table", "tables": save_table(name, data)}
    d = save_document(name, data, groups)
    return {"file": d["name"], "kind": "document", "groups": d["groups"]}


def rebuild(info: dict):
    """The rebuild after uploads: documents (then the graph, if it is in use) and/or data tables."""
    files = info.get("files", [])
    kinds = {f["kind"] for f in files}
    print(f"Rebuilding for {len(files)} new file(s): {', '.join(f['file'] for f in files)}")
    if "document" in kinds:
        prepare_documents()
        if info.get("graph"):
            update_graph([f["file"] for f in files if f["kind"] == "document"])
    if "table" in kinds:
        layer = info.get("layer")
        from semantic import defstore
        if layer and defstore.enabled():
            with defstore.acting(info.get("who") or "upload", "upload", stack=(defstore.COMPANY, layer), write_layer=layer):
                prepare_data()                   # drafts this person's tables into their own layer
        else:
            prepare_data()


def update_graph(sources: list[str]):
    """After uploads: read only the new files' passages for the graph, then merge. A full read of every
    passage (GLiNER, 1-2 s each) only happens from the Graph page, never because of an upload."""
    chunks = json.loads((OUT_DIR / "2_chunks.json").read_text(encoding="utf-8"))
    files = sorted(OUT_DIR.glob("gr_extract_*.jsonl"), key=lambda p: (p.stem != f"gr_extract_{graph_mode()}",
                                                                        -p.stat().st_mtime))
    files = [p for p in files if p.stem.removeprefix("gr_extract_") in GRAPH_MODES]
    new = {c["chunk_id"] for c in chunks if c["source"] in set(sources)}
    if not files:
        print("[graph] skipped: build the graph once from the Graph page, then uploads keep it up to date")
        return
    out = files[0]
    mode = out.stem.removeprefix("gr_extract_")
    have = set()
    for line in out.read_text(encoding="utf-8").splitlines():
        if line.strip():
            have.add(json.loads(line)["chunk_id"])
    others = {c["chunk_id"] for c in chunks} - new
    if others and len(have & others) < 0.9 * len(others):
        print(f"[graph] skipped: the graph covers {len(have & others)} of {len(others)} passages; rebuild it from "
              "the Graph page")
        return
    if mode == "ai":
        from graph.extract_ai import problems
        if problems():
            mode, out = "accurate", OUT_DIR / "gr_extract_accurate.jsonl"
            if not out.exists():
                print("[graph] skipped: AI extraction is not set up")
                return
    print(f"[graph] reading {len(new)} new passages ({GRAPH_MODES[mode]})")
    from graph.nlp_extract import extract_all
    extract_all(out, mode=mode, ids=sorted(new), resume=True, compact=True)
    print("[graph] merging and storing the graph")
    from graph.build import build
    build([str(out)])


def queue_rebuild(saved: list[dict], by: str, layer: str | None = None, who: str | None = None) -> dict:
    from app import jobs
    return jobs.enqueue("rebuild", "Adding uploaded files", rebuild, by,
                        {"files": saved, "graph": graph_ready() or bool(info_graph_wanted()), "layer": layer,
                         "who": who or by})


def info_graph_wanted() -> bool:
    from app import core
    return bool(core.get_setting("graph_on_upload", False))


def rebuild_report(job: dict, info: dict) -> dict:
    """What the uploaded files turned into, for build_status."""
    out = {"job_id": job["id"], "status": job["status"], "queue_position": job.get("queue_position"),
           "started": job["started"], "finished": job["finished"], "error": job["error"],
           "steps": (job.get("steps") or [])[-15:], "files": []}
    if job.get("started") and job["status"] == "running":
        from datetime import datetime, timezone
        out["running_for"] = f"{int((datetime.now(timezone.utc) - datetime.fromisoformat(job['started'])).total_seconds() // 60)} min"
    docs = {d["name"]: d for d in documents()} if job["status"] == "done" else {}
    drafted = []
    if job["status"] == "done" and any(f["kind"] == "table" for f in info.get("files", [])):
        drafted = [m["name"] for m in metrics()["metrics"] if (m.get("status") or "") == "draft"]
    for f in info.get("files", []):
        r = dict(f)
        if f["kind"] == "document" and f["file"] in docs:
            r["passages"] = docs[f["file"]]["passages"]
            r["ready"] = docs[f["file"]]["status"] == "ready"
        out["files"].append(r)
    if job["status"] == "done":
        g = graph_overview() or {}
        out["graph"] = {"entities": g.get("entities"), "relations": g.get("relations")} if g else None
        out["metrics_waiting_for_approval"] = drafted
        out["next"] = ("Ready. Use ask or search_documents to answer from the new files"
                       + (". New metric definitions are drafts: an admin approves them on the Metrics page "
                          "before query_metrics uses them." if drafted else "."))
    elif job["status"] in ("running", "queued"):
        out["next"] = ("Still building. Questions keep working meanwhile and use the documents as they were "
                       "before the upload. Check again with build_status in about 30 seconds.")
    return out


JOBS = {
    "documents": ("Preparing documents", prepare_documents, "admin"),
    "data": ("Preparing data tables", prepare_data, "admin"),
    "graph": ("Building the knowledge graph", build_graph, "admin"),
    "eval_documents": ("Testing document search", eval_documents, "admin"),
    "eval_data": ("Testing metric questions", eval_data, "admin"),
    "eval_graph": ("Testing entity and link extraction", eval_graph, "admin"),
}


# ------------------------------------------------------------------ quality

def quality() -> list[dict]:
    rag_res = _json(OUT_DIR / "9_retrieval_eval_app.json")
    sl_res = _json(SC.OUT_DIR / "sl_7_rule_eval_app.json")
    gr_res = _json(OUT_DIR / "gr_extract_eval.json")

    def pick(d, keys):
        return {k: d[k] for k in keys if isinstance(d, dict) and isinstance(d.get(k), (int, float))}
    docs = {}
    if rag_res:
        best = rag_res["summary"].get("+graph+coverage") or next(iter(rag_res["summary"].values()))
        docs = pick(best, ("recall@1", "recall@5", "all@5", "mrr", "ndcg@10"))
    data = pick((sl_res or {}).get("summary", {}), ("execution_accuracy", "refusal_accuracy", "questions"))
    graph = {}
    if gr_res:
        r = next(iter(gr_res.values()))
        graph = {"entity_f1": r["entities"]["f1"], "typed_entity_f1": r["entities_typed"]["f1"],
                 "link_recall": r["relations_loose"]["recall"]}
    return [
        {"id": "eval_documents", "name": "Document search", "what": "Gold questions: is the passage with the answer found, and how high?",
         "last_run": iso(_mtime(OUT_DIR / "9_retrieval_eval_app.json")), "scores": docs},
        {"id": "eval_data", "name": "Metric questions", "what": "Gold questions turned into metric requests without an AI model: same numbers as the reviewed answer?",
         "last_run": iso(_mtime(SC.OUT_DIR / "sl_7_rule_eval_app.json")), "scores": data},
        {"id": "eval_graph", "name": "Entities and links", "what": "Hand-labelled passages: entities and links found by the graph builder",
         "last_run": iso(_mtime(OUT_DIR / "gr_extract_eval.json")), "scores": graph},
    ]


# ------------------------------------------------------------------ answering (evidence, no language model)

def graph_ready() -> bool:
    from graph import search as GS
    return GS.ready()


GUIDANCE = ("Context Layer returns evidence, not a written answer: answer from these passages, facts and numbers, "
            "cite passage ids, and say so if they do not contain the answer. Each passage has an 'evidence' label "
            "(strong = answers the question, partial = related, weak = barely related): do not state a fact that only "
            "weak passages support. 'evidence' at the top level is the best passage's label; 'none' means nothing "
            "relevant was found, so say the sources do not cover it. When passages disagree, prefer the most "
            "recently updated source (source_updated) and mention the conflict.")
CONTEXT_FOR_TOP = 3                     # passages that get their neighbouring text from the same section


def _passage_out(p: dict, question: str, groups: list[str] | None = None, with_context: bool = False) -> dict:
    from rag.retrieve import highlights, neighbours
    out = {"id": p["chunk_id"], "source": p["source"], "pages": p.get("pages") or [],
           "section": " > ".join(p.get("headings") or []), "text": p["text"][:2000],
           "score": p.get("rerank_score"), "relevance": p.get("relevance"), "evidence": p.get("evidence", "unscored"),
           "highlights": highlights(question, p["text"]),
           **({"included_for": p["coverage_for"]} if p.get("coverage_for") else {})}
    updated = iso(_mtime(DOCS_DIR / p["source"]))
    if updated:
        out["source_updated"] = updated
    if with_context:
        ctx = neighbours(p["chunk_id"], groups)
        if ctx:
            out["context"] = ctx
    return out


def _passages_out(passages: list[dict], question: str, groups: list[str] | None) -> list[dict]:
    shown = {p["chunk_id"] for p in passages}
    out = []
    for i, p in enumerate(passages):
        o = _passage_out(p, question, groups, with_context=i < CONTEXT_FOR_TOP)
        for side in ("before", "after"):              # never repeat a passage that is already in the list
            if o.get("context", {}).get(side, {}).get("id") in shown:
                o["context"].pop(side)
        if o.get("context") == {}:
            o.pop("context")
        out.append(o)
    return out


def search(query: str, groups: list[str], top_k: int = 5, files: list[str] | None = None,
           use_graph: bool = True) -> dict:
    from rag.retrieve import retrieve
    ret = retrieve(query, groups, files or None, save=False, top_k=max(1, min(top_k, 10)), use_graph=use_graph)
    g = ret.get("graph") or {}
    return {"passages": _passages_out(ret["passages"], query, groups), "evidence": ret.get("evidence", "none"),
            "entities": [e["name"] for e in g.get("entities", [])], "timings_s": ret["timings_s"]}


def metric_query(request: dict | None = None, question: str | None = None) -> dict:
    """A structured request (metric and dimension names; the agent fills it in) or a plain question
    (the rule planner fills it in). The compiler writes the SQL; DuckDB runs it."""
    from semantic.answer import fix_values, template_answer, today
    from semantic.compiler import CompileError, compile_query
    from semantic.dates import resolve
    from semantic.db import connect
    from semantic.enrich import load_store
    from semantic.schema import SemanticQuery
    from semantic.yamlio import load_metrics, load_model
    t0 = time.perf_counter()
    from semantic.rule_planner import planner
    planner()                                    # builds the catalog on first use
    model, mets = load_model(), load_metrics()
    store = load_store()
    assumptions, how = [], "request"
    if request:
        req = dict(request)
        period = req.pop("period", None)
        if period and not (req.get("start_date") or req.get("end_date")):
            hints = resolve(str(period), today())
            if not hints:
                raise ValueError(f"Could not read the period '{period}'. Use e.g. 'last quarter', 'FY2025-26', "
                                 "'2025', 'October 2026', or give start_date and end_date.")
            req["start_date"], req["end_date"] = hints[0]["start_date"], hints[0]["end_date"]
            assumptions.append(f"period '{period}' = {req['start_date']} to {req['end_date']}")
        try:
            q = SemanticQuery.model_validate(req)
        except Exception as e:                                       # noqa: BLE001 - shown to the caller
            raise ValueError(f"Invalid request: {e}") from e
        review = [m.name for m in mets.metrics if m.name in q.metrics and m.status == "needs_review"]
        if review:
            raise ValueError(f"{', '.join(review)} is being reviewed (it no longer runs on the current data, e.g. a "
                             "column was renamed); it cannot be used until an admin fixes it.")
    else:
        from semantic.rule_planner import planner
        p = planner().plan(question or "", today())
        if not p.answerable:
            return {"found": False, "answer": None, "not_answerable": p.reason_if_not_answerable,
                    "metrics": [m.name for m in mets.metrics if m.status == "approved"],
                    "timings_s": {"total": round(time.perf_counter() - t0, 3)}}
        q, assumptions, how = p.query, list(p.assumptions), "rule planner"
    corrections = fix_values(q, {"values": store["values"]}, store["synonyms"])
    try:
        compiled = compile_query(q, model, mets)
    except CompileError as e:
        raise ValueError(str(e)) from e
    from semantic.db import connect_scoped
    df = connect_scoped(model).execute(compiled.sql).df()          # only the rows the caller may see (row rules)
    rows = json.loads(df.head(200).to_json(orient="records", date_format="iso"))
    summary = template_answer(q, rows)
    return {"found": not df.empty, "answer": summary, "planned_by": how,
            "request": q.model_dump(mode="json", exclude_defaults=True), "assumptions": assumptions,
            "value_corrections": corrections, "sql": compiled.sql, "rows": rows, "row_count": len(df),
            "timings_s": {"total": round(time.perf_counter() - t0, 3)}}


# Repeat questions: the same question from callers with the same data sets, connectors and layers, against the
# same built sources, returns the stored evidence instead of searching again. Exact match only (after normalising
# case and spacing): "revenue in 2024" and "revenue in 2025" are near-identical as vectors, so a similarity cache
# would hand back the wrong evidence. Any rebuild, access change, definition change or new day invalidates entries.
ASK_CACHE_SIZE = 512
ASK_CACHE_TTL_S = 600
_ASK_CACHE: "dict[tuple, tuple[float, dict]]" = {}


def _sources_version() -> tuple:
    from semantic import config as S
    from semantic import defstore
    paths = [OUT_DIR / "2_chunks.json", OUT_DIR / "4_index.json", OUT_DIR / "_graph_build.json", ACCESS_FILE,
             S.OUT_DIR / "sl_1_profile.json", S.STORE_DB]
    if S.DATA_DIR.exists():
        paths += sorted(p for p in S.DATA_DIR.iterdir() if p.suffix.lower() in S.TABLE_EXTS)
    if defstore.enabled():
        layers = (defstore.STACK.get(), defstore.version())
    else:
        layers = tuple(_mtime(p) for p in [*sorted(S.LAYER_DIR.glob("*.yaml")), S.INSTRUCTIONS_FILE])
    from rag.wspath import workspace_key
    return (workspace_key(), tuple(_mtime(p) for p in paths), layers)


def _ask_key(question: str, groups, capabilities, mode: str) -> tuple:
    from datetime import date
    from semantic.access import SCOPE
    norm = re.sub(r"\s+", " ", question.strip().lower()).rstrip(" ?.!")
    scope = SCOPE.get()
    rows = None if scope is None else (tuple(sorted(scope.get("teams") or [])), tuple(sorted(scope.get("roles") or [])))
    return (norm, None if groups is None else tuple(sorted(groups)), tuple(sorted(capabilities)), mode, rows,
            date.today().isoformat(), _sources_version())


def clear_ask_cache():
    _ASK_CACHE.clear()


def ask(question: str, groups: list[str] | None, capabilities: list[str], mode: str = "auto") -> dict:
    """Gather the evidence for a question as a caller with these groups and capabilities (cached, see above).
    mode: auto | documents | metrics | graph. groups=None means unrestricted (owner tools only)."""
    import copy
    try:
        key = _ask_key(question, groups, capabilities, mode)
    except Exception:                                      # noqa: BLE001 - no cache when the version is unknown
        key = None
    hit = _ASK_CACHE.get(key) if key else None
    if hit and time.time() - hit[0] < ASK_CACHE_TTL_S:
        out = copy.deepcopy(hit[1])
        out["cached"] = True
        out["timings_s"] = {"total": 0.0, "original_total": hit[1]["timings_s"].get("total")}
        return out
    out = _ask(question, groups, capabilities, mode)
    if key:
        if len(_ASK_CACHE) >= ASK_CACHE_SIZE:
            _ASK_CACHE.pop(min(_ASK_CACHE, key=lambda k: _ASK_CACHE[k][0]))
        _ASK_CACHE[key] = (time.time(), copy.deepcopy(out))
    return out


def _ask(question: str, groups: list[str] | None, capabilities: list[str], mode: str = "auto",
         split: bool = True) -> dict:
    """In auto mode the question is first checked (app/answerable.py, rule-based): not answerable from what this
    install knows -> found false with the reason and clarifying questions (nothing guessed); a mixed question is
    answered part by part. Routing itself is app/router.py."""
    import os
    if mode == "auto" and os.environ.get("CL_ANSWERABLE", "1") != "0" and question.strip():
        from app.answerable import check
        chk = check(question, capabilities)
        if not chk["answerable"]:
            return {"question": question.strip(), "route": "not_answerable", "route_reason": chk["reason"],
                    "answer": None, "passages": [], "evidence": "none", "found": False,
                    "not_answerable": chk["reason"], "clarifying_questions": chk["clarify"],
                    "notes": [chk["reason"] + " Ask the user to clarify rather than answering from general knowledge."],
                    "guidance": GUIDANCE, "timings_s": {"total": 0.0}}
        if split and chk["parts"]:
            return _ask_parts(question, chk["parts"], groups, capabilities)
    return _ask_one(question, groups, capabilities, mode)


def _ask_parts(question: str, parts: list[str], groups, capabilities) -> dict:
    """A mixed question: each part gathered with its own route; passages merged, numbers kept per part."""
    t0 = time.perf_counter()
    subs = []
    for p in parts:
        try:
            subs.append(_ask(p, groups, capabilities, "auto", split=False))
        except PermissionError as e:              # e.g. a documents part for a data-only key: that part is refused
            subs.append({"route": "refused", "route_reason": str(e), "evidence": "none", "found": False, "passages": []})
    seen, passages = set(), []
    for s in subs:
        for p in s.get("passages") or []:
            if p["id"] not in seen:
                seen.add(p["id"])
                passages.append(p)
    order = ["exact", "strong", "partial", "unscored", "weak", "none"]
    best = min((s.get("evidence", "none") for s in subs), key=lambda e: order.index(e) if e in order else len(order))
    return {"question": question.strip(), "route": "mixed", "route_reason": f"{len(parts)} parts answered separately",
            "answer": None, "passages": passages, "evidence": best, "found": any(s.get("found") for s in subs),
            "parts": [{"question": p, **{k: s.get(k) for k in ("route", "route_reason", "evidence", "found", "data",
                                                               "graph", "not_answerable", "clarifying_questions")
                                       if s.get(k) is not None},
                       "passage_ids": [x["id"] for x in s.get("passages") or []]} for p, s in zip(parts, subs)],
            "notes": [], "guidance": GUIDANCE, "timings_s": {"total": round(time.perf_counter() - t0, 3)}}


def _ask_one(question: str, groups: list[str] | None, capabilities: list[str], mode: str = "auto") -> dict:
    from app.router import route as pick_route
    from graph import search as GS
    from graph.access import sees_everything, visible
    question = question.strip()
    if not question:
        raise ValueError("Ask a question.")
    caps = set(capabilities)
    t0 = time.perf_counter()
    notes = []
    if mode == "auto":
        chosen, reason = pick_route(question)
    else:
        chosen, reason = {"documents": "documents", "metrics": "data", "graph": "connections",
                          "overview": "overview"}.get(mode, mode), f"requested: {mode}"
    if chosen == "data" and "data" not in caps:
        chosen, reason = "documents", reason
        notes.append("Looks like a metrics question, but this caller has no access to data; searched the documents.")
    if chosen in ("connections", "overview") and ("graph" not in caps or not graph_ready()):
        notes.append("Knowledge graph not available to this caller; searched the documents.")
        chosen = "documents"
    if chosen == "overview" and not sees_everything(groups):
        notes.append("Overview search covers every document group; this caller does not see all of them, so it "
                     "searched the passages it can see.")
        chosen = "documents"
    if chosen != "data" and "documents" not in caps and "graph" not in caps:
        raise PermissionError("This caller may not read documents.")

    out = {"question": question, "route": chosen, "route_reason": reason, "answer": None, "passages": [],
           "notes": notes, "guidance": GUIDANCE}
    g = groups if groups is not None else sorted(_all_chunk_groups())
    if chosen == "data":
        r = metric_query(question=question)
        out["answer"] = r.get("answer")
        out["data"] = {k: r.get(k) for k in ("found", "request", "assumptions", "value_corrections", "sql", "rows",
                                            "row_count", "not_answerable", "metrics", "planned_by") if r.get(k) is not None}
        if not r.get("found") and r.get("not_answerable") and "documents" in caps:
            notes.append("No metric matches; the documents were searched as well.")
            s = search(question, g)
            out["passages"], out["evidence"] = s["passages"], s["evidence"]
    else:
        use_graph = "graph" in caps and graph_ready()
        if "documents" in caps:
            s = search(question, g, 5, use_graph=use_graph)
            out["passages"], out["evidence"] = s["passages"], s["evidence"]
        if chosen == "connections" and use_graph:
            loc = GS.local(question, groups, visible)
            out["graph"] = {"entities": loc["entities"], "bridges": loc["bridges"], "relations": loc["relations"][:25]}
            if "documents" not in caps:            # graph-only key: passages behind the graph facts
                from rag.retrieve import evidence_strength, fetch_chunks, rerank, screen
                hits = screen(rerank(question, fetch_chunks(loc["passage_ids"][:12], g)))[:5]
                out["passages"], out["evidence"] = _passages_out(hits, question, g), evidence_strength(hits)
        if chosen == "overview" and use_graph:
            ov = GS.overview(question, groups, visible)
            out["overview"] = ov["communities"]
    out["timings_s"] = {"total": round(time.perf_counter() - t0, 3)}
    if (out.get("data") or {}).get("found"):
        out["evidence"] = "exact"                  # computed from the governed metric definitions
    out.setdefault("evidence", "none")
    out["found"] = bool(out["passages"] or (out.get("data") or {}).get("found"))
    return out


def _all_chunk_groups() -> set:
    from graph.access import all_groups
    return all_groups() or {"public"}


def explore_entity(name: str, groups: list[str] | None) -> dict | None:
    from graph import search as GS
    from graph.access import visible
    if not graph_ready():
        raise RuntimeError("The knowledge graph has not been built yet.")
    return GS.entity(name, groups, visible)


def describe_data() -> dict:
    """Everything an agent needs to fill in a metric request itself."""
    from semantic.enrich import load_store
    from semantic.rule_planner import planner
    from semantic.yamlio import load_metrics, load_model, load_verified
    planner()                                    # builds the catalog on first use
    model, mets = load_model(), load_metrics()
    store = load_store()
    dims = []
    for e in model.entities:
        for d in e.dimensions:
            if d.type == "key":
                continue
            ref = f"{e.name}.{d.name}"
            item = {"name": ref, "label": d.label or d.name, "type": d.type, "description": d.description}
            if d.type == "time":
                item["grains"] = ["day", "week", "month", "quarter", "year", "fiscal_quarter", "fiscal_year"]
                item["use_as"] = f"{ref}__month (etc.)"
            if ref in store["values"]:
                item["values"] = store["values"][ref][:60]
            dims.append(item)
    try:
        examples = [{"question": v.question, "request": v.query.model_dump(mode="json", exclude_defaults=True)}
                    for v in load_verified().queries][:10]
    except Exception:                                          # noqa: BLE001
        examples = []
    return {
        "metrics": [{"name": m.name, "label": m.label, "description": m.description, "format": m.format,
                     "entity": m.entity, "time_column": m.time_column} for m in mets.metrics if m.status == "approved"],
        "dimensions": dims,
        "synonyms": [{"term": t, "means": v} for t, v in store["synonyms"]][:200],
        "business_rules": store["instructions"],
        "examples": examples,
        "request_format": {"metrics": ["metric name"], "dimensions": ["entity.dimension or entity.time_dim__grain"],
                           "filters": [{"dimension": "entity.dimension", "op": "= | != | in | not in | > | >= | < | <=",
                                        "values": ["..."]}],
                           "period": "optional words: 'last quarter', 'FY2025-26', '2025', 'October 2026'",
                           "start_date": "YYYY-MM-DD (inclusive)", "end_date": "YYYY-MM-DD (inclusive)",
                           "order_by": ["metric or dimension, '-' prefix for descending"], "limit": "number"},
    }


def passage(chunk_id: str, groups: list[str]) -> dict | None:
    from graph.access import visible
    if not visible(chunk_id, groups):
        return None
    for c in _json(OUT_DIR / "2_chunks.json", []) or []:
        if c["chunk_id"] == chunk_id:
            return {"id": chunk_id, "source": c["source"], "pages": c.get("pages") or [],
                    "section": " > ".join(c.get("headings") or []), "text": c["text"]}
    return None


def visible_documents(groups: list[str]) -> list[dict]:
    return [{"name": d["name"], "groups": d["groups"], "passages": d["passages"]} for d in documents()
            if d["status"] != "new" and set(d["groups"]) & set(groups)]


def reset_outputs_for(kind: str):
    """Remove built files of one kind (used before a full rebuild)."""
    if kind == "graph":
        for p in OUT_DIR.glob("gr_extract_all.jsonl"):
            p.unlink()


def disk_usage() -> dict:
    total, used, free = shutil.disk_usage(str(OUT_DIR.parent))
    return {"free_gb": round(free / 2 ** 30, 1), "total_gb": round(total / 2 ** 30, 1)}
