"""Evaluate the rule-based planner (no language model) on the semantic layer's gold questions.

  execution_accuracy  the planner's request returns the same rows and numbers as the gold request
                      (unanswerable questions count as correct when the planner refuses)
  refusal_accuracy    unanswerable questions are refused instead of answered with a guess
  by_kind             the same, per question kind (simple, breakdown, filter, top_n, synonym, typo, ...)

Gold sets: semantic_layer/eval_questions.yaml (development set) and, when present,
semantic_layer/eval_questions_holdout.yaml (written separately; not used to tune the planner).

  python -m semantic.rule_eval [tag]
Output: outputs/sl_7_rule_eval_<tag>.json
"""
import json
import sys
from collections import defaultdict
from datetime import date

from ruamel.yaml import YAML

from rag.utils import write_json

from .config import LAYER_DIR
from .evaluate import _rows, execution_match


def score_set(path) -> dict:
    from .answer import fix_values
    from .compiler import compile_query
    from .enrich import load_store
    from .rule_planner import planner
    from .schema import SemanticQuery
    from .yamlio import load_metrics, load_model
    gold = YAML(typ="safe").load(path.read_text(encoding="utf-8"))
    today = date.fromisoformat(str(gold["as_of"]))
    P, model, metrics, store = planner(), load_model(), load_metrics(), load_store()
    kinds, results = defaultdict(list), []
    for q in gold["questions"]:
        if q["kind"] == "route":
            continue
        p = P.plan(q["question"], today)
        row = {"id": q["id"], "kind": q["kind"], "question": q["question"]}
        if q["kind"] == "unanswerable":
            ok = not p.answerable
            row |= {"refused": not p.answerable}
        elif not p.answerable:
            ok = False
            row |= {"refused": True, "reason": p.reason_if_not_answerable}
        else:
            fix_values(p.query, {"values": store["values"]}, store["synonyms"])
            gq = SemanticQuery.model_validate(q["gold"])
            try:
                ok = execution_match(_rows(compile_query(p.query, model, metrics).sql),
                                     _rows(compile_query(gq, model, metrics).sql), p.query.metrics, gq.metrics)
            except Exception as e:                                # noqa: BLE001 - a failure is a wrong answer
                ok, row["error"] = False, str(e)[:200]
            row["request"] = p.query.model_dump(mode="json", exclude_defaults=True)
        row["ok"] = ok
        kinds[q["kind"]].append(ok)
        results.append(row)
    allv = [r["ok"] for r in results]
    ref = kinds.get("unanswerable", [])
    return {"questions": len(allv), "execution_accuracy": round(sum(allv) / len(allv), 3),
            "refusal_accuracy": round(sum(ref) / len(ref), 3) if ref else None,
            "by_kind": {k: f"{sum(v)}/{len(v)}" for k, v in kinds.items()},
            "failures": [r for r in results if not r["ok"]], "results": results}


def run(tag: str = "latest") -> dict:
    sets = {"development": LAYER_DIR / "eval_questions.yaml", "holdout": LAYER_DIR / "eval_questions_holdout.yaml",
            "lumora": LAYER_DIR / "eval_questions_lumora.yaml", "blind2": LAYER_DIR / "eval_questions_blind2.yaml"}
    out = {name: score_set(p) for name, p in sets.items() if p.exists()}
    total = sum(v["questions"] for v in out.values())
    acc = sum(v["execution_accuracy"] * v["questions"] for v in out.values()) / total
    summary = {"questions": total, "execution_accuracy": round(acc, 3),
               "refusal_accuracy": out["development"]["refusal_accuracy"] if "development" in out else None}
    for name, v in out.items():
        summary[f"{name}_accuracy"] = v["execution_accuracy"]
        print(f"  {name}: {v['execution_accuracy']:.3f} on {v['questions']} questions {v['by_kind']}")
    write_json(f"sl_7_rule_eval_{tag}.json", {"summary": summary, "sets": out})
    return {"summary": summary, "sets": out}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    run(sys.argv[1] if len(sys.argv) > 1 else "latest")
