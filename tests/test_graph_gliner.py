"""GLiNER2 decoding and hybrid escalation, with a stand-in model (no download): python -m pytest tests -q"""
import json

import pytest

import rag.graph.extract as X
import rag.graph.gliner_extract as G
import rag.utils as U
from rag.graph import llm


class FakeGliner:
    """Same interface as gliner2's AutoExtractor model. `spans` maps (text, label) -> confidence;
    `rels` maps (head, label, tail) -> confidence."""

    def __init__(self, spans, rels=None):
        self.spans, self.rels, self.calls = spans, rels or {}, 0

    def create_schema(self):
        model = self

        class B:
            def entities(self, d):
                self.ents = d
                return self

            def relations(self, d):
                self.rels = d
                return self
        model.builder = B()
        return model.builder

    def extract(self, text, schema, threshold=0.5, include_confidence=True, include_spans=True):
        self.calls += 1
        ents = {}
        for (name, label), conf in self.spans.items():
            i = text.find(name)
            if i >= 0 and conf >= threshold:
                ents.setdefault(label, []).append({"text": name, "start": i, "end": i + len(name), "confidence": conf})
        rels = {}
        for (h, label, t), conf in self.rels.items():
            if h in text and t in text:
                rels.setdefault(label, []).append({
                    "head": {"text": h, "start": text.find(h), "end": text.find(h) + len(h), "confidence": conf},
                    "tail": {"text": t, "start": text.find(t), "end": text.find(t) + len(t), "confidence": conf}})
        return {"entities": ents, "relation_extraction": rels}


TEXT = "Priya Raman works at INI8 LABS. She uses Qdrant and BGE-M3 every day."


def test_decode_picks_best_label_and_drops_weak_items():
    raw = {"entities": {
        "Organization": [{"text": "INI8 LABS", "start": 21, "end": 30, "confidence": 0.9}],
        "Product": [{"text": "INI8 LABS", "start": 21, "end": 30, "confidence": 0.6},     # same span, lower score
                    {"text": " Qdrant", "start": 40, "end": 47, "confidence": 0.4}],      # below 0.5
        "Person": [{"text": "Priya Raman", "start": 0, "end": 11, "confidence": 0.95}],
        "NotAType": [{"text": "She", "start": 32, "end": 35, "confidence": 0.99}]},
        "relation_extraction": {
        "WORKS_AT": [{"head": {"text": "Priya Raman", "start": 0, "end": 11, "confidence": 0.85},
                      "tail": {"text": "INI8 LABS", "start": 21, "end": 30, "confidence": 0.85}}],
        "USES": [{"head": {"text": "Priya Raman", "confidence": 0.9}, "tail": {"text": "Qdrant", "confidence": 0.9}},
                 {"head": {"text": "Priya Raman", "start": 0, "end": 11, "confidence": 0.5},
                  "tail": {"text": "INI8 LABS", "start": 21, "end": 30, "confidence": 0.5}}]}}
    out = G.decode(TEXT, raw)
    assert [(e["name"], e["type"]) for e in out["entities"]] == [("Priya Raman", "Person"), ("INI8 LABS", "Organization")]
    assert out["entities"][1]["description"] == "Priya Raman works at INI8 LABS."     # its sentence
    assert [(r["source"], r["type"], r["target"]) for r in out["relations"]] == [("Priya Raman", "WORKS_AT", "INI8 LABS")]
    assert out["relations"][0]["strength"] == 8
    assert any("Qdrant" in d and "below" in d for d in out["dropped"])
    assert any("not a kept entity" in d for d in out["dropped"])            # Qdrant was not kept
    assert any("USES" in d and "below 0.7" in d for d in out["dropped"])


def _chunks():
    return [
        {"chunk_id": "c_strong", "source": "a.md", "access": ["public"], "headings": [], "embed_text": "1",
         "text": "Priya Raman works at INI8 LABS."},
        {"chunk_id": "c_norel", "source": "a.md", "access": ["public"], "headings": [], "embed_text": "2",
         "text": "Qdrant and BGE-M3 appear in this sentence together."},
        {"chunk_id": "c_weak", "source": "a.md", "access": ["public"], "headings": [], "embed_text": "3",
         "text": "Databricks is mentioned once here."},
        {"chunk_id": "c_empty", "source": "a.md", "access": ["public"], "headings": [], "embed_text": "4",
         "text": "this sentence has plenty of words but nothing the model recognises at all"},
        {"chunk_id": "c_short", "source": "a.md", "access": ["public"], "headings": [], "embed_text": "5",
         "text": "ok then"},
    ]


SPANS = {("Priya Raman", "Person"): 0.95, ("INI8 LABS", "Organization"): 0.9, ("Qdrant", "Technology"): 0.9,
         ("BGE-M3", "Technology"): 0.88, ("Databricks", "Technology"): 0.6}
RELS = {("Priya Raman", "WORKS_AT", "INI8 LABS"): 0.9}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(U, "OUT_DIR", tmp_path)
    fake = FakeGliner(SPANS, RELS)
    G.set_model(fake)
    asked = []

    def stub(system, user, schema):
        asked.append(user)
        assert "<draft>" in user                                     # the LLM reviews GLiNER's draft
        draft = json.loads(user.split("<draft>\n")[1].split("\n</draft>")[0])
        ents = [X.Entity(name=e["name"], type=e["type"], description="reviewed") for e in draft["entities"]]
        if "Databricks" in user:
            ents = [X.Entity(name="Databricks", type="Technology", description="reviewed"),
                    X.Entity(name="Spark", type="Technology", description="added by the LLM")]
        return X.Extraction(entities=ents, relations=[])

    llm.set_backend(stub)
    U.write_json("2_chunks.json", _chunks())
    yield monkeypatch, fake, asked
    llm.set_backend(None)
    G.set_model(None)


def test_escalation_order_and_budget(env):
    monkeypatch, fake, asked = env
    monkeypatch.setattr(X, "EXTRACT_MODE", "hybrid")
    monkeypatch.setattr(X, "ESCALATE_BUDGET", 0.4)          # 0.4 * 5 chunks -> 2 reviews
    stats = X.extract_all()
    recs = {r["chunk_id"]: r for r in U.read_json("g1_extraction.json")["chunks"]}
    # ranking: entities-but-no-relation first, then weakest score, then empty-but-long; short chunks never
    assert recs["c_norel"]["escalation"]["rank"] == 1
    assert recs["c_weak"]["escalation"]["rank"] == 2
    assert recs["c_empty"]["escalation"]["rank"] == 3
    assert recs["c_short"]["escalation"] is None and recs["c_strong"]["escalation"] is None
    assert [recs[c]["extractor"] for c in ("c_norel", "c_weak", "c_empty", "c_strong")] == \
        ["gliner+llm", "gliner+llm", "gliner", "gliner"]
    assert stats["llm_calls"] == 2 and len(asked) == 2
    assert stats["escalation"] == {"eligible": 3, "escalated": 2, "budget": 0.4, "left_to_gliner": 1,
                                   "lowest_score_left": None}
    weak = {e["name"]: e["from"] for e in recs["c_weak"]["entities"]}
    assert weak == {"Databricks": "gliner+llm", "Spark": "llm"}
    strong = recs["c_strong"]
    assert strong["relations"][0]["type"] == "WORKS_AT" and strong["entities"][0]["from"] == "gliner"


def test_cache_means_no_new_calls(env):
    monkeypatch, fake, asked = env
    monkeypatch.setattr(X, "EXTRACT_MODE", "hybrid")
    X.extract_all()
    calls, model_calls = len(asked), fake.calls
    stats = X.extract_all()
    assert stats["reused_from_cache"] == 5 and stats["llm_calls"] == 0
    assert len(asked) == calls and fake.calls == model_calls


def test_gliner_mode_makes_no_llm_calls(env):
    monkeypatch, fake, asked = env
    monkeypatch.setattr(X, "EXTRACT_MODE", "gliner")
    stats = X.extract_all()
    assert stats["llm_calls"] == 0 and not asked and stats["chunks_by_extractor"] == {"gliner": 5}
    assert stats["escalation"] is None


def test_zero_budget_escalates_nothing(env):
    monkeypatch, fake, asked = env
    monkeypatch.setattr(X, "EXTRACT_MODE", "hybrid")
    monkeypatch.setattr(X, "ESCALATE_BUDGET", 0)
    stats = X.extract_all()
    assert stats["escalation"]["escalated"] == 0 and stats["escalation"]["eligible"] == 3 and not asked


def test_schema_gets_types_and_descriptions(env):
    _, fake, _ = env
    G.schema()
    assert set(fake.builder.ents) == set(X.ENTITY_TYPES)
    assert fake.builder.ents["Person"] == "a named individual person"
    assert fake.builder.rels["WORKS_AT"]["description"].startswith("a person works at")


def test_missing_package_gives_clear_message(monkeypatch):
    import builtins
    G.set_model(None)
    real = builtins.__import__

    def no_gliner(name, *a, **k):
        if name == "gliner2":
            raise ImportError
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", no_gliner)
    with pytest.raises(SystemExit, match="pip install"):
        G.model()
