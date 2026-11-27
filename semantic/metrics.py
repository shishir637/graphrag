"""Phase 3 - Metric definition: write each business number once, exactly.

  validate : SQLGlot parses every formula and filter; every column must exist in the model's tables;
             every table a metric touches must be reachable through safe joins; each metric compiles
             and DuckDB accepts the SQL (EXPLAIN).
  reconcile: compute net_revenue and order_count per month through the semantic layer and compare
             them with the finance team's report (pandas) - the "does our number match finance" test.
  draft    : Claude proposes more metrics from the model's measures, as status: draft, for review.

Output: outputs/sl_3_metrics_check.json  (+ outputs/sl_3_metrics.draft.yaml with --draft)
"""
import pandas as pd
import sqlglot
from pydantic import BaseModel, ValidationError

from rag.utils import write_json

from .compiler import CompileError, columns_in, compile_query
from .config import OUT_DIR, REFERENCE_DIR
from .db import columns, connect
from .model import join_path
from .schema import Metric, MetricSet, SemanticModel, SemanticQuery
from .yamlio import load_metrics, load_model, write

REPORT = REFERENCE_DIR / "finance_monthly_report.csv"
RECONCILE = {"net_revenue": 0.01, "order_count": 0}         # metric -> allowed absolute difference


def validate(model: SemanticModel, metrics: MetricSet, con) -> list[str]:
    problems = []
    table_cols = {e.name: set(columns(con, e.table)) for e in model.entities}
    names = {m.name for m in metrics.metrics}
    for m in metrics.metrics:
        where = f"metric '{m.name}'"
        if m.entity not in table_cols:
            problems.append(f"{where}: unknown entity '{m.entity}'")
            continue
        sql_parts = [p for p in [m.expression if m.type != "derived" else None, *m.filters] if p]
        for part in sql_parts:
            try:
                cols = columns_in(part)
            except sqlglot.errors.ParseError as e:
                problems.append(f"{where}: does not parse: {part!r} ({str(e).splitlines()[0]})")
                continue
            for ent, col in cols:
                if not ent:
                    problems.append(f"{where}: column '{col}' must be written as entity.column")
                elif ent not in table_cols:
                    problems.append(f"{where}: unknown entity '{ent}' in {part!r}")
                elif col not in table_cols[ent]:
                    problems.append(f"{where}: column '{ent}.{col}' does not exist")
                elif ent != m.entity:
                    try:
                        join_path(model, m.entity, ent)
                    except ValueError as e:
                        problems.append(f"{where}: {e}")
        if m.type == "ratio":
            for ref in (m.numerator, m.denominator):
                if ref not in names:
                    problems.append(f"{where}: refers to unknown metric '{ref}'")
        if m.time_column:
            ent, _, col = m.time_column.partition(".")
            if col not in table_cols.get(ent, set()):
                problems.append(f"{where}: time_column '{m.time_column}' does not exist")
        if not any(p.startswith(where) for p in problems):
            q = SemanticQuery(metrics=[m.name],
                              dimensions=[f"{m.time_column}__month"] if m.type == "cumulative" else [])
            try:
                con.execute("EXPLAIN " + compile_query(q, model, metrics).sql)
            except (CompileError, Exception) as e:
                problems.append(f"{where}: does not compile/run: {str(e).splitlines()[0]}")
    return problems


def reconcile(model, metrics, con) -> dict:
    """Compare with the finance report of the sample data (only when that data and its metrics are loaded)."""
    names = {m.name for m in metrics.metrics}
    if not REPORT.exists() or not set(RECONCILE) <= names or "orders" not in {e.name for e in model.entities}:
        return {"skipped": "no finance report to compare with"}
    report = pd.read_csv(REPORT, parse_dates=["month"])
    q = SemanticQuery(metrics=list(RECONCILE), dimensions=["orders.order_date__month"])
    ours = con.execute(compile_query(q, model, metrics).sql).df()
    ours["month"] = pd.to_datetime(ours["order_date_month"])
    merged = report.merge(ours, on="month", how="outer", suffixes=("_finance", "_semantic"))
    result = {"months_compared": len(merged), "metrics": {}}
    for name, tol in RECONCILE.items():
        col = {"net_revenue": "net_revenue", "order_count": "orders"}[name]
        fin = merged[f"{col}_finance" if col == "net_revenue" else col]
        sem = merged[f"{name}_semantic" if name == "net_revenue" else name]
        diff = (sem.astype(float) - fin.astype(float)).abs()
        bad = merged[(diff > tol) | diff.isna()]
        result["metrics"][name] = {
            "finance_total": round(float(fin.sum()), 2), "semantic_total": round(float(sem.sum()), 2),
            "max_monthly_difference": round(float(diff.max()), 4), "tolerance": tol,
            "matches": bad.empty, "mismatched_months": [str(d.date()) for d in bad["month"]],
        }
    return result


# --- Claude proposes metrics for review ---
class _Proposed(BaseModel):
    name: str
    label: str
    description: str
    type: str
    entity: str
    expression: str | None = None
    numerator: str | None = None
    denominator: str | None = None
    filters: list[str] = []
    time_column: str | None = None
    format: str = "number"


class _Proposals(BaseModel):
    metrics: list[_Proposed]


def draft_metrics(n: int = 8) -> list[dict]:
    from rag.config import CLAUDE_MODEL
    from rag.llm import claude
    from .config import INSTRUCTIONS_FILE

    model, metrics, con = load_model(), load_metrics(), connect()
    catalog = "\n".join(
        f"{e.name} ({e.description}) measures: " + ", ".join(f"{x.agg}({e.name}.{x.column})" for x in e.measures) +
        "; dimensions: " + ", ".join(f"{e.name}.{d.column} [{d.type}]" for d in e.dimensions)
        for e in model.entities)
    existing = "\n".join(f"- {m.name}: {m.description}" for m in metrics.metrics)
    rules = INSTRUCTIONS_FILE.read_text(encoding="utf-8") if INSTRUCTIONS_FILE.exists() else ""
    resp = claude().messages.parse(
        model=CLAUDE_MODEL, max_tokens=16000, output_config={"effort": "low"},
        messages=[{"role": "user", "content":
                   f"Propose up to {n} additional business metrics for this semantic layer that are NOT already defined. "
                   "Use only the columns listed, written as entity.column in DuckDB SQL. type is simple (one aggregate "
                   "expression), ratio (numerator/denominator are existing metric names), derived ({metric} references) "
                   "or cumulative. Follow the business rules (e.g. exclude cancelled orders). name must be snake_case.\n\n"
                   f"<model>\n{catalog}\n</model>\n<existing_metrics>\n{existing}\n</existing_metrics>\n"
                   f"<business_rules>\n{rules}\n</business_rules>"}],
        output_format=_Proposals,
    )
    drafts, out = [], []
    for p in resp.parsed_output.metrics:
        d = p.model_dump() | {"owner": "", "status": "draft"}
        try:
            m = Metric.model_validate(d)
            trial = MetricSet(metrics=[*metrics.metrics, m])
            issues = [x for x in validate(model, trial, con) if f"'{m.name}'" in x]
        except (ValidationError, ValueError) as e:
            issues = [str(e).splitlines()[0]]
        d["review_notes"] = issues or ["parses, columns exist, compiles - check the business meaning"]
        drafts.append(d)
        out.append((d["name"], "OK" if not issues else "PROBLEM"))
    path = OUT_DIR / "sl_3_metrics.draft.yaml"
    write(path, {"version": 1, "metrics": drafts},
          header="DRAFT metrics proposed by Claude. Review each one; copy approved ones into "
                 "semantic_layer/metrics.yaml with status: approved and an owner.")
    for name, status in out:
        print(f"  draft {name}: {status}")
    print(f"  -> wrote {path.relative_to(OUT_DIR.parent)}")
    return drafts


def check_metrics() -> dict:
    model, metrics, con = load_model(), load_metrics(), connect()
    problems = validate(model, metrics, con)
    rec = reconcile(model, metrics, con) if not problems else {"skipped": "fix validation problems first"}
    result = {
        "metrics": len(metrics.metrics),
        "approved": sum(m.status == "approved" for m in metrics.metrics),
        "problems": problems, "reconciliation": rec,
    }
    write_json("sl_3_metrics_check.json", result)
    print(f"  {result['metrics']} metrics, {result['approved']} approved, {len(problems)} problems")
    for p in problems:
        print("   -", p)
    for name, r in rec.get("metrics", {}).items():
        print(f"  reconcile {name}: {'MATCH' if r['matches'] else 'MISMATCH'} "
              f"(finance {r['finance_total']:,} vs semantic {r['semantic_total']:,}, "
              f"max monthly diff {r['max_monthly_difference']})")
    return result
