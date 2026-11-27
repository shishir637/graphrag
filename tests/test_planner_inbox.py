"""One question planner and one inbox (0.7): app/planner.py makes the answerable / route / split / secondary
decisions in one rule-based pass (the metric planner runs once per question), ask uses it, the old router and
answerable functions are thin wrappers, and every review decision (inbox page, Metrics and Ontology pages, MCP
review_inbox / review_ontology / approve_metric) goes through facts/inbox.py, whose count the guide shows."""
from datetime import date

import pytest

from app import core, jobs, vault
from tests.conftest import needs_pg, seed

pytestmark = needs_pg
D = date(2026, 11, 27)


@pytest.fixture
def ws(tmp_path):
    jobs.stop_workers()
    core.reset_for_tests(tmp_path / "var")
    vault.reset_for_tests()
    t = core.create_tenant("Acme")
    seed(t)
    return t


def test_one_pass_matches_the_old_functions_and_plans_metrics_once(ws, monkeypatch):
    from app import answerable, planner, router
    from semantic import rule_planner
    with core.tenant_scope(ws):
        for q in ("Net revenue by region last quarter", "Why did net revenue drop last quarter?",
                  "What is the capital of Mongolia?", "Explain how average order value is calculated"):
            p = planner.plan_question(q, None, D)
            old = answerable.check(q)
            assert {k: p[k] for k in ("answerable", "parts", "signals")} == {k: old[k] for k in ("answerable", "parts", "signals")}
            assert (p["route"], p["secondary"], p["route_reason"]) == router.plan(q, D)
        real = rule_planner.planner()
        calls = []
        orig = real.plan
        monkeypatch.setattr(real, "plan", lambda *a, **k: calls.append(a) or orig(*a, **k))
        planner.plan_question("Why did net revenue drop last quarter?", None, D)
        assert len(calls) == 1                                       # shared by answerable?, route and secondary
    assert router.route is planner.route and answerable.check is planner.check


def test_ask_uses_the_planner(ws, monkeypatch):
    from app import engine, planner
    seen = []
    orig = planner.plan_question
    monkeypatch.setattr(planner, "plan_question", lambda *a, **k: seen.append(a[0]) or orig(*a, **k))
    with core.tenant_scope(ws):
        engine.clear_ask_cache()
        out = engine.ask("Net revenue by region last quarter", ["public"], ["data"])
        assert seen == ["Net revenue by region last quarter"] and out["route"] == "data"


def test_every_review_decision_goes_through_the_inbox(ws, monkeypatch):
    from app import engine, guide, mcp_server
    from facts import inbox
    from graph import ontology
    from semantic import repo
    decided = []
    orig = inbox.decide
    monkeypatch.setattr(inbox, "decide", lambda *a, **k: decided.append(a[:3]) or orig(*a, **k))
    admin = {"name": "setup-bot", "scope": "admin", "capabilities": ["documents", "data"], "groups": ["public"]}
    with core.tenant_scope(ws):
        m = repo.rows("metric")[0]
        repo.set_status("metric", m["name"], "draft", "test")
        ontology.put("entity_type", "Vendor", {"description": "A supplier"}, status="draft", source="documents")
        n = len(inbox.items())
        assert n >= 2 and inbox.count() == n == guide.facts({"email": "x", "name": "X", "id": "u1", "role": "owner"})["drafts"]
        mcp_server.tool_approve_metric(admin, m["name"])
        monkeypatch.setattr(engine, "graph_ready", lambda: True)
        mcp_server.tool_review_ontology(admin, approve=["Vendor"])
        assert ("definition", f"metric:{m['name']}", "approve") in decided
        assert ("ontology", "entity_type:Vendor", "approve") in decided
        assert repo.get("metric", m["name"])["status"] == "approved" and ontology.get("entity_type", "Vendor")["status"] == "approved"
        kinds = [r["kind"] for r in core.db().execute("SELECT kind FROM jobs WHERE tenant_id = ?", (ws,))]
        assert kinds.count("graph") == 1                              # one re-extraction for the ontology change
        assert inbox.count() == n - 2 == len(inbox.items())
        with pytest.raises(KeyError):
            inbox.decide("definition", "metric:no_such_metric", "approve", "x")
