"""Entity resolution by meaning: near-spellings and acronyms merge only when their passages agree."""
import json
from collections import Counter

import numpy as np
import pytest

from graph import build


def group(name, typ, chunks):
    return {"key": build.norm(name), "names": Counter({name: 1}), "types": Counter({typ: 1}), "descriptions": [],
            "chunk_ids": set(chunks), "origin": "document"}


@pytest.fixture
def vectors(tmp_path, monkeypatch):
    rng = np.random.default_rng(1)
    a, b = rng.normal(size=8), rng.normal(size=8)
    near_a = a + rng.normal(scale=0.05, size=8)
    recs = [{"chunk_id": "c1", "dense": a.tolist()}, {"chunk_id": "c2", "dense": near_a.tolist()},
            {"chunk_id": "c3", "dense": b.tolist()}, {"chunk_id": "c4", "dense": (-a).tolist()}]
    (tmp_path / "3_embeddings.json").write_text(json.dumps({"vectors": recs}))
    monkeypatch.setattr(build, "OUT_DIR", tmp_path)
    monkeypatch.setenv("CL_GRAPH_CONTEXT", "1")


def merged(groups):
    ents, key_to_id, report = build.resolve({g["key"]: g for g in groups})
    return {frozenset(e["aliases"] + [e["name"]]) for e in ents}, report


def test_near_spelling_merges_only_with_the_same_context(vectors):
    gs = [group("Kestrel Labs", "Organisation", ["c1"]), group("Kestral Labs", "Organisation", ["c2"]),
          group("Kestrol Labs", "Organisation", ["c3"])]      # same spelling distance, unrelated passages
    sets, report = merged(gs)
    assert frozenset({"Kestrel Labs", "Kestral Labs"}) in sets
    assert frozenset({"Kestrol Labs"}) in sets and report["context_merges"] == 1


def test_acronym_merges_with_its_expansion(vectors):
    gs = [group("SRE", "Organisation", ["c1"]), group("Site Reliability Engineering", "Organisation", ["c2"]),
          group("Sales Revenue Estimate", "Organisation", ["c3"])]
    sets, report = merged(gs)
    assert frozenset({"SRE", "Site Reliability Engineering"}) in sets and report["acronym_merges"] == 1


def test_spelling_merge_vetoed_when_contexts_are_opposite(vectors):
    gs = [group("Kestrel Analytics", "Organisation", ["c1"]), group("Kestrel Analytic", "Organisation", ["c4"])]
    sets, report = merged(gs)
    assert len(sets) == 2 and report["vetoed_by_context"] == 1


def test_without_vectors_spelling_only(tmp_path, monkeypatch):
    monkeypatch.setattr(build, "OUT_DIR", tmp_path)
    sets, report = merged([group("Kestrel Labs", "Organisation", ["c1"]), group("Kestral Labs", "Organisation", ["c2"])])
    assert len(sets) == 2 and report["by_meaning"].startswith("off")
