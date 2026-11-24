"""g7 - Does the graph help? Plain RAG and the graph modes answer the same questions and are scored
by the same RAGAS judge (rag/evaluate.py score_runs), overall and per question type.

Question types come from a "type" field in the questions file (single-hop, multi-hop, global,
unanswerable); untagged questions count as single-hop. Pass rule: for every type and every metric,
hybrid must score no lower than plain RAG minus TOLERANCE. GraphRAG is only worth shipping if it
keeps single-hop quality and adds on multi-hop / global.

Uses eval_questions_graph.json if present (the RAG set plus tagged multi-hop/global questions),
otherwise eval_questions.json.

Output: outputs/g7_graph_eval.json
"""
import json
import statistics

from ..config import ROOT
from ..utils import write_json

TOLERANCE = 0.02


def _questions():
    for name in ("eval_questions_graph.json", "eval_questions.json"):
        p = ROOT / name
        if p.exists():
            return name, json.loads(p.read_text(encoding="utf-8"))
    raise SystemExit("no eval questions file found")


def _avg(v):
    v = [x for x in v if x is not None]
    return round(statistics.mean(v), 3) if v else None


def run_graph_eval(modes=("rag", "hybrid", "local")):
    from ..evaluate import score_runs
    from ..generate import ask
    from .search import ask_graph

    fname, items = _questions()
    for it in items:
        it.setdefault("type", "single-hop")
    types = sorted({it["type"] for it in items})
    print(f"  {len(items)} questions from {fname}; types: {', '.join(types)}; modes: {', '.join(modes)}")

    results = {}
    for mode in modes:
        print(f"  [{mode}] answering...")
        runs = []
        for i, it in enumerate(items, 1):
            g, s = it.get("groups"), it.get("sources")
            r = ask(it["question"], g, s, save=False) if mode == "rag" else ask_graph(it["question"], mode, g, s, save=False)
            runs.append(r)
            print(f"   {i}/{len(items)} {r['timings_s']['total']}s found={r['found']}")
        per_q, metrics = score_runs(items, runs)
        for q, it, r in zip(per_q, items, runs):
            q["type"] = it["type"]
            q["graph_llm_calls"] = r.get("graph_llm", {}).get("calls", 0)
        overall = {m: _avg([q["scores"][m] for q in per_q]) for m in metrics}
        by_type = {t: {m: _avg([q["scores"][m] for q in per_q if q["type"] == t]) for m in metrics} for t in types}
        results[mode] = {"overall": overall, "by_type": by_type,
                         "answered_rate": round(sum(r["found"] for r in runs) / len(runs), 3),
                         "latency_mean_s": round(statistics.mean(r["timings_s"]["total"] for r in runs), 3),
                         "per_question": per_q}

    verdict = None
    if "rag" in results and "hybrid" in results:
        drops = []
        for t in types:
            for m, base in results["rag"]["by_type"][t].items():
                new = results["hybrid"]["by_type"][t].get(m)
                if base is not None and new is not None and new < base - TOLERANCE:
                    drops.append({"type": t, "metric": m, "rag": base, "hybrid": new, "delta": round(new - base, 3)})
        verdict = {"rule": f"hybrid >= rag - {TOLERANCE} on every metric for every question type",
                   "passed": not drops, "regressions": drops}

    table = {mode: {"overall": res["overall"], "by_type": res["by_type"], "answered_rate": res["answered_rate"],
                    "latency_mean_s": res["latency_mean_s"]} for mode, res in results.items()}
    write_json("g7_graph_eval.json", {"questions_file": fname, "verdict": verdict, "summary": table,
                                      "per_question": {m: r["per_question"] for m, r in results.items()}})
    print(json.dumps({"verdict": verdict, "overall": {m: t["overall"] for m, t in table.items()}}, indent=2))
    return verdict
