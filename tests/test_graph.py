"""Graph RAG checks: name normalisation, label safety, Cypher parameter safety (needs the AGE container)."""
import json

import pytest

from graph.build import aggregate, norm
from graph.eval_extraction import same


def test_norm_merges_spelling_variants():
    assert norm("The Mlechchha Dynasty") == norm("mlechchha dynasty")
    assert norm("Paço de Arcos") == "paco de arcos"
    assert norm("J&K") == "j and k"


def test_fuzzy_match_rules():
    assert same("Gumti", "Gumti Wildlife Sanctuary")               # short form of the full name
    assert same("Rohtang pass", "Rohtang Pass")
    assert not same("North Goa", "South Goa")


def test_aggregate_weights_repeated_relations():
    rels = [{"s": "a", "t": "b", "type": "RULED", "description": "x", "strength": 8, "chunk_id": "c1"},
            {"s": "a", "t": "b", "type": "RULED", "description": "y", "strength": 6, "chunk_id": "c2"},
            {"s": "a", "t": "a", "type": "RULED", "description": "self", "strength": 9, "chunk_id": "c3"}]
    out = aggregate(rels, {"a": "e1", "b": "e2"})
    assert len(out) == 1 and out[0]["count"] == 2 and out[0]["weight"] == 1.4
    assert out[0]["chunk_ids"] == ["c1", "c2"]


def test_labels_are_whitelisted():
    from experiments.graph_age.store import _label
    assert _label("LOCATED_IN") == "LOCATED_IN"
    for bad in ("x) DETACH DELETE (n", "City{", "", "drop"):
        with pytest.raises(ValueError):
            _label(bad)


def _age():
    try:
        from experiments.graph_age import store
        store.conn()
        return store
    except Exception:
        return None


@pytest.mark.skipif(_age() is None, reason="Apache AGE container not running")
def test_cypher_parameters_cannot_inject():
    from experiments.graph_age import store
    c = store.conn()
    if c.execute("SELECT count(*) FROM ag_graph WHERE name = 'india_kg'").fetchone()[0] == 0:
        pytest.skip("graph 'india_kg' not built")
    evil = "x' }) MATCH (n) DETACH DELETE n //$$"
    before = store.cypher("MATCH (n) RETURN count(n)")[0][0]
    rows = store.cypher("MATCH (n) WHERE n.name = $name RETURN n.id", {"name": evil})
    assert rows == []
    assert store.cypher("MATCH (n) RETURN count(n)")[0][0] == before
    assert json.loads(json.dumps(store.stats()))["nodes"] == before
