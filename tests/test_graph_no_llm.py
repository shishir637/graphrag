"""Everything must work with no LLM configured (no key, no local model): python -m pytest tests -q"""
import pytest

import rag.graph.extract as X
import rag.graph.gliner_extract as G
import rag.graph.resolve as R
import rag.graph.summarize as SU
import rag.utils as U
from rag import generate as GEN
from rag.graph import llm
from tests.test_graph_gliner import RELS, SPANS, FakeGliner, _chunks
from tests.test_graph_resolve_pipeline import _chunk, _embed


@pytest.fixture
def no_llm(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(llm, "GRAPH_LLM", "anthropic")
    monkeypatch.setattr(U, "OUT_DIR", tmp_path)
    llm.set_backend(None)

    def boom(*a, **k):
        raise AssertionError("an LLM call was attempted")
    monkeypatch.setattr(llm, "_anthropic", boom)
    monkeypatch.setattr(llm, "_openai_compatible", boom)
    assert not llm.available()
    return monkeypatch


def test_hybrid_extraction_keeps_gliner_result(no_llm):
    G.set_model(FakeGliner(SPANS, RELS))
    no_llm.setattr(X, "EXTRACT_MODE", "hybrid")
    U.write_json("2_chunks.json", _chunks())
    stats = X.extract_all()
    G.set_model(None)
    assert stats["llm_calls"] == 0 and stats["chunks_by_extractor"] == {"gliner": 5}
    assert stats["escalation"]["escalated"] == 0 and stats["escalation"]["eligible"] == 3
    assert stats["escalation"]["llm_available"] is False
    recs = {r["chunk_id"]: r for r in U.read_json("g1_extraction.json")["chunks"]}
    assert "no LLM configured" in recs["c_norel"]["escalation"]["reason"]


def test_llm_mode_explains_instead_of_crashing(no_llm):
    no_llm.setattr(X, "EXTRACT_MODE", "llm")
    U.write_json("2_chunks.json", _chunks())
    with pytest.raises(SystemExit, match="needs an LLM"):
        X.extract_all()


def test_resolution_leaves_ambiguous_pairs_undecided(no_llm):
    no_llm.setattr(R, "embed_dense", _embed)
    no_llm.setattr(R, "OVERRIDES_FILE", U.OUT_DIR / "none.json")
    U.write_json("g1_extraction.json", {"chunks": [_chunk("c1", [("Ravi Kumar", "Person"), ("Ravi K", "Person"),
                                                                  ("BAAI", "Organization")]),
                                                   _chunk("c2", [("Beijing Academy of Artificial Intelligence", "Organization")])]})
    stats = R.resolve_all()
    out = U.read_json("g2_resolution.json")
    ravi = next(l for l in out["links"] if {l["a"], l["b"]} == {"Ravi Kumar", "Ravi K"})
    assert (ravi["decision"], ravi["reason"]) == ("undecided", "no LLM configured")
    assert stats["llm_pairs"] == 0 and stats["llm_available"] is False
    assert any(set(e["aliases"]) | {e["name"]} >= {"BAAI", "Beijing Academy of Artificial Intelligence"}
               for e in out["entities"])                     # the rule tiers still work


def test_template_summaries_and_reports(no_llm):
    U.write_json("2_chunks.json", [{"chunk_id": c, "access": ["public"]} for c in ("c1", "c2")])
    U.write_json("g2_resolution.json", {"entities": [
        {"id": "a", "name": "Qdrant", "type": "Technology", "chunk_ids": ["c1", "c2"], "descriptions": [
            {"chunk_id": "c1", "text": "Qdrant stores vectors. It is written in Rust."},
            {"chunk_id": "c2", "text": "Qdrant stores vectors. It supports filters."}]},
        {"id": "b", "name": "BM25", "type": "Technology", "chunk_ids": ["c1"],
         "descriptions": [{"chunk_id": "c1", "text": "BM25 scores keywords."}]}],
        "relations": [{"source": "a", "target": "b", "type": "USES", "weight": 2, "strength": 8,
                       "descriptions": [{"chunk_id": "c1", "text": "Qdrant runs BM25 search."}], "chunk_ids": ["c1"]}]})
    U.write_json("g3_communities.json", {"communities": [{"id": "c0_0", "level": 0, "size": 2, "entity_ids": ["a", "b"]}]})
    stats = SU.summarize_all()
    out = U.read_json("g4_summaries.json")
    assert out["entities"]["a"]["summary"] == "Qdrant stores vectors. It is written in Rust. It supports filters."
    rep = out["reports"][0]
    assert rep["by"] == "template" and rep["title"] in ("Qdrant / BM25", "BM25 / Qdrant")
    assert rep["findings"][0] == {"summary": "Qdrant uses BM25", "explanation": "Qdrant runs BM25 search."}
    assert stats["reports_by_llm"] == 0 and stats["entity_summaries_by_llm"] == 0


def test_answer_step_returns_passages_only(no_llm):
    assert not GEN.can_answer()
    out = GEN.generate("q", [{"chunk_id": "x", "source": "s", "pages": [], "headings": [], "text": "t"}])
    assert out["answer"] is None and out["found"] is None


def test_local_model_counts_as_llm(no_llm):
    no_llm.setattr(llm, "GRAPH_LLM", "openai")
    no_llm.setattr("rag.config.GRAPH_LLM", "openai")
    assert llm.available() and GEN.can_answer()
