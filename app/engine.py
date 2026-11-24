"""Adapter between the app and the three engines (documents RAG, semantic layer, knowledge graph).
The engines stay usable on their own from rag.py; this module only reads their files and calls
their entry points, adding access control and the per-agent capabilities."""
import json
import re
import shutil
import time
from pathlib import Path

import yaml

from rag.config import DOCLING_EXTS, DOCS_DIR, OUT_DIR, PLAINTEXT_EXTS
from semantic import config as SC

DOC_EXTS = DOCLING_EXTS | PLAINTEXT_EXTS
ACCESS_FILE = DOCS_DIR / "_access.json"
_GROUP_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
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
    ptables = {t.get("name") or t.get("table"): t for t in prof.get("tables", [])} if isinstance(prof, dict) else {}
    out = []
    if SC.DATA_DIR.exists():
        for p in sorted(SC.DATA_DIR.iterdir()):
            if p.suffix.lower() in SC.TABLE_EXTS:
                t = ptables.get(p.stem, {})
                out.append({"name": p.name, "table": p.stem, "size": p.stat().st_size, "modified": iso(p.stat().st_mtime),
                            "rows": t.get("rows") or t.get("row_count"), "columns": len(t.get("columns", []) or [])})
    return out


def save_table(filename: str, data: bytes) -> str:
    name = safe_filename(filename)
    if Path(name).suffix.lower() not in SC.TABLE_EXTS:
        raise ValueError(f"{name}: use CSV, Excel (.xlsx) or Parquet.")
    SC.DATA_DIR.mkdir(parents=True, exist_ok=True)
    (SC.DATA_DIR / name).write_bytes(data)
    return name


def metrics() -> dict:
    def load(path):
        try:
            return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
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


def graph_explore(q: str = "", limit: int = 120) -> dict:
    """Entities and relations for the explorer, from the merged graph the build saved."""
    g = _json(OUT_DIR / "_graph_build.json")
    if not g:
        return {"entities": [], "relations": [], "total": 0}
    ents = g.get("entities", [])
    rels = g.get("relations", [])
    if q:
        ql = q.lower()
        hit = [e for e in ents if ql in e["name"].lower()]
        ids = {e["id"] for e in hit[:20]}
        near = {r["target_id"] for r in rels if r["source_id"] in ids} | {r["source_id"] for r in rels if r["target_id"] in ids}
        keep = ids | near
    else:
        keep = {e["id"] for e in sorted(ents, key=lambda e: -e.get("degree", 0))[:limit]}
    by_id = {e["id"]: e for e in ents}
    keep = set(list(keep)[: limit * 2])
    return {
        "total": len(ents),
        "entities": [{"id": i, "name": by_id[i]["name"], "type": by_id[i]["type"], "degree": by_id[i].get("degree", 0),
                      "description": (by_id[i].get("description") or "")[:400],
                      "community": by_id[i].get("community_l0"), "passages": len(by_id[i].get("chunk_ids", []) or [])}
                     for i in keep if i in by_id],
        "relations": [{"source": r["source_id"], "target": r["target_id"], "type": r["type"], "weight": r.get("weight", 1),
                       "description": (r.get("description") or "")[:300]}
                      for r in rels if r["source_id"] in keep and r["target_id"] in keep][:600],
    }


def postgres_ok() -> tuple[bool, str]:
    try:
        import psycopg
        from graph.config import PG_DSN
        with psycopg.connect(PG_DSN, connect_timeout=2) as c:
            c.execute("SELECT 1")
        return True, "connected"
    except Exception as e:                                     # noqa: BLE001 - reported to the admin as text
        return False, f"{type(e).__name__}: {str(e).splitlines()[0][:160]}"


# ------------------------------------------------------------------ readiness

def llm_ready() -> bool:
    import os
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def status() -> dict:
    docs = documents()
    pg_ok, pg_msg = postgres_ok()
    graph = graph_overview()
    return {
        "llm": {"ready": llm_ready()},
        "documents": {"count": len(docs), "ready": sum(d["status"] == "ready" for d in docs),
                      "pending": sum(d["status"] != "ready" for d in docs),
                      "passages": sum(d["passages"] for d in docs), "indexed": iso(_chunk_stats()[1])},
        "data": {"tables": len(tables()), "metrics": len(metrics()["metrics"]), "ready": SC.STORE_DB.exists(),
                 "prepared": iso(_mtime(SC.STORE_DB))},
        "graph": {"ready": bool(graph) and pg_ok, "built": graph.get("built") if graph else None,
                  "entities": graph.get("entities") if graph else 0, "relations": graph.get("relations") if graph else 0},
        "postgres": {"ok": pg_ok, "message": pg_msg},
    }


# ------------------------------------------------------------------ jobs (run in the background by app.jobs)

def prepare_documents():
    print("[1/4] Reading documents"); from rag.parse import parse_all; parse_all()
    print("[2/4] Splitting into passages"); from rag.chunk import chunk_all; chunk_all()
    print("[3/4] Embedding"); from rag.embed import embed_all; embed_all()
    print("[4/4] Building the search index"); from rag.store import build_index; build_index()
    from graph.query import chunk_access
    chunk_access()


def prepare_data():
    print("[1/3] Profiling tables"); from semantic.profile import profile_all; profile_all()
    print("[2/3] Checking metric definitions"); from semantic.metrics import check_metrics; check_metrics()
    print("[3/3] Building the metric catalog"); from semantic.enrich import build_enrichment; build_enrichment()


def build_graph():
    import sys
    if not llm_ready():
        raise RuntimeError("Building the knowledge graph needs an AI model: add an Anthropic API key in Settings.")
    ok, msg = postgres_ok()
    if not ok:
        raise RuntimeError(f"The graph database is not reachable ({msg}). Start it with docker compose.")
    out = OUT_DIR / "gr_extract_all.jsonl"
    print("[1/2] Extracting entities and relationships (Claude Haiku)")
    from graph.config import EXTRACT_MODEL
    from graph.extract_llm import main as extract_main
    argv = sys.argv
    sys.argv = ["extract_llm", "--model", EXTRACT_MODEL, "--workers", "4", "--out", str(out)]
    try:
        extract_main()
    finally:
        sys.argv = argv
    print("[2/2] Merging duplicates, finding communities, storing the graph")
    from graph.build import build
    build([str(out)])


def eval_documents():
    from rag.evaluate import run_eval
    run_eval()


def eval_data():
    from semantic.evaluate import run
    run("app")


def eval_graph():
    from graph import evaluate as ge
    if not ge.QFILE.exists():
        ge.make_questions()
    ge.run()
    ge.score()


JOBS = {
    "documents": ("Preparing documents", prepare_documents, "admin"),
    "data": ("Preparing data tables", prepare_data, "admin"),
    "graph": ("Building the knowledge graph", build_graph, "admin"),
    "eval_documents": ("Testing document answers", eval_documents, "admin"),
    "eval_data": ("Testing metric answers", eval_data, "admin"),
    "eval_graph": ("Testing graph answers", eval_graph, "admin"),
}


# ------------------------------------------------------------------ quality

def quality() -> list[dict]:
    rag_res = _json(OUT_DIR / "8_eval_results.json")
    sl = sorted(SC.OUT_DIR.glob("sl_6_eval_*.json"), key=lambda p: p.stat().st_mtime)
    sl_res = _json(sl[-1]) if sl else None
    gr_res = _json(OUT_DIR / "gr_5_eval_compare.json")

    def nums(summary, keys=None):
        if not isinstance(summary, dict):
            return {}
        flat = {}
        for k, v in summary.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                flat[k] = v
            elif isinstance(v, dict) and keys is None:
                for k2, v2 in v.items():
                    if isinstance(v2, (int, float)) and not isinstance(v2, bool):
                        flat[f"{k}.{k2}"] = v2
        return flat
    return [
        {"id": "eval_documents", "name": "Document answers", "what": "RAGAS: context recall and precision, faithfulness, relevancy",
         "last_run": iso(_mtime(OUT_DIR / "8_eval_results.json")), "scores": nums((rag_res or {}).get("summary"))},
        {"id": "eval_data", "name": "Metric answers", "what": "60 gold questions: execution accuracy, refusals, routing, number faithfulness",
         "last_run": iso(_mtime(sl[-1])) if sl else None, "scores": nums((sl_res or {}).get("summary"))},
        {"id": "eval_graph", "name": "Graph answers", "what": "Graph vs plain search on connection and overview questions",
         "last_run": iso(_mtime(OUT_DIR / "gr_5_eval_compare.json")), "scores": nums((gr_res or {}).get("summary"))},
    ]


# ------------------------------------------------------------------ answering

def graph_ready() -> bool:
    return (OUT_DIR / "gr_3_graph.json").exists() and postgres_ok()[0]


def ask(question: str, groups: list[str] | None, capabilities: list[str], mode: str = "auto") -> dict:
    """Answer as a caller with these groups and capabilities. Returns the route taken, the answer,
    sources and timings. groups=None means unrestricted (owner tools only)."""
    from graph import query as GQ
    question = question.strip()
    if not question:
        raise ValueError("Ask a question.")
    if not llm_ready():
        raise RuntimeError("Answering needs an AI model: add an Anthropic API key in Settings.")
    caps = set(capabilities)
    t0 = time.perf_counter()
    notes = []
    chosen = GQ.route(question) if mode == "auto" else mode
    if chosen == "data" and "data" not in caps:
        chosen = "vector"
        notes.append("Looks like a metrics question, but this caller has no access to data; answered from documents.")
    if chosen in ("local", "global") and ("graph" not in caps or not graph_ready()):
        notes.append("Knowledge graph not available to this caller; answered from documents.")
        chosen = "vector"
    if chosen in ("vector", "local", "global") and "documents" not in caps and "graph" not in caps:
        raise PermissionError("This caller may not read documents.")
    r = GQ.answer(question, chosen, groups=groups, allow_data="data" in caps)
    if r.get("note"):
        notes.append(r["note"])
    route = {"vector": "documents", "local": "graph", "global": "graph overview", "data": "metrics"}.get(r.get("mode"), r.get("mode"))
    sources = [{"id": c.get("chunk_id"), "source": c.get("source"), "pages": c.get("pages") or [],
                "section": " > ".join(c.get("headings") or []), "text": (c.get("text") or "")[:1500],
                "score": c.get("rerank_score")} for c in r.get("contexts", []) or []]
    out = {"question": question, "route": route, "answer": r.get("answer"), "found": r.get("found"),
           "citations": r.get("citations", []), "sources": sources, "notes": notes,
           "timings_s": {"total": round(time.perf_counter() - t0, 3)} | (r.get("timings_s") or {})}
    if r.get("mode") == "data":
        out["data"] = {k: r.get(k) for k in ("request", "sql", "rows", "row_count", "assumptions", "value_corrections",
                                             "data_warnings") if r.get(k) is not None}
    if r.get("graph"):
        out["graph"] = {"entities": [{"name": s.get("name"), "type": s.get("type")} for s in r["graph"].get("seeds", [])],
                        "relations": r["graph"].get("relations", [])[:25]}
    return out


def search(query: str, groups: list[str], top_k: int = 5, files: list[str] | None = None) -> list[dict]:
    from rag.retrieve import retrieve
    ret = retrieve(query, groups, files or None, save=False)
    return [{"id": p["chunk_id"], "source": p["source"], "pages": p.get("pages") or [],
             "section": " > ".join(p.get("headings") or []), "text": p["text"][:2000], "score": p.get("rerank_score")}
            for p in ret["passages"][:max(1, min(top_k, 10))]]


def passage(chunk_id: str, groups: list[str]) -> dict | None:
    from graph.query import visible
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
