"""resolve_all() end to end with a stub LLM and stub vectors: python -m pytest tests -q"""
import json
import math

import pytest

import rag.graph.resolve as R
import rag.utils as U
from rag.graph import llm


def _vec(name):
    """Names sharing a first word get cosine 0.9 (ambiguous band); anything else is orthogonal."""
    v = {}
    toks = R.normalise(name).split()
    v["f:" + toks[0]] = 3.0
    v["n:" + " ".join(toks)] = 1.0
    n = math.sqrt(sum(x * x for x in v.values()))
    return {k: x / n for k, x in v.items()}


def _embed(names):
    keys = sorted({k for n in names for k in _vec(n)})
    return [[_vec(n).get(k, 0.0) for k in keys] for n in names]


def _chunk(cid, ents, rels=()):
    return {"chunk_id": cid, "source": "t.md", "access": ["public"],
            "entities": [{"name": n, "type": t, "description": f"{n} in {cid}"} for n, t in ents],
            "relations": [{"source": s, "target": o, "type": "RELATED_TO", "description": "x", "strength": 5}
                          for s, o in rels], "problems": []}


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setattr(U, "OUT_DIR", tmp_path)
    monkeypatch.setattr(R, "embed_dense", _embed)
    monkeypatch.setattr(R, "OVERRIDES_FILE", tmp_path / "overrides.json")
    asked = []

    def judge(system, user, schema):
        asked.append(user)
        a = user.split("Entity A (")[1].split("): ")[1].split("\n")[0]
        b = user.split("Entity B (")[1].split("): ")[1].split("\n")[0]
        if "Unsure" in a + b:
            return schema(verdict="uncertain", reason="cannot tell")
        return schema(verdict="match", reason="stub says match")

    llm.set_backend(judge)

    def go(chunks, overrides=None, **cfg):
        for k, v in cfg.items():
            monkeypatch.setattr(R, k, v)
        if overrides:
            (tmp_path / "overrides.json").write_text(json.dumps(overrides))
        U.write_json("g1_extraction.json", {"chunks": chunks})
        R.resolve_all()
        return U.read_json("g2_resolution.json"), asked

    yield go
    llm.set_backend(None)


def _groups(out):
    return sorted(sorted([e["name"], *e["aliases"]]) for e in out["entities"])


def _link(out, a, b):
    return next(l for l in out["links"] if {l["a"], l["b"]} == {a, b})


def test_tiers(run):
    out, asked = run([
        _chunk("c1", [("BAAI", "Organization"), ("BGE-M3", "Technology"), ("Priya Raman", "Person")],
               [("Priya Raman", "BGE-M3")]),
        _chunk("c2", [("Beijing Academy of Artificial Intelligence", "Organization"), ("BGE-M3 model", "Technology"),
                      ("BGE reranker v2 m3", "Technology"), ("Priya R", "Person"), ("INI8 LABS", "Organization")]),
        _chunk("c3", [("Ini8 Labs Pvt. Ltd.", "Organization"), ("Priya R", "Person")],
               [("Priya R", "Ini8 Labs Pvt. Ltd.")]),
    ])
    assert _link(out, "BAAI", "Beijing Academy of Artificial Intelligence")["tier"] == "fuzzy"
    assert _link(out, "BGE-M3", "BGE-M3 model")["tier"] == "fuzzy"
    assert _link(out, "BGE-M3", "BGE reranker v2 m3")["decision"] == "no_match"
    assert _link(out, "BGE-M3", "BGE reranker v2 m3")["tier"] == "name_gate"
    k = _link(out, "Priya Raman", "Priya R")
    assert (k["tier"], k["decision"]) == ("llm", "match")
    assert len(asked) == 1                                   # only the one ambiguous pair reached the LLM
    groups = _groups(out)
    assert ["INI8 LABS", "Ini8 Labs Pvt. Ltd."] in groups    # exact after normalisation: one surface entity
    assert ["BGE-M3", "BGE-M3 model"] in groups
    assert ["BGE reranker v2 m3"] in groups
    # relations follow the resolved entities: both Priya Raman mentions point at one node
    kan = next(e["id"] for e in out["entities"] if e["type"] == "Person")
    assert {r["source"] for r in out["relations"]} == {kan}


def test_uncertain_is_undecided_not_rejected(run):
    out, _ = run([_chunk("c1", [("Unsure Corp", "Organization")]), _chunk("c2", [("Unsure Holdings", "Organization")])])
    l = _link(out, "Unsure Corp", "Unsure Holdings")
    assert l["decision"] == "undecided"
    assert len(out["entities"]) == 2


def test_budget_leaves_rest_undecided(run):
    out, asked = run([_chunk("c1", [("Ravi Kumar", "Person"), ("Ravi K", "Person"), ("Ravi Shankar", "Person")])],
                     RESOLVE_MAX_LLM_PAIRS=1)
    assert len(asked) == 1
    assert sum(l["decision"] == "undecided" and "budget" in l["reason"] for l in out["links"]) == 2


def test_no_match_blocks_transitive_merge(run):
    # LLM says Ravi Kumar ~ Ravi K and Ravi K ~ Ravi Shankar; the override says Kumar != Shankar.
    out, _ = run([_chunk("c1", [("Ravi Kumar", "Person"), ("Ravi K", "Person"), ("Ravi Shankar", "Person")])],
                 overrides={"no_match": [["Ravi Kumar", "Ravi Shankar"]]})
    assert _link(out, "Ravi Kumar", "Ravi Shankar")["tier"] == "override"
    blocked = [l for l in out["links"] if l.get("applied") is False]
    assert len(blocked) == 1 and "Ravi Kumar" in blocked[0]["blocked_by"]
    for g in _groups(out):
        assert not {"Ravi Kumar", "Ravi Shankar"} <= set(g)


def test_override_match_wins(run):
    out, asked = run([_chunk("c1", [("GPT", "Product")]), _chunk("c2", [("GPT-4", "Product")])],
                     overrides={"match": [["GPT", "GPT-4"]]})
    assert _link(out, "GPT", "GPT-4")["tier"] == "override"
    assert len(out["entities"]) == 1 and not asked


def test_different_types_never_compared(run):
    out, asked = run([_chunk("c1", [("Qdrant", "Technology"), ("Qdrant", "Organization")])])
    assert len(out["entities"]) == 2 and not out["links"] and not asked
