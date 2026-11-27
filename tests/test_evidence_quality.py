"""Evidence quality: relevance labels, unrelated passages dropped, near-duplicates removed, neighbouring
section text, and the exact-match ask cache. No models needed."""
import json

from app import engine
from rag import retrieve as R


def hit(cid, text, score):
    return {"chunk_id": cid, "source": "a.md", "pages": [], "headings": [], "text": text, "rerank_score": score}


def test_relevance_and_labels():
    assert R.relevance(None) is None
    assert R.relevance(0) == 0.5
    assert R.label(R.relevance(3)) == "strong"
    assert R.label(R.relevance(-1)) == "partial"
    assert R.label(R.relevance(-3)) == "weak"
    assert R.relevance(-1000) == 0.0 and R.relevance(1000) == 1.0          # no overflow


def test_screen_drops_unrelated_and_duplicates():
    text = "Kerala is a state on the Malabar coast of India with Thiruvananthapuram as its capital city."
    hits = [hit("a", text, 4.0), hit("b", text + " ", 3.5), hit("c", "Goa has beaches and a capital at Panaji.", -1.0),
            hit("d", "Profit margin was twelve percent across all product lines last year.", -10.0)]
    out = R.screen(hits)
    assert [h["chunk_id"] for h in out] == ["a", "c"]                     # b = duplicate of a, d = unrelated
    assert out[0]["evidence"] == "strong" and out[1]["evidence"] == "partial"
    assert R.evidence_strength(out) == "strong"
    assert R.evidence_strength([]) == "none"


def test_screen_keeps_unscored_when_reranker_off():
    out = R.screen([hit("a", "some text here about things", None)])
    assert out[0]["evidence"] == "unscored" and R.evidence_strength(out) == "unscored"


def test_neighbours_same_section_and_access(tmp_path, monkeypatch):
    import rag.config as C
    chunks = [
        {"chunk_id": "c0", "source": "d.md", "headings": ["Intro"], "access": ["public"], "text": "intro"},
        {"chunk_id": "c1", "source": "d.md", "headings": ["Leave"], "access": ["public"], "text": "Leave rules: 1. annual"},
        {"chunk_id": "c2", "source": "d.md", "headings": ["Leave"], "access": ["public"], "text": "2. sick leave 3. casual"},
        {"chunk_id": "c3", "source": "d.md", "headings": ["Leave"], "access": ["hr"], "text": "4. secret"},
    ]
    (tmp_path / "2_chunks.json").write_text(json.dumps(chunks), encoding="utf-8")
    monkeypatch.setattr(C, "OUT_DIR", tmp_path)
    R._CHUNK_INDEX["mtime"] = None
    n = R.neighbours("c2", ["public"])
    assert n["before"]["id"] == "c1" and "after" not in n                # c3 not visible to public
    assert "before" not in R.neighbours("c1", ["public"])                 # c0 is another section
    assert R.neighbours("c2", ["hr", "public"])["after"]["id"] == "c3"
    assert R.neighbours("missing", ["public"]) == {}


def test_ask_cache_exact_match_and_invalidation(monkeypatch):
    calls = []

    def fake(question, groups, caps, mode):
        calls.append(question)
        return {"question": question, "route": "documents", "passages": [], "timings_s": {"total": 1.0}}
    monkeypatch.setattr(engine, "_ask", fake)
    version = [1]
    monkeypatch.setattr(engine, "_sources_version", lambda: tuple(version))
    engine.clear_ask_cache()
    engine.ask("Capital of Kerala?", ["public"], ["documents"])
    r = engine.ask("  capital of   kerala ", ["public"], ["documents"])
    assert len(calls) == 1 and r["cached"] is True
    engine.ask("Capital of Kerala?", ["hr"], ["documents"])              # other groups: not shared
    engine.ask("Capital of Goa?", ["public"], ["documents"])             # other question
    assert len(calls) == 3
    version[0] = 2                                                         # sources rebuilt
    engine.ask("Capital of Kerala?", ["public"], ["documents"])
    assert len(calls) == 4
    r2 = engine.ask("Capital of Kerala?", ["public"], ["documents"])
    assert r2["cached"] is True
    r2["passages"].append("x")                                             # callers cannot corrupt the cache
    assert engine.ask("Capital of Kerala?", ["public"], ["documents"])["passages"] == []
