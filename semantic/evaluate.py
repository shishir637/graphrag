"""Evaluate the semantic layer - the data-question counterpart of RAGAS.

  python rag.py sl-eval [--planner claude-haiku-5-5] [--tag name]

Gold set: semantic_layer/eval_questions.yaml (question -> the request the planner should produce).
"Today" is pinned to the set's as_of date so relative dates ("last quarter") are reproducible.

Metrics (deterministic first; no judge model is needed for the numbers)
  execution_accuracy  run the gold request and the planner's request; same rows and values?
                      (equivalent requests written differently still pass; extra metric columns are allowed)
  request_exact       metrics, dimensions, filters and date range all equal to gold
     metric / dimension / filter / date match reported separately, to show WHERE the planner goes wrong
  refusal_accuracy    unanswerable questions are declined instead of answered with a guess
  route_accuracy      documents-vs-data router picks the right side
  number_faithfulness every number in the written answer appears in (or is a rounding of) the result rows
  efficiency          seconds per question, Claude calls and tokens, compile retries

Output: outputs/sl_6_eval_<tag>.json
"""
import json
import re
import statistics
import time
from datetime import date
from pathlib import Path

from ruamel.yaml import YAML

from rag.utils import write_json

from .config import LAYER_DIR
from .schema import SemanticQuery

GOLD_FILE = LAYER_DIR / "eval_questions.yaml"


def load_gold() -> dict:
    return YAML(typ="safe").load(GOLD_FILE.read_text(encoding="utf-8"))


def _rows(sql: str) -> list[dict]:
    from .db import connect
    df = connect().execute(sql).df()
    return json.loads(df.to_json(orient="records", date_format="iso"))


def check_gold() -> list[str]:
    """Every gold request must compile and run (used by the tests and before an eval run)."""
    from .compiler import compile_query
    from .yamlio import load_metrics, load_model
    model, metrics = load_model(), load_metrics()
    problems = []
    for q in load_gold()["questions"]:
        if "gold" in q:
            try:
                _rows(compile_query(SemanticQuery.model_validate(q["gold"]), model, metrics).sql)
            except Exception as e:                                    # noqa: BLE001 - report every problem
                problems.append(f"{q['id']}: {str(e).splitlines()[0]}")
    return problems


# ---------- comparisons ----------
def _norm_filters(fs) -> set:
    return {(f["dimension"], f.get("op", "="), tuple(sorted(str(v).lower() for v in f["values"]))) for f in fs or []}


def request_match(pred: dict | None, gold: dict) -> dict:
    if pred is None:
        return {"metrics": False, "dimensions": False, "filters": False, "dates": False, "exact": False}
    m = {
        "metrics": set(pred.get("metrics", [])) == set(gold.get("metrics", [])),
        "dimensions": set(pred.get("dimensions", [])) == set(gold.get("dimensions", [])),
        "filters": _norm_filters(pred.get("filters")) == _norm_filters(gold.get("filters")),
        "dates": (str(pred.get("start_date")), str(pred.get("end_date"))) ==
                 (str(gold.get("start_date")), str(gold.get("end_date"))),
    }
    m["exact"] = all(m.values())
    return m


def _close(a, b) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= max(0.01, 1e-6 * abs(b))
    return str(a) == str(b)


def execution_match(pred_rows: list[dict], gold_rows: list[dict], pred_metrics: list[str], gold_metrics: list[str]) -> bool:
    """Same groups and the same values for every gold metric. Extra metric columns in the prediction are fine;
    a different grouping (other or missing dimension columns) is not."""
    if not gold_rows:
        return not pred_rows
    if not pred_rows:
        return False
    gold_dims = [c for c in gold_rows[0] if c not in gold_metrics]
    pred_dims = [c for c in pred_rows[0] if c not in pred_metrics]
    if set(gold_dims) != set(pred_dims) or not set(gold_metrics) <= set(pred_rows[0]):
        return False
    key = lambda r: tuple(str(r[d]) for d in sorted(gold_dims))            # noqa: E731
    g = {key(r): r for r in gold_rows}
    p = {key(r): r for r in pred_rows}
    if set(g) != set(p):
        return False
    return all(_close(p[k][m], g[k][m]) for k in g for m in gold_metrics)


_NUM = re.compile(r"(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?")


def number_faithfulness(answer: str, rows: list[dict], question: str) -> float | None:
    """Share of numbers in the written answer that come from the result rows (allowing rounding,
    percentages and lakh/crore). Years and numbers copied from the question are ignored."""
    values = [v for r in rows for v in r.values() if isinstance(v, (int, float)) and v is not None]
    cols = {k for r in rows for k, v in r.items() if isinstance(v, (int, float))}
    values += [sum(r[k] for r in rows if isinstance(r.get(k), (int, float))) for k in cols]   # totals across rows
    q_nums = {n.replace(",", "") for n in _NUM.findall(question)}
    month = re.compile(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*", re.I)
    answer = re.sub(r"\d{4}-\d{2}-\d{2}", " ", answer)                      # ISO dates are not quantities
    answer = re.sub(r"\d{4}-\d{2}(?!\d)", " ", answer)                       # nor fiscal years: FY2024-25
    nums = []
    for match in _NUM.finditer(answer):
        tok = match.group(0)
        near = answer[max(0, match.start() - 12): match.end() + 12]
        if month.search(near) and tok.isdigit() and int(tok) <= 31:
            continue                                                  # a calendar day: "18 November"
        t = tok.replace(",", "")
        try:
            n = float(t)
        except ValueError:
            continue
        if t in q_nums or (n.is_integer() and 1900 <= n <= 2100) or (n.is_integer() and 0 <= n <= 12):
            continue                                                  # years, month numbers, small ordinals
        nums.append(n)
    if not nums:
        return None

    def ok(n):
        for v in values:
            for cand in (v, v * 100, v / 1e5, v / 1e6, v / 1e7):       # as is, percent, lakh, million, crore
                if abs(n - cand) <= max(0.51, 0.006 * abs(cand)):
                    return True
        return False
    return round(sum(ok(n) for n in nums) / len(nums), 3)


def rescore(tag: str) -> dict:
    """Recompute number faithfulness of a saved run with the current checker (rows are rebuilt from the
    saved requests - deterministic, no model calls)."""
    from .compiler import compile_query
    from .yamlio import load_metrics, load_model
    path = Path(__file__).resolve().parent.parent / "outputs" / f"sl_6_eval_{tag}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    model, metrics = load_model(), load_metrics()
    scores = []
    for r in data["results"]:
        if r.get("request") and "number_faithfulness" in r:
            rows = _rows(compile_query(SemanticQuery.model_validate(r["request"]), model, metrics).sql)
            r["number_faithfulness"] = number_faithfulness(r["answer"], rows, r["question"])
            scores.append(r["number_faithfulness"])
    vals = [s for s in scores if s is not None]
    data["summary"]["number_faithfulness"] = round(statistics.mean(vals), 3) if vals else None
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return data["summary"]


# ---------- run ----------
def run(tag: str = "baseline", planner: str | None = None) -> dict:
    import os

    from . import answer as A
    from .compiler import compile_query
    from .yamlio import load_metrics, load_model

    gold = load_gold()
    os.environ["SL_TODAY"] = str(gold["as_of"])
    os.environ["SL_PLAN_CACHE"] = "0"                    # measure the planner, not the cache
    if planner:
        os.environ["SL_PLANNER_MODEL"] = planner
    problems = check_gold()
    if problems:
        raise SystemExit("gold set does not compile:\n  " + "\n  ".join(problems))
    model, metrics = load_model(), load_metrics()
    usage0 = dict(A.USAGE)
    results, t_run = [], time.perf_counter()
    for q in gold["questions"]:
        t0 = time.perf_counter()
        row = {"id": q["id"], "kind": q["kind"], "question": q["question"]}
        if q["kind"] == "route":
            got = A.route_question(q["question"])
            row |= {"route": got, "route_ok": got == q["route"], "seconds": round(time.perf_counter() - t0, 2)}
            results.append(row)
            print(f"  {q['id']} route {'OK ' if row['route_ok'] else 'BAD'} {got}", flush=True)
            continue
        try:
            rec = A.answer_data_question(q["question"])
        except Exception as e:                                        # noqa: BLE001 - a crash is a failed answer
            rec = {"answer": f"ERROR: {e}", "found": False, "timings_s": {}}
        pred = rec.get("request")
        row |= {"answer": rec["answer"], "request": pred, "value_corrections": rec.get("value_corrections", []),
                "answer_method": rec.get("answer_method"),
                "compile_retries": len(rec.get("compile_errors", [])), "seconds": round(time.perf_counter() - t0, 2)}
        if q["kind"] == "unanswerable":
            row["refused_ok"] = pred is None
        else:
            g = q["gold"]
            gold_rows = _rows(compile_query(SemanticQuery.model_validate(g), model, metrics).sql)
            pred_rows = rec.get("rows", [])
            row["request_match"] = request_match(pred, g)
            row["execution_ok"] = execution_match(pred_rows, gold_rows, (pred or {}).get("metrics", []), g["metrics"])
            row["number_faithfulness"] = number_faithfulness(rec["answer"], pred_rows, q["question"])
        results.append(row)
        flag = row.get("execution_ok", row.get("refused_ok"))
        print(f"  {q['id']} {q['kind']:<12} {'OK ' if flag else 'BAD'} {row['seconds']:>5}s", flush=True)

    def avg(xs):
        xs = [x for x in xs if x is not None]
        return round(statistics.mean(xs), 3) if xs else None
    data_rows = [r for r in results if "execution_ok" in r]
    kinds = sorted({r["kind"] for r in data_rows})
    usage = {k: A.USAGE[k] - usage0[k] for k in A.USAGE}
    answered = [r for r in results if r["kind"] != "route"]
    summary = {
        "tag": tag, "planner_model": A.planner_model(), "as_of": str(gold["as_of"]),
        "n_questions": len(results),
        "execution_accuracy": avg([float(r["execution_ok"]) for r in data_rows]),
        "request_exact": avg([float(r["request_match"]["exact"]) for r in data_rows]),
        "request_parts": {p: avg([float(r["request_match"][p]) for r in data_rows])
                          for p in ("metrics", "dimensions", "filters", "dates")},
        "by_kind": {k: {"n": sum(r["kind"] == k for r in data_rows),
                        "execution_accuracy": avg([float(r["execution_ok"]) for r in data_rows if r["kind"] == k])}
                    for k in kinds},
        "refusal_accuracy": avg([float(r["refused_ok"]) for r in results if "refused_ok" in r]),
        "route_accuracy": avg([float(r["route_ok"]) for r in results if "route_ok" in r]),
        "number_faithfulness": avg([r.get("number_faithfulness") for r in data_rows]),
        "answer_methods": {m: sum(r.get("answer_method") == m for r in data_rows)
                           for m in sorted({str(r.get("answer_method")) for r in data_rows})},
        "efficiency": {
            "seconds_per_question_p50": round(statistics.median([r["seconds"] for r in answered]), 2),
            "seconds_per_question_mean": round(statistics.mean([r["seconds"] for r in answered]), 2),
            "claude_calls": usage["calls"], "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
            "tokens_per_question": round((usage["input_tokens"] + usage["output_tokens"]) / len(results)),
            "compile_retries": sum(r.get("compile_retries", 0) for r in results),
            "total_seconds": round(time.perf_counter() - t_run, 1),
        },
        "failures": [{"id": r["id"], "question": r["question"], "request": r.get("request"),
                      "match": r.get("request_match")} for r in data_rows if not r["execution_ok"]]
                    + [{"id": r["id"], "question": r["question"], "request": r.get("request")}
                       for r in results if r.get("refused_ok") is False or r.get("route_ok") is False],
    }
    write_json(f"sl_6_eval_{tag}.json", {"summary": summary, "results": results})
    print(json.dumps({k: v for k, v in summary.items() if k != "failures"}, indent=1))
    print(f"  {len(summary['failures'])} failures -> outputs/sl_6_eval_{tag}.json")
    return summary
