"""Review checks for the semantic layer. CI runs these on every pull request and blocks the merge
if one fails. They need only the YAML files, data/ and reference/ - no models, no API key.

  python -m pytest tests -q
  python -m pytest tests -q --update-golden     # after an intended SQL change, rewrite tests/golden/
"""
import json
from pathlib import Path

import pytest
import sqlglot

from semantic.compiler import CompileError, compile_query
from semantic.enrich import _check_synonyms, _value_lists
from semantic.metrics import reconcile, validate
from semantic.schema import SemanticQuery
from semantic.yamlio import load_enrichment, load_verified

GOLDEN = Path(__file__).parent / "golden"
SAMPLE_REQUESTS = {
    "net_revenue_by_region_fy": {"metrics": ["net_revenue", "order_count"], "dimensions": ["regions.region_name"],
                                 "start_date": "2025-04-01", "end_date": "2026-03-31"},
    "aov_monthly_bengaluru": {"metrics": ["average_order_value"], "dimensions": ["orders.order_date__month"],
                              "filters": [{"dimension": "customers.city", "values": ["Bengaluru"]}]},
    "category_revenue_top3": {"metrics": ["product_revenue"], "dimensions": ["products.category"],
                              "order_by": ["-product_revenue"], "limit": 3},
    "revenue_and_units_by_fy": {"metrics": ["net_revenue", "units_sold"],
                                "dimensions": ["orders.order_date__fiscal_year"]},
    "cumulative_by_quarter": {"metrics": ["cumulative_net_revenue"], "dimensions": ["orders.order_date__quarter"]},
}


def _norm(sql: str) -> str:
    return sqlglot.parse_one(sql, read="duckdb").sql(dialect="duckdb")


# 1. every YAML file loads through the Pydantic schema (fixtures fail loudly if not)
def test_yaml_files_load(model, metrics):
    assert model.entities and metrics.metrics
    assert load_enrichment().synonyms
    assert load_verified().queries


# 2. every metric formula parses, uses real columns, joins safely and compiles
def test_metrics_valid(model, metrics, con):
    assert validate(model, metrics, con) == []


def test_only_approved_metrics_have_owners(metrics):
    for m in metrics.metrics:
        if m.status == "approved":
            assert m.owner, f"approved metric '{m.name}' needs an owner"


# 3. sample requests compile to the expected SQL (golden files)
@pytest.mark.parametrize("name", sorted(SAMPLE_REQUESTS))
def test_compiles_to_expected_sql(name, model, metrics, con, request):
    sql = compile_query(SemanticQuery.model_validate(SAMPLE_REQUESTS[name]), model, metrics).sql
    con.execute("EXPLAIN " + sql)
    path = GOLDEN / f"{name}.sql"
    if request.config.getoption("--update-golden") or not path.exists():
        path.write_text(sql + "\n", encoding="utf-8")
    assert _norm(sql) == _norm(path.read_text(encoding="utf-8")), f"SQL for {name} changed; review and --update-golden"


def test_verified_queries_compile_and_run(model, metrics, con):
    for v in load_verified().queries:
        con.execute(compile_query(v.query, model, metrics).sql).fetchall()


# 4. key metrics still match trusted numbers
def test_reconciles_with_finance_report(model, metrics, con):
    result = reconcile(model, metrics, con)
    for name, r in result["metrics"].items():
        assert r["matches"], f"{name} differs from finance in {r['mismatched_months']}"


def test_product_revenue_equals_gross_revenue(model, metrics, con):
    """Two routes to the same money (order totals vs. order lines) must agree."""
    q = SemanticQuery(metrics=["gross_revenue", "product_revenue"])
    gross, product = con.execute(compile_query(q, model, metrics).sql).fetchone()
    assert abs(gross - product) < 1.0


def test_enrichment_targets_exist(model, metrics, con):
    enrichment = load_enrichment()
    assert _check_synonyms(model, metrics, enrichment, _value_lists(model, enrichment, con)) == []


# safety rules of the compiler
def test_fan_out_join_is_refused(model, metrics):
    with pytest.raises(CompileError, match="one-to-many"):
        compile_query(SemanticQuery(metrics=["net_revenue"], dimensions=["products.category"]), model, metrics)


def test_unknown_names_are_refused(model, metrics):
    with pytest.raises(CompileError):
        compile_query(SemanticQuery(metrics=["profit"]), model, metrics)
    with pytest.raises(CompileError):
        compile_query(SemanticQuery(metrics=["net_revenue"], dimensions=["orders.colour"]), model, metrics)


def test_filter_values_are_quoted_not_executed(model, metrics, con):
    evil = "x'); DROP TABLE orders; --"
    q = SemanticQuery(metrics=["order_count"], filters=[{"dimension": "customers.city", "values": [evil]}])
    sql = compile_query(q, model, metrics).sql
    assert con.execute(sql).fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM orders").fetchone()[0] > 0
