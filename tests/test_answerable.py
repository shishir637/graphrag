"""The "can this be answered?" check (rule-based, before any evidence is gathered): not answerable -> found false
with the reason and clarifying questions; mixed questions are split into parts with their own routes. Uses the
shipped sample semantic layer and a passages file; no models."""
import json

import pytest

from app import answerable as A
from app import engine
from rag import config as RC

LEAVE = ("Employees get 24 days of annual leave and 12 days of sick leave each year. "
         "Leave requests go to the line manager at least two weeks ahead. Incident INC-2041 was a payroll bug.")


@pytest.fixture
def passages(tmp_path, monkeypatch):
    out = tmp_path / "outputs"
    out.mkdir()
    (out / "2_chunks.json").write_text(json.dumps([{"chunk_id": "leave-1", "source": "leave.md", "headings": ["Leave policy"],
                                                     "access": ["public"], "text": LEAVE}]), encoding="utf-8")
    monkeypatch.setattr(RC, "OUT_DIR", out)
    monkeypatch.setattr(engine, "OUT_DIR", out)
    A._VOCAB["key"] = None
    yield out
    A._VOCAB["key"] = None


def test_answerable_signals(passages):
    assert A.check("Net revenue by region last quarter")["signals"]["metric"]
    r = A.check("How many days of sick leave do employees get?")
    assert r["answerable"] and r["signals"]["coverage"] >= 0.5
    assert A.check("What caused incident INC-2041?")["answerable"]            # ids are known names too
    r = A.check("What is the capital of Mongolia?")
    assert not r["answerable"] and "Mongolia" in r["reason"] and r["clarify"]
    r = A.check("Wat is the levae policy for managers in Ulaanbaatar?")
    assert not r["answerable"] and any("leave" in c for c in r["clarify"])


def test_mixed_question_is_split(passages):
    r = A.check("What was net revenue last quarter and what is the sick leave policy?")
    assert r["answerable"] and len(r["parts"]) == 2


def test_ask_stops_before_retrieval_when_not_answerable(passages, monkeypatch):
    monkeypatch.setattr(engine, "search", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no retrieval")))
    monkeypatch.setattr(engine, "graph_ready", lambda: False)
    engine.clear_ask_cache()
    out = engine.ask("What is the capital of Mongolia?", ["public"], ["documents", "data", "graph"])
    assert out["found"] is False and out["route"] == "not_answerable" and out["clarifying_questions"]
    mixed = engine.ask("What was net revenue last quarter and how many days of sick leave do employees get?",
                       ["public"], ["data"])                        # data only: no document retrieval either
    assert mixed["route"] == "mixed" and len(mixed["parts"]) == 2
    assert mixed["parts"][0]["route"] == "data" and mixed["parts"][0]["found"]
    assert mixed["parts"][1]["route"] == "refused"
    monkeypatch.setenv("CL_ANSWERABLE", "0")                        # off: routed as before
    engine.clear_ask_cache()
    with pytest.raises(AssertionError):
        engine.ask("What is the capital of Mongolia?", ["public"], ["documents"])
