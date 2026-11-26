"""The router's "answerable?" check: decided before any evidence is gathered, rule-based. Not answerable ->
found false with a reason and clarifying questions; mixed questions are split into parts."""
import pytest

from app import core, engine, jobs, vault
from tests.conftest import needs_pg, seed

pytestmark = needs_pg
LEAVE = ("# Leave policy\n\nEmployees get 24 days of annual leave and 12 days of sick leave each year. "
         "Leave requests go to the line manager at least two weeks ahead.")


@pytest.fixture
def ws(tmp_path):
    jobs.stop_workers()
    core.reset_for_tests(tmp_path / "var")
    vault.reset_for_tests()
    t = core.create_tenant("Acme")
    seed(t)
    from tests.test_facts import write_chunks
    with core.tenant_scope(t):
        write_chunks({"leave.md": (LEAVE, ["public"])})
    return t


def test_answerable_signals(ws):
    from app.answerable import check
    with core.tenant_scope(ws):
        assert check("Net revenue by region last quarter")["signals"]["metric"]
        r = check("How many days of sick leave do employees get?")
        assert r["answerable"] and r["signals"]["coverage"] >= 0.5
        r = check("What is the capital of Mongolia?")
        assert not r["answerable"] and "Mongolia" in r["reason"] and r["clarify"]
        r = check("Wat is the levae policy for managers in Ulaanbaatar?")
        assert not r["answerable"] and any("leave" in c for c in r["clarify"])


def test_mixed_question_is_split(ws):
    from app.answerable import check
    with core.tenant_scope(ws):
        r = check("What was net revenue last quarter and what is the sick leave policy?")
        assert r["answerable"] and len(r["parts"]) == 2


def test_ask_stops_before_retrieval_when_not_answerable(ws, monkeypatch):
    monkeypatch.setattr(engine, "search", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no retrieval")))
    with core.tenant_scope(ws):
        engine.clear_ask_cache()
        out = engine.ask("What is the capital of Mongolia?", ["public"], ["documents", "data", "graph"])
        assert out["found"] is False and out["route"] == "not_answerable" and out["clarifying_questions"]
        mixed = engine.ask("What was net revenue last quarter and how many days of sick leave do employees get?",
                           ["public"], ["data"])                       # data only: no document retrieval either
        assert mixed["route"] == "mixed" and len(mixed["parts"]) == 2
        assert mixed["parts"][0]["route"] == "data" and mixed["parts"][0]["found"]
