"""Quality scores after every build, with a trend over builds (Quality tab). Deterministic, no AI model anywhere:

  documents   retrieval on gold passages (eval_questions.json): recall@1/5, all@5, MRR, nDCG@10. Only gold questions
              whose passages exist in this install count (a company's own documents have no gold set until they add
              one); the configuration scored is the one answers use (+graph+coverage when the graph is built).
  metrics     the rule-based planner on gold questions (semantic_layer/eval_questions*.yaml and the Kestrel demo's
              demo/kestrel/semantic_layer/eval_questions.yaml): execution accuracy (same rows as the gold request),
              refusal accuracy. A gold set is used only when every metric it names exists in the company layer, so the
              Kestrel questions run when the Kestrel demo is loaded and the sample company's when it is.
  graph       entity and link extraction on hand-labelled passages (graph_layer/gold_extraction.json): entity F1,
              typed entity F1, link recall. Only when the graph is in use and the gold passages exist.

A build (prepare documents / data, build the graph, an upload's rebuild, a source sync, loading the demo) queues one
quality run of the suites it touched. Off: Settings value quality_after_build = false, or CL_QUALITY_AFTER_BUILD=0.
Each run is stored in `quality_runs` (time, trigger, suite, scores) for the trend.
"""
import json
import os
import secrets
import time
from pathlib import Path

from . import core

ROOT = Path(__file__).resolve().parent.parent
SUITES = {
    "documents": {"name": "Document search", "what": "Gold questions: is the passage with the answer found, and how high?",
                  "main": "recall@5"},
    "metrics": {"name": "Metric questions", "what": "Gold questions planned without an AI model: same rows and numbers as the reviewed request?",
                "main": "execution_accuracy"},
    "graph": {"name": "Entities and links", "what": "Hand-labelled passages: entities and links found by the graph builder.",
              "main": "entity_f1"},
}
KESTREL_GOLD = ROOT / "demo" / "kestrel" / "semantic_layer" / "eval_questions.yaml"
SCHEMA = """CREATE TABLE IF NOT EXISTS quality_runs (
  id TEXT PRIMARY KEY, time TEXT NOT NULL, suite TEXT NOT NULL, trigger TEXT NOT NULL DEFAULT '', ok INTEGER NOT NULL,
  scores TEXT NOT NULL DEFAULT '{}', detail TEXT NOT NULL DEFAULT '{}', seconds REAL)"""
_ready: str | None = None


def db():
    global _ready
    c = core.db()
    if _ready != str(core.DB_PATH):
        c.execute(SCHEMA)
        _ready = str(core.DB_PATH)
    return c


# ------------------------------------------------------------------ the suites

def _chunk_ids() -> set[str]:
    from . import engine
    p = engine.OUT_DIR / "2_chunks.json"
    try:
        return {c["chunk_id"] for c in json.loads(p.read_text(encoding="utf-8"))}
    except (OSError, ValueError):
        return set()


def run_documents() -> dict:
    from rag.retrieval_eval import GOLD, run
    from . import engine
    have = _chunk_ids()
    gold = [q for q in json.loads(GOLD.read_text(encoding="utf-8")) if q.get("gold_chunk_ids")]
    usable = [q for q in gold if set(q["gold_chunk_ids"]) <= have]
    if not usable:
        return {"skipped": "No gold questions for the documents in this install (eval_questions.json)."}
    cfg = "+graph+coverage" if engine.graph_ready() else "hybrid+rerank"
    out = run([cfg], tag="app", questions=usable)
    s = out["summary"][cfg]
    return {"scores": {k: s[k] for k in ("recall@1", "recall@5", "all@5", "mrr", "ndcg@10")} | {"questions": len(usable)},
            "detail": {"configuration": cfg, "skipped_questions": len(gold) - len(usable), "by_type": s.get("by_type")}}


def gold_sets() -> list[Path]:
    from semantic.config import LAYER_DIR
    return sorted(LAYER_DIR.glob("eval_questions*.yaml")) + ([KESTREL_GOLD] if KESTREL_GOLD.exists() else [])


def run_metrics() -> dict:
    from ruamel.yaml import YAML
    from semantic.rule_eval import score_set
    from semantic.yamlio import load_metrics
    names = {m.name for m in load_metrics().metrics}
    sets, skipped = {}, []
    for p in gold_sets():
        qs = YAML(typ="safe").load(p.read_text(encoding="utf-8")).get("questions") or []
        need = {m for q in qs for m in ((q.get("gold") or {}).get("metrics") or [])}
        label = "kestrel" if p == KESTREL_GOLD else p.stem.replace("eval_questions", "").strip("_") or "development"
        if not need or not need <= names:
            skipped.append(label)
            continue
        sets[label] = score_set(p)
    if not sets:
        return {"skipped": "No gold metric questions match the metrics in the company layer."}
    total = sum(v["questions"] for v in sets.values())
    acc = sum(v["execution_accuracy"] * v["questions"] for v in sets.values()) / total
    refusals = [v["refusal_accuracy"] for v in sets.values() if v.get("refusal_accuracy") is not None]
    scores = {"execution_accuracy": round(acc, 3), "questions": total}
    if refusals:
        scores["refusal_accuracy"] = round(sum(refusals) / len(refusals), 3)
    scores |= {f"{k}_accuracy": v["execution_accuracy"] for k, v in sets.items()}
    return {"scores": scores, "detail": {"sets": {k: {"by_kind": v["by_kind"],
                                                      "failures": [f["id"] for f in v["failures"]][:30]}
                                                  for k, v in sets.items()}, "skipped_sets": skipped}}


def run_graph() -> dict:
    from graph.eval_extraction import GOLD, score
    from graph.eval_extraction import load as load_extract
    from . import engine
    if not engine.graph_ready():
        return {"skipped": "The knowledge graph is not built."}
    gold = json.loads(GOLD.read_text(encoding="utf-8"))
    ids = sorted(k for k in gold if not k.startswith("_") and k in _chunk_ids())
    if not ids:
        return {"skipped": "The hand-labelled passages are not in this install's documents."}
    from graph.nlp_extract import extract_all
    out = engine.OUT_DIR / "gr_extract_gold.jsonl"
    extract_all(out, mode=engine.graph_mode() if engine.graph_mode() != "ai" else "accurate", ids=ids, resume=False)
    _, recs = load_extract(out)
    r = score(recs, {k: v for k, v in gold.items() if k in ids or k.startswith("_")})
    return {"scores": {"entity_f1": r["entities"]["f1"], "typed_entity_f1": r["entities_typed"]["f1"],
                       "link_recall": r["relations_loose"]["recall"], "passages": len(ids)},
            "detail": {"seconds_per_passage": r.get("seconds_per_chunk")}}


RUNNERS = {"documents": run_documents, "metrics": run_metrics, "graph": run_graph}


def record(suite: str, trigger: str, result: dict, ok: bool, seconds: float) -> dict:
    row = {"id": f"{time.time_ns():020d}{secrets.token_hex(3)}", "time": core.now(), "suite": suite, "trigger": trigger[:120], "ok": int(ok),
           "scores": json.dumps(result.get("scores") or {}),
           "detail": json.dumps({k: v for k, v in result.items() if k != "scores"}, default=str)[:20000],
           "seconds": round(seconds, 2)}
    db().execute("INSERT INTO quality_runs (id, time, suite, trigger, ok, scores, detail, seconds) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", tuple(row.values()))
    return row


def run(suites: list[str] | None = None, trigger: str = "manual") -> dict:
    """Run suites (default all) and record each result. Never raises: a broken suite is recorded as failed."""
    out = {}
    for s in suites or list(RUNNERS):
        if s not in RUNNERS:
            continue
        print(f"[quality] {SUITES[s]['name']}")
        t0 = time.perf_counter()
        try:
            res, ok = RUNNERS[s](), True
        except Exception as e:                                # noqa: BLE001 - shown on the Quality tab
            res, ok = {"error": f"{type(e).__name__}: {e}"[:500]}, False
        if res.get("skipped"):
            print(f"  skipped: {res['skipped']}")
            out[s] = res
            continue
        record(s, trigger, res, ok, time.perf_counter() - t0)
        print(f"  {res.get('scores') or res.get('error')}")
        out[s] = res
    return out


# ------------------------------------------------------------------ after builds

def enabled() -> bool:
    if os.environ.get("CL_QUALITY_AFTER_BUILD", "1") == "0":
        return False
    return bool(core.get_setting("quality_after_build", True))


def suites_for(kind: str, info: dict | None = None) -> list[str]:
    """Which suites a finished job touched."""
    info = info or {}
    if kind == "documents":
        return ["documents"]
    if kind == "data":
        return ["metrics"]
    if kind == "graph":
        return ["graph", "documents"]
    if kind == "rebuild":
        kinds = {f.get("kind") for f in info.get("files") or []}
        return (["documents"] if "document" in kinds else []) + (["graph"] if "document" in kinds and info.get("graph") else []) \
            + (["metrics"] if "table" in kinds else [])
    if kind.startswith("sync:") or kind in ("demo", "sync"):
        return ["documents", "metrics", "graph"]
    return []


def after_build(kind: str, info: dict | None, by: str):
    """Queue one quality run for what a finished build touched (merged with a waiting one)."""
    suites = suites_for(kind, info)
    if not suites or not enabled():
        return None
    from . import jobs
    return jobs.enqueue("quality", "Scoring quality after the build", lambda i: run(i["suites"], i["trigger"]), by,
                        {"suites": suites, "trigger": f"after {kind.split(':')[0]}"})


# ------------------------------------------------------------------ for the Quality tab

def _row(r) -> dict:
    return {"id": r["id"], "time": r["time"], "suite": r["suite"], "trigger": r["trigger"], "ok": bool(r["ok"]),
            "scores": json.loads(r["scores"] or "{}"), "detail": json.loads(r["detail"] or "{}"), "seconds": r["seconds"]}


def history(suite: str | None = None, limit: int = 30) -> list[dict]:
    sql, args = "SELECT * FROM quality_runs", []
    if suite:
        sql += " WHERE suite = ?"
        args.append(suite)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(limit, 500)))
    return [_row(r) for r in db().execute(sql, args).fetchall()]


def overview() -> list[dict]:
    out = []
    for sid, meta in SUITES.items():
        runs = history(sid, 30)
        last = runs[0] if runs else None
        prev = next((r for r in runs[1:] if r["ok"]), None)
        main = meta["main"]
        change = None
        if last and prev and isinstance(last["scores"].get(main), (int, float)) and isinstance(prev["scores"].get(main), (int, float)):
            change = round(last["scores"][main] - prev["scores"][main], 3)
        out.append({"id": sid, "name": meta["name"], "what": meta["what"], "main": main,
                    "last_run": last and last["time"], "ok": last["ok"] if last else None,
                    "error": last and last["detail"].get("error"), "scores": last["scores"] if last else {},
                    "change": change,
                    "trend": [{"time": r["time"], "value": r["scores"].get(main), "trigger": r["trigger"]}
                              for r in reversed(runs) if r["ok"]]})
    return out
