"""The metric catalog in memory (0.7): built from the semantic layer in Postgres, cached by the layer's state
version, rebuilt when a definition or a value list changes, and never written to disk (no enrichment.duckdb)."""
from pathlib import Path

import pytest

from app import core, jobs, vault
from semantic import enrich, repo
from tests.conftest import needs_pg, seed

pytestmark = needs_pg
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def ws(tmp_path):
    jobs.stop_workers()
    core.reset_for_tests(tmp_path / "var")
    vault.reset_for_tests()
    t = core.create_tenant("Acme")
    seed(t)
    return t


def test_catalog_is_cached_and_follows_the_layer(ws):
    with core.tenant_scope(ws):
        c1 = enrich.catalog()
        assert c1["synonyms"] and c1["values"] and enrich.load_store() is c1          # cached, same object
        terms = {t for t, _ in c1["synonyms"]}
        assert "takings" not in terms
        repo.put("synonym", "takings", {"term": "takings", "target": "net_revenue"}, status="approved",
                 source="manual", by="ann")
        c2 = enrich.catalog()
        assert c2 is not c1 and ("takings", "net_revenue") in c2["synonyms"]          # layer changed: rebuilt
        assert enrich.catalog() is c2
        repo.save_value_lists({"orders.channel": ["app", "web", "kiosk"]})
        assert enrich.catalog()["values"]["orders.channel"] == ["app", "kiosk", "web"]   # value lists too


def test_planner_and_metrics_use_the_catalog_without_files(ws):
    from app import engine
    from semantic.rule_planner import planner
    from rag.wspath import files_root
    with core.tenant_scope(ws):
        from datetime import date
        assert planner().plan("net revenue by channel", date(2026, 11, 27)).answerable
        assert engine.metric_query(question="net revenue by channel", groups=None)["found"]
        assert engine.describe_data(None)["metrics"]
    assert not list(files_root().rglob("enrichment.duckdb")) and not list(files_root().rglob("sl_store"))


def test_no_enrichment_store_left_in_the_code():
    import semantic.config as C
    assert not hasattr(C, "STORE_DB") and not hasattr(C, "STORE_DIR")
    for folder in ("app", "semantic", "facts", "connectors", "graph", "rag"):
        for p in (ROOT / folder).glob("*.py"):
            assert "enrichment.duckdb" not in p.read_text(encoding="utf-8"), p
    assert "metric_store" not in (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
