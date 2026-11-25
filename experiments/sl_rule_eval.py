import json, sys, collections
from datetime import date
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent; sys.path.insert(0, str(ROOT))
from semantic.evaluate import load_gold, request_match, execution_match, _rows
from semantic.compiler import compile_query
from semantic.schema import SemanticQuery
from semantic.yamlio import load_model, load_metrics
from semantic.rule_planner import planner
from semantic.answer import fix_values
from semantic.enrich import load_store
import os
from ruamel.yaml import YAML
g = YAML(typ="safe").load(open(sys.argv[1])) if len(sys.argv) > 1 else load_gold(); today = date.fromisoformat(str(g["as_of"]))
P = planner(); model, metrics = load_model(), load_metrics(); store = load_store()
res = collections.defaultdict(list); fails = []
for q in g["questions"]:
    if q["kind"] == "route": continue
    p = P.plan(q["question"], today)
    if q["kind"] == "unanswerable":
        ok = not p.answerable; res["refusal"].append(ok); res["all"].append(ok)
        if not ok: fails.append((q["id"], q["question"], p.query.model_dump(mode="json", exclude_defaults=True)))
        continue
    if not p.answerable:
        res[q["kind"]].append(False); res["all"].append(False); fails.append((q["id"], q["question"], p.reason_if_not_answerable)); continue
    fix_values(p.query, {"values": store["values"]}, store["synonyms"])
    pred = p.query.model_dump(mode="json", exclude_defaults=True)
    gold_q = SemanticQuery.model_validate(q["gold"])
    try:
        ok = execution_match(_rows(compile_query(p.query, model, metrics).sql), _rows(compile_query(gold_q, model, metrics).sql), p.query.metrics, gold_q.metrics)
    except Exception as e:
        ok = False; pred["error"] = str(e)
    res[q["kind"]].append(ok); res["all"].append(ok)
    if not ok: fails.append((q["id"], q["question"], pred, q["gold"]))
summary = {k: f"{sum(v)}/{len(v)}" for k, v in res.items()}
print(json.dumps(summary)); print(f"execution accuracy (incl. refusals): {sum(res['all'])/len(res['all']):.3f}")
for f in fails: print("FAIL", *f)
