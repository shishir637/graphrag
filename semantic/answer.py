"""Answering data questions through the semantic layer, and routing between documents and data.

  1. route      : Claude decides whether a question is about the documents (-> RAG) or about numbers
                  in the tables (-> semantic layer)
  2. context    : the catalog of metrics and dimensions, synonyms that appear in the question, the
                  closest verified questions, and the business rules (instructions.md)
                    - small catalog (<= FULL_CATALOG_MAX items): send all of it; no embedding model needed
                    - large catalog: retrieve the closest items from the Qdrant 'semantic' collection
  3. plan       : Claude fills in a SemanticQuery (metric and dimension NAMES only, never SQL)
  4. fix values : filter values are checked against the stored value lists; synonyms and RapidFuzz
                  correct them ("Bangalor" -> "Bengaluru")
  5. compile+run: the compiler writes the SQL, DuckDB runs it
  6. summarise  : Claude states the result in one or two sentences, using only the returned rows

Output: outputs/sl_5_answer.json and a line in outputs/query_log.jsonl
"""
import json
import os
import re
import time
from datetime import date, datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field
from rapidfuzz import fuzz, process

from rag.config import CLAUDE_MODEL, OUT_DIR
from rag.utils import write_json

from .compiler import CompileError, compile_query
from .config import SEMANTIC_COLLECTION, VALUE_MATCH_MIN_SCORE
from .db import connect
from .enrich import catalog_items, load_store
from .schema import SemanticQuery
from .yamlio import load_enrichment, load_metrics, load_model, load_verified

FULL_CATALOG_MAX = 150          # above this many items, retrieve from Qdrant instead of sending everything
USAGE = {"input_tokens": 0, "output_tokens": 0, "calls": 0}     # running token count (read by the evaluator)


def _claude():
    from rag.llm import claude
    return claude()


def _track(resp):
    USAGE["input_tokens"] += resp.usage.input_tokens
    USAGE["output_tokens"] += resp.usage.output_tokens
    USAGE["calls"] += 1
    return resp


def today() -> date:
    """Today's date; SL_TODAY=YYYY-MM-DD pins it (evaluation runs resolve 'last quarter' reproducibly)."""
    v = os.environ.get("SL_TODAY")
    return date.fromisoformat(v) if v else date.today()


def planner_model() -> str:
    return os.environ.get("SL_PLANNER_MODEL", CLAUDE_MODEL)


# ---------- 1. routing ----------
class _Route(BaseModel):
    route: Literal["documents", "data"]
    reason: str


def route_question(question: str) -> str:
    metrics = load_metrics()
    docs = "the documents in the documents/ folder"
    try:
        from rag.utils import read_json
        docs = ", ".join(read_json("4_index.json")["sources"])
    except Exception:
        pass
    resp = _track(_claude().messages.parse(
        model=planner_model(), max_tokens=2000, output_config={"effort": "low"},
        messages=[{"role": "user", "content":
                   "Decide where this question should be answered.\n"
                   f"- documents: text documents ({docs}): facts, descriptions, policies, people, how things work.\n"
                   "- data: numbers computed from the sales tables (orders, customers, products, regions), e.g. "
                   "totals, counts, trends, rankings. Available metrics: "
                   + ", ".join(m.label for m in metrics.metrics) +
                   f"\n\nQuestion: {question}"}],
        output_format=_Route,
    ))
    return "data" if resp.parsed_output.route == "data" else "docs"


# ---------- 2. context ----------
def _synonym_hits(question: str, synonyms: list[tuple[str, str]]) -> list[str]:
    q = question.lower()
    hits = []
    for term, target in synonyms:
        if fuzz.partial_ratio(term.lower(), q) >= 90 and len(term) >= 3:
            hits.append(f'"{term}" means {target}')
    return hits


def build_context(question: str, force_vector_search: bool = False) -> dict:
    model, metrics, enrichment, verified = load_model(), load_metrics(), load_enrichment(), load_verified()
    items = catalog_items(model, metrics, enrichment, verified)
    catalog = [i for i in items if i["kind"] != "verified"]
    examples = [i for i in items if i["kind"] == "verified"]
    use_vectors = force_vector_search or os.environ.get("SL_VECTOR_SEARCH") == "1" or len(catalog) > FULL_CATALOG_MAX

    if use_vectors:
        from qdrant_client import models as qm

        from rag.models import embed_dense
        from rag.store import client
        qv = embed_dense([question])[0]

        def top(kind, k):
            return [p.payload for p in client().query_points(
                SEMANTIC_COLLECTION, query=qv, using="dense", limit=k, with_payload=True,
                query_filter=qm.Filter(must=[qm.FieldCondition(key="kind", match=qm.MatchValue(value=kind))])).points]
        picked = top("metric", 8) + top("dimension", 12)
        times = [i for i in catalog if "Time dimension" in i["text"] and i not in picked]
        catalog = picked + times                      # time dimensions are always offered
        examples = top("verified", 3)
        method = "qdrant vector search"
    else:
        ranked = process.extract(question, [e["text"] for e in examples], scorer=fuzz.token_set_ratio, limit=3)
        examples = [examples[i] for _, _, i in ranked]
        method = "full catalog (small) + fuzzy example match"

    store = load_store()
    from .dates import resolve
    return {"catalog": catalog, "examples": examples, "synonym_hints": _synonym_hits(question, store["synonyms"]),
            "instructions": store["instructions"], "values": store["values"], "method": method,
            "date_hints": resolve(question, today())}


# ---------- 3. plan ----------
class _Plan(BaseModel):
    answerable: bool = Field(description="false if no metric in the catalog can answer the question")
    query: SemanticQuery | None = None
    assumptions: list[str] = Field(default_factory=list,
                                   description="how ambiguous words were interpreted, e.g. date ranges chosen")
    reason_if_not_answerable: str = ""


def _plan_prompt(question: str, ctx: dict) -> str:
    cat = "\n".join(f"- {i['text']}" for i in ctx["catalog"])
    ex = "\n".join(f"Q: {e['ref']}\nrequest: {json.dumps(e['query'])}" for e in ctx["examples"])
    hints = "\n".join(f"- {h}" for h in ctx["synonym_hints"]) or "- (none)"
    dates = "\n".join(f"- \"{d['phrase']}\" = {d['start_date']} to {d['end_date']}" for d in ctx.get("date_hints", []))
    date_block = (f"<dates_resolved_by_business_rules>\n{dates}\n</dates_resolved_by_business_rules>\n"
                  "Use these date ranges exactly for those phrases.\n\n") if dates else ""
    return (f"Today's date is {today().isoformat()}.\n\n"
            f"<business_rules>\n{ctx['instructions']}\n</business_rules>\n\n"
            f"<catalog>\n{cat}\n</catalog>\n\n<synonyms_found_in_question>\n{hints}\n</synonyms_found_in_question>\n\n"
            f"<verified_examples>\n{ex}\n</verified_examples>\n\n{date_block}"
            "Turn the question into a semantic-layer request. Use ONLY metric names and dimension references from "
            "the catalog (dimensions as entity.dimension, time grains as entity.time_dimension__grain). Put date "
            "ranges in start_date/end_date (inclusive), resolved against today's date. Filter values should be the "
            "user's words; they are matched to real values afterwards. If nothing in the catalog can answer it, set "
            f"answerable=false.\n\nQuestion: {question}")


def _layer_version() -> str:
    """Fingerprint of the reviewed layer: any change to a definition invalidates cached plans."""
    import hashlib
    from .config import LAYER_DIR
    h = hashlib.sha1()
    for f in sorted(LAYER_DIR.glob("*")):
        if f.is_file() and f.name != "eval_questions.yaml":
            h.update(f.read_bytes())
    return h.hexdigest()[:12]


def _plan_cache_key(question: str) -> str:
    import hashlib
    q = re.sub(r"\s+", " ", question.lower().strip(" ?.!"))
    return hashlib.sha1(f"{q}|{today()}|{planner_model()}|{_layer_version()}".encode()).hexdigest()[:20]


def cached_plan(question: str, ctx: dict) -> tuple["_Plan", list[str], bool]:
    """Plan cache: the same question against the same layer and day reuses the plan (no Claude call).
    SL_PLAN_CACHE=0 turns it off (the evaluator does, to measure the planner itself)."""
    from .config import STORE_DIR
    use = os.environ.get("SL_PLAN_CACHE", "1") != "0"
    path = STORE_DIR / "plan_cache.json"
    cache = json.loads(path.read_text(encoding="utf-8")) if use and path.exists() else {}
    key = _plan_cache_key(question)
    if use and key in cache:
        return _Plan.model_validate(cache[key]), [], True
    plan, errors = plan_query(question, ctx)
    if use and not errors:
        STORE_DIR.mkdir(exist_ok=True)
        cache[key] = plan.model_dump(mode="json")
        path.write_text(json.dumps(cache), encoding="utf-8")
    return plan, errors, False


def apply_date_hints(plan: "_Plan", ctx: dict) -> list[str]:
    """One unambiguous date phrase in the question wins over the planner's own date arithmetic."""
    hints = ctx.get("date_hints", [])
    if len(hints) != 1 or plan.query is None:
        return []
    h = hints[0]
    q = plan.query
    if (q.start_date, q.end_date) != (h["start_date"], h["end_date"]):
        old = f"{q.start_date}..{q.end_date}"
        q.start_date, q.end_date = h["start_date"], h["end_date"]
        return [f"dates for \"{h['phrase']}\": {old} -> {h['start_date']}..{h['end_date']}"]
    return []


def plan_query(question: str, ctx: dict) -> tuple[_Plan, list[str]]:
    """Ask Claude for a request; if it does not compile, send the error back once to fix it."""
    model, metrics = load_model(), load_metrics()
    messages = [{"role": "user", "content": _plan_prompt(question, ctx)}]
    errors = []
    for _ in range(2):
        resp = _track(_claude().messages.parse(model=planner_model(), max_tokens=4000, output_config={"effort": "low"},
                                               messages=messages, output_format=_Plan))
        plan = resp.parsed_output
        if not plan.answerable or plan.query is None:
            return plan, errors
        try:
            compile_query(plan.query, model, metrics)
            return plan, errors
        except CompileError as e:
            errors.append(str(e))
            messages += [{"role": "assistant", "content": plan.model_dump_json()},
                         {"role": "user", "content": f"That request does not compile: {e}\nReturn a corrected request."}]
    return plan, errors


# ---------- 4. fix filter values ----------
def fix_values(q: SemanticQuery, ctx: dict, synonyms: list[tuple[str, str]]) -> list[str]:
    notes = []
    value_syn = {t.lower(): tgt.split("=", 1) for t, tgt in synonyms if "=" in tgt}
    for f in q.filters:
        known = ctx["values"].get(f.dimension)
        if not known:
            continue
        fixed = []
        for v in f.values:
            sv = str(v)
            if sv in known:
                fixed.append(sv)
                continue
            exact = next((k for k in known if k.lower() == sv.lower()), None)
            syn = value_syn.get(sv.lower())
            if exact:
                new = exact
            elif syn and syn[0] == f.dimension:
                new = syn[1]
            else:   # closest real value OR closest synonym term ("Bangalor" ~ "Bangalore" -> Bengaluru)
                options = {k: k for k in known}
                options |= {t: val for t, (dim, val) in value_syn.items() if dim == f.dimension}
                lowered = {k.lower(): v for k, v in options.items()}
                match = process.extractOne(sv.lower(), list(lowered), scorer=fuzz.WRatio)
                new = lowered[match[0]] if match and match[1] >= VALUE_MATCH_MIN_SCORE else None
            if new and new != sv:
                notes.append(f"{f.dimension}: '{sv}' -> '{new}'")
            if not new:
                notes.append(f"{f.dimension}: '{sv}' is not a known value (kept as is; expect no rows)")
            fixed.append(new or sv)
        f.values = fixed
    return notes


# ---------- 6. summarise ----------
def summarise(question: str, rows_md: str, assumptions: list[str]) -> str:
    resp = _track(_claude().messages.create(
        model=planner_model(), max_tokens=2000, output_config={"effort": "low"},
        system="State the answer to the question in one to three sentences using ONLY the numbers in the result "
               "table. Money is INR: write amounts as plain numbers with thousands separators (e.g. ₹5,917,234); "
               "never convert to lakh, crore or million unless the question asks. Quote numbers as they appear in the "
               "table (rounding to whole rupees is fine). Mention an assumption only if it changes the meaning. "
               "If the table is empty, say no matching data was found.",
        messages=[{"role": "user", "content": f"Question: {question}\nAssumptions: {assumptions}\n\nResult:\n{rows_md}"}],
    ))
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def _fmt(value, fmt: str) -> str:
    if value is None:
        return "no data"
    if fmt == "currency":
        return f"₹{value:,.0f}"
    if fmt == "percent":
        return f"{value * 100:.1f}%" if abs(value) <= 1 else f"{value:.1f}%"
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"


def template_answer(q: SemanticQuery, rows: list[dict]) -> str | None:
    """One-row results need no model: state each metric with its label and format (no tokens, no misquoted numbers)."""
    if len(rows) != 1 or q.dimensions:
        return None
    by_name = {m.name: m for m in load_metrics().metrics}
    parts = [f"{by_name[m].label}: {_fmt(rows[0].get(m), by_name[m].format)}" for m in q.metrics if m in by_name]
    period = (f" ({q.start_date} to {q.end_date})" if q.start_date and q.end_date
              else f" (from {q.start_date})" if q.start_date else "")
    where = "; ".join(f"{f.dimension.split('.')[-1]} {f.op} {', '.join(map(str, f.values))}" for f in q.filters)
    return "; ".join(parts) + period + (f", where {where}" if where else "") + "."


def answer_data_question(question: str, force_vector_search: bool = False) -> dict:
    from .quality import checks
    t0 = time.perf_counter()
    ctx = build_context(question, force_vector_search)
    t1 = time.perf_counter()
    plan, errors, from_cache = cached_plan(question, ctx)
    date_notes = apply_date_hints(plan, ctx)
    t2 = time.perf_counter()
    record = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "route": "data",
              "question": question, "context_method": ctx["method"], "synonym_hints": ctx["synonym_hints"],
              "date_hints": [f"{d['phrase']}={d['start_date']}..{d['end_date']}" for d in ctx["date_hints"]],
              "examples_used": [e["ref"] for e in ctx["examples"]], "compile_errors": errors,
              "plan_from_cache": from_cache}
    if not plan.answerable or plan.query is None:
        record |= {"answer": f"The semantic layer cannot answer this: {plan.reason_if_not_answerable}",
                   "found": False}
    else:
        from .evaluate import number_faithfulness
        store_syn = load_store()["synonyms"]
        value_notes = fix_values(plan.query, ctx, store_syn) + date_notes
        model, metrics = load_model(), load_metrics()
        compiled = compile_query(plan.query, model, metrics)
        df = connect().execute(compiled.sql).df()
        t3 = time.perf_counter()
        rows_md = df.head(50).to_markdown(index=False, floatfmt=",.2f", intfmt=",") if not df.empty else "(no rows)"
        rows = json.loads(df.head(200).to_json(orient="records", date_format="iso"))
        answer, answer_method = template_answer(plan.query, rows), "template"
        if answer is None:
            answer, answer_method = summarise(question, rows_md, plan.assumptions), "llm"
            if (number_faithfulness(answer, rows, question) or 1.0) < 1.0:          # number guard: one retry
                answer = summarise(question, rows_md + "\n\nQuote numbers exactly as in the table.", plan.assumptions)
                answer_method = "llm-retry"
                if (number_faithfulness(answer, rows, question) or 1.0) < 1.0:
                    answer, answer_method = f"Result:\n{rows_md}", "table"
        quality = checks(today())
        record |= {
            "answer_method": answer_method, "data_warnings": quality["warnings"],
            "found": not df.empty, "answer": answer, "request": plan.query.model_dump(mode="json", exclude_defaults=True),
            "assumptions": plan.assumptions, "value_corrections": value_notes, "sql": compiled.sql,
            "joins": compiled.joins, "rows": rows,
            "row_count": len(df), "table": rows_md,
        }
        record["timings_s"] = {"context": round(t1 - t0, 3), "plan": round(t2 - t1, 3),
                               "compile_run": round(t3 - t2, 3), "summary": round(time.perf_counter() - t3, 3)}
    record.setdefault("timings_s", {})["total"] = round(time.perf_counter() - t0, 3)
    write_json("sl_5_answer.json", record)
    with open(OUT_DIR / "query_log.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({k: v for k, v in record.items() if k not in ("rows", "table")}, ensure_ascii=False) + "\n")
    return record


def print_data_answer(r: dict):
    print("\n" + r["answer"])
    if r.get("table"):
        print("\n" + r["table"])
    if r.get("request"):
        print(f"\n  request: {json.dumps(r['request'])}")
    for a in r.get("assumptions", []):
        print(f"  assumption: {a}")
    for w in r.get("data_warnings", []):
        print(f"  data warning: {w}")
    for v in r.get("value_corrections", []):
        print(f"  value fixed: {v}")
    if r.get("sql"):
        print("\n  SQL:\n" + "\n".join("    " + line for line in r["sql"].splitlines()))
    print(f"  ({r['timings_s']['total']}s total, context: {r['context_method']})")
