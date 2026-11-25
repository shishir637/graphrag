"""The semantic layer in Postgres: same definitions as the reviewed YAML, versions and history, the approval policy,
workspace isolation, YAML export/import, and the planner and compiler answering from it. No models needed."""
from datetime import date

import pytest

from app import core
from semantic import repo
from semantic import yamlio as Y
from semantic.config import LAYER_DIR
from tests.conftest import needs_pg, seed

pytestmark = needs_pg


@pytest.fixture
def ws(tmp_path):
    core.reset_for_tests(tmp_path / "var")
    a, b = core.create_tenant("Acme"), core.create_tenant("Globex")
    seed(a)
    return a, b


def test_same_definitions_as_the_yaml(ws):
    a, _ = ws
    with core.tenant_scope(a):
        m_db, m_file = Y.load_metrics(), Y.load_metrics(LAYER_DIR / "metrics.yaml")
        assert [(m.name, m.expression, m.status) for m in m_db.metrics] == \
               [(m.name, m.expression, m.status) for m in m_file.metrics]
        assert [e.name for e in Y.load_model().entities] == [e.name for e in Y.load_model(LAYER_DIR / "model.yaml").entities]
        assert len(Y.load_enrichment().synonyms) == len(Y.load_enrichment(LAYER_DIR / "enrichment.yaml").synonyms)
        assert len(Y.load_verified().queries) == len(Y.load_verified(LAYER_DIR / "verified_queries.yaml").queries)
        assert "revenue" in Y.load_instructions().lower()


def test_workspaces_do_not_share_definitions(ws):
    a, b = ws
    with core.tenant_scope(b):
        assert repo.is_empty() and Y.load_metrics().metrics == []
    with core.tenant_scope(a):
        assert not repo.is_empty()


def test_versions_and_history(ws):
    a, _ = ws
    with core.tenant_scope(a):
        v0 = repo.layer_version()
        r = repo.get("metric", "net_revenue")
        assert r["version"] == 1 and r["status"] == "approved"
        repo.put("metric", "net_revenue", r["definition"] | {"description": "changed"}, status="draft", source="manual",
                 by="ann@acme.io")
        r2 = repo.get("metric", "net_revenue")
        assert r2["version"] == 2 and repo.layer_version() > v0
        repo.set_status("metric", "net_revenue", "approved", "ben@acme.io")
        h = repo.history("metric", "net_revenue")
        assert [x["op"] for x in h] == ["insert", "update", "update"]
        assert h[1]["old_definition"]["description"] != "changed" and h[1]["changed_by"] == "ann@acme.io"
        assert h[2]["new_status"] == "approved" and h[2]["changed_by"] == "ben@acme.io"


def test_approval_policy(ws):
    a, _ = ws
    from semantic.schema import Metric, MetricSet
    with core.tenant_scope(a):
        new = Metric(name="big_orders", label="Big orders", description="Orders over 10k", type="simple",
                     entity="orders", expression="count(*)", filters=["orders.amount > 10000"], status="approved")
        repo.import_layer(metrics=MetricSet(metrics=[new]), source="auto_draft")
        assert repo.get("metric", "big_orders")["status"] == "draft"          # drafts need an owner's approval
        changed = Y.load_metrics().get("gross_revenue").model_copy(update={"expression": "sum(orders.amount) * 2"})
        repo.import_layer(metrics=MetricSet(metrics=[changed]), source="auto_draft")
        assert repo.get("metric", "gross_revenue")["status"] == "draft"       # a changed formula is reviewed again
        assert "gross_revenue" not in {m.name for m in Y.load_metrics().metrics if m.status == "approved"}
        same = Y.load_metrics().get("net_revenue")
        repo.import_layer(metrics=MetricSet(metrics=[same]), source="auto_draft")
        assert repo.get("metric", "net_revenue")["status"] == "approved"      # unchanged: stays approved
        repo.set_status("metric", "big_orders", "retired", "owner")
        repo.import_layer(metrics=MetricSet(metrics=[new]), source="auto_draft")
        assert repo.get("metric", "big_orders")["status"] == "retired"        # drafting does not bring it back
        assert "big_orders" not in {m.name for m in Y.load_metrics().metrics}


def test_export_import_roundtrip(ws):
    a, b = ws
    with core.tenant_scope(a):
        files = repo.export_texts()
        names = [m.name for m in Y.load_metrics().metrics]
    with core.tenant_scope(b):
        repo.import_texts(files, by="test")
        assert [m.name for m in Y.load_metrics().metrics] == names


def test_planner_and_compiler_answer_from_the_workspace(ws):
    a, b = ws
    from semantic.compiler import run
    from semantic.enrich import build_enrichment
    from semantic.rule_planner import planner
    with core.tenant_scope(a):
        build_enrichment(embed=False)                     # known values (cities, regions) into Postgres
        assert "customers.city" in repo.value_lists()
        p = planner().plan("net revenue by region last year", date(2026, 11, 25))
        assert p.answerable and p.query.metrics == ["net_revenue"]
        compiled, df = run(p.query)
        assert len(df) > 0 and "region" in compiled.sql.lower()
        v = repo.layer_version()
        repo.set_status("metric", "net_revenue", "draft", "owner")
        assert repo.layer_version() > v
        assert "net_revenue" not in [m.name for m in planner().approved]    # the planner follows the change
    with core.tenant_scope(b):
        assert not planner().plan("net revenue by region", date(2026, 11, 25)).answerable
