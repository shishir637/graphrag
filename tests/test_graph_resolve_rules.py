"""Entity-resolution rules that need no model: python -m pytest tests -q"""
import pytest

from rag.graph.resolve import _average_linkage, fuzzy_rule, name_gate, normalise


@pytest.mark.parametrize("raw, expected", [
    ("INI8 LABS", "ini8 labs"),
    ("Ini8 Labs Pvt. Ltd.", "ini8 labs"),
    ("Dr. Priya Raman", "priya raman"),
    ("Beijing Academy of Artificial Intelligence (BAAI)", "beijing academy of artificial intelligence baai"),
    ("Claude Opus 5.5", "claude opus 5.5"),
    ("BGE-M3", "bge m3"),
])
def test_normalise(raw, expected):
    assert normalise(raw) == expected


@pytest.mark.parametrize("a, b", [
    ("BGE-M3", "BGE reranker v2 m3"),        # different numbers
    ("Claude Opus 4", "Claude Opus 5.5"),
    ("GPT", "GPT-4"),                       # number on one side only, different words
    ("Python 3.11", "Python 3.12"),
])
def test_name_gate_blocks(a, b):
    assert name_gate(normalise(a), normalise(b)) is not None


@pytest.mark.parametrize("a, b", [
    ("INI8 LABS", "Ini8 Labs Pvt Ltd"),
    ("BGE-M3", "BGE-M3 model"),
    ("Qdrant", "Qdrant vector database"),
])
def test_name_gate_allows(a, b):
    assert name_gate(normalise(a), normalise(b)) is None


@pytest.mark.parametrize("a, b", [
    ("Qdrant", "Qdrant vector database"),                                  # generic words only
    ("BGE-M3", "BGE-M3 model"),
    ("BAAI", "Beijing Academy of Artificial Intelligence"),               # acronym
    ("BAAI", "Beijing Academy of Artificial Intelligence (BAAI)"),        # acronym given inside
    ("Retrieval Augmented Generation", "Retrieval-Augmented Generation"),
    ("Priya Raman", "Raman Priya"),                     # word order
])
def test_fuzzy_matches(a, b):
    assert fuzzy_rule(normalise(a), normalise(b)) is not None


@pytest.mark.parametrize("a, b", [
    ("Senior engineer", "Associate engineer"),
    ("Google Cloud Platform", "Google"),          # extra words are not generic
    ("IBM Research", "IBM"),
    ("Databricks", "BigQuery"),
    ("AI", "Apache Iceberg"),                     # two-letter acronyms are too ambiguous
])
def test_fuzzy_does_not_match(a, b):
    assert fuzzy_rule(normalise(a), normalise(b)) is None


def test_average_linkage_groups_only_tight_sets():
    sim = {(0, 1): 0.97, (0, 2): 0.96, (1, 2): 0.93, (2, 3): 0.5}
    # 0 and 1 join first; 2's average to them is (0.96 + 0.93) / 2 = 0.945 < 0.95, so 2 stays out
    assert _average_linkage([0, 1, 2, 3], sim, 0.95) == [[0, 1]]
    sim[(1, 2)] = 0.95                           # now (0.96 + 0.95) / 2 = 0.955 -> 2 joins
    assert _average_linkage([0, 1, 2, 3], sim, 0.95) == [[0, 1, 2]]
    assert _average_linkage([0, 1, 2, 3], {(0, 1): 0.9}, 0.95) == []
