"""AI-proposed ontology: proposals keep only types with real examples, approval replaces the graph's types."""
import pytest

from graph import nlp_extract, ontology
from semantic import defstore

SAMPLE = [
    {"source": "handbook.md", "headings": ["Services"], "text": "The billing-service and the api-gateway are owned by the "
     "Backend team. Daniel Okafor leads Backend. Incident INC-2026-031 hit billing-service." * 2},
    {"source": "oncall.md", "headings": ["On call"], "text": "Ravi Shankar leads Platform and DevOps, which runs the "
     "api-gateway. Incident INC-2026-077 was an api-gateway outage." * 2},
]


def fake_ask(cfg, system, user):
    if system == ontology.MERGE_PROMPT:
        return {"groups": [{"keep": "Person", "same_as": ["Employee"]}]}
    return {"entity_types": [
        {"name": "Person", "description": "a named person", "label": "person", "examples": ["Daniel Okafor", "Ravi Shankar"]},
        {"name": "Employees", "description": "staff member", "label": "employee", "examples": ["Daniel Okafor"]},
        {"name": "Team", "description": "a team", "label": "team", "examples": ["Backend", "Platform and DevOps"]},
        {"name": "Service", "description": "software service", "label": "software service", "examples": ["billing-service", "api-gateway"]},
        {"name": "Incident", "description": "production incident", "label": "incident", "examples": ["INC-2026-031", "INC-2026-077"]},
        {"name": "Spaceship", "description": "invented", "label": "spaceship", "examples": ["USS Enterprise"]}],
        "relation_types": [{"name": "leads", "source": "Person", "target": "Team", "description": "X leads Y",
                            "examples": ["Daniel Okafor -> Backend"]},
                           {"name": "OWNS", "source": "Team", "target": "Service", "examples": ["Backend -> Mars"]}]}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("CL_DEFS_DSN", f"sqlite:///{tmp_path / 'defs.db'}")
    monkeypatch.setenv("CL_DEFS_EXPORT", "0")
    monkeypatch.setattr(ontology, "PROPOSALS", tmp_path / "proposals.json")
    defstore.reset_for_tests()
    nlp_extract.schema.cache_clear()
    yield
    defstore.reset_for_tests()
    nlp_extract.schema.cache_clear()


def cfg():
    return {"provider": "local", "model": "qwen3.5:4b", "base_url": "http://ollama:11434/v1", "workers": 1, "api_key": ""}


def test_proposal_keeps_only_types_with_real_examples(store):
    p = ontology.propose(cfg(), ask=fake_ask, sample=SAMPLE, log=lambda *a: None)
    names = [e["name"] for e in p["entity_types"]]
    assert set(names) == {"Person", "Team", "Service", "Incident"}          # Spaceship invented, Employee merged
    assert "Spaceship" in p["dropped_without_examples"]
    person = next(e for e in p["entity_types"] if e["name"] == "Person")
    assert person["merged"] == ["Employee"] and person["status"] == "already there"
    assert next(e for e in p["entity_types"] if e["name"] == "Service")["status"] == "new"
    assert [r["name"] for r in p["relation_types"]] == ["LEADS"]            # OWNS example is not in the text
    assert ontology.proposal()["model"] == "qwen3.5:4b"


def test_approval_replaces_the_types_the_extractors_use(store):
    p = ontology.propose(cfg(), ask=fake_ask, sample=SAMPLE, log=lambda *a: None)
    assert ontology.current()["source"].startswith("hand-written")
    ontology.apply(p["entity_types"], p["relation_types"], "admin@example.com")
    s = nlp_extract.schema()
    assert set(s["entity_types"]) == {"Person", "Team", "Service", "Incident"}
    assert s["entity_types"]["Service"]["gliner_label"] == "software service"
    assert "LEADS" in s["relation_types"] and "RELATED_TO" in s["relation_types"]
    assert s["spacy_label_map"]["PERSON"] == "Person" and s["spacy_label_map"]["ORG"] == "Team"
    assert ontology.current()["source"] == "approved"
    h = defstore.history([defstore.COMPANY], "entity_type", "Service", 5)
    assert h and h[0]["by_user"] == "admin@example.com"


def test_bad_names_and_empty_are_refused(store):
    with pytest.raises(ValueError):
        ontology.apply([], [], "a")
    with pytest.raises(ValueError):
        ontology.apply([{"name": "123"}], [], "a")
