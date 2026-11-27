"""One graph store (0.7): the build writes plain Postgres tables in one transaction with a new version, search
loads them into an in-memory cache keyed by (workspace, version) with every access check unchanged, nothing in the
product imports Apache AGE, and old workspaces are moved over from _graph_build.json automatically."""
import json

import pytest

from app import core, jobs, vault
from graph import kg
from tests.conftest import needs_pg

pytestmark = needs_pg

ENTS = [{"id": "e1", "name": "Asha Rao", "type": "Person", "aliases": ["Asha"], "description": "Leads Pune", "degree": 1,
         "community_l0": 0, "community_l1": 0, "chunk_ids": ["c1"], "mentions": 2, "origin": ["document"],
         "names_evidence": [{"text": "Asha Rao", "chunk_ids": ["c1"]}],
         "description_evidence": [{"text": "Leads Pune", "chunk_ids": ["c1"]}]},
        {"id": "e2", "name": "Pune Office", "type": "Organisation", "aliases": [], "description": "", "degree": 1,
         "community_l0": 0, "community_l1": 0, "chunk_ids": ["c1", "c2"], "mentions": 3, "origin": ["document"]},
        {"id": "e3", "name": "Payroll Bug", "type": "Event", "aliases": [], "description": "", "degree": 1,
         "community_l0": 1, "community_l1": 1, "chunk_ids": ["c3"], "mentions": 1, "origin": ["document"]}]
RELS = [{"source_id": "e1", "target_id": "e2", "type": "LEADS", "description": "Asha Rao leads the Pune office.",
         "weight": 2.0, "count": 2, "chunk_ids": ["c1"],
         "evidence": [{"text": "Asha Rao leads the Pune office.", "chunk_id": "c1"}]},
        {"source_id": "e2", "target_id": "e3", "type": "AFFECTED_BY", "description": "The payroll bug hit Pune.",
         "weight": 1.0, "count": 1, "chunk_ids": ["c3"]}]
COMMS = [{"id": 0, "level": 0, "parent": None, "entity_ids": ["e1", "e2"], "profile": "Asha Rao, Pune Office"},
         {"id": 1, "level": 0, "parent": None, "entity_ids": ["e3"], "profile": "Payroll Bug"}]


@pytest.fixture
def ws(tmp_path):
    jobs.stop_workers()
    core.reset_for_tests(tmp_path / "var")
    vault.reset_for_tests()
    a, b = core.create_tenant("Acme"), core.create_tenant("Globex")
    from rag import passages
    with core.tenant_scope(a):
        passages.save_all([
            {"chunk_id": "c1", "source": "team.md", "access": ["public"], "headings": [], "text": "Asha Rao leads the Pune office."},
            {"chunk_id": "c2", "source": "team.md", "access": ["public"], "headings": [], "text": "The Pune office opened in 2020."},
            {"chunk_id": "c3", "source": "ops.md", "access": ["ops"], "headings": [], "text": "The payroll bug hit Pune."}])
    return a, b


def test_round_trip_and_version(ws):
    a, b = ws
    with core.tenant_scope(a):
        assert kg.load() is None and kg.version() is None
        v1 = kg.save(ENTS, RELS, COMMS, ["c1", "c2", "c3"])
        g = kg.load()
        assert g["entities"] == sorted(ENTS, key=lambda e: e["id"])         # every field survives, evidence included
        assert g["relations"] == RELS and g["communities"] == COMMS and g["chunk_ids"] == ["c1", "c2", "c3"]
        v2 = kg.save(ENTS[:2], RELS[:1], COMMS[:1], ["c1", "c2"])
        assert v2 != v1 and len(kg.load()["entities"]) == 2                  # replaced, not appended
        assert kg.stats()["relations"] == 1
    with core.tenant_scope(b):
        assert kg.load() is None                                           # row-level security


def test_search_reads_the_tables_with_the_same_access_checks(ws):
    a, _ = ws
    from graph import search as GS
    from graph.access import visible
    with core.tenant_scope(a):
        kg.save(ENTS, RELS, COMMS, ["c1", "c2", "c3"])
        assert GS.ready()
        g1 = GS.graph()
        assert g1["nx"].number_of_nodes() == 3 and g1["nx"].number_of_edges() == 2
        v1 = g1["mtime"]
        assert GS.graph()["mtime"] == v1 and GS.graph()["nx"] is g1["nx"]   # cached by version
        pub = GS.local("Who leads the Pune office?", ["public"], visible)
        assert any(r["type"] == "LEADS" for r in pub["relations"])
        assert not any(r["type"] == "AFFECTED_BY" for r in pub["relations"])   # supported only by an ops passage
        ops = GS.local("What affected the Pune office?", ["ops", "public"], visible)
        assert any(r["type"] == "AFFECTED_BY" for r in ops["relations"])
        assert GS.entity("Payroll Bug", ["public"], visible) is None
        kg.save(ENTS[:2], RELS[:1], COMMS[:1], ["c1", "c2"])
        assert GS.graph()["mtime"] != v1 and len(GS.graph()["ents"]) == 2   # a new build reloads
        from app import engine
        ex = engine.graph_explore("", groups=["public"])
        assert {e["name"] for e in ex["entities"]} == {"Asha Rao", "Pune Office"}


def test_backfill_from_build_file(ws):
    a, _ = ws
    from graph.config import OUT_DIR
    with core.tenant_scope(a):
        out = OUT_DIR.resolve_now()
        out.mkdir(parents=True, exist_ok=True)
        (out / "_graph_build.json").write_text(json.dumps({"entities": ENTS, "relations": RELS, "communities": COMMS,
                                                           "chunk_ids": ["c1", "c2", "c3"]}))
        assert kg.backfill() == {"entities": 3, "relations": 2}
        assert not (out / "_graph_build.json").exists() and (out / "_graph_build.json.pre-0.7").exists()
        v = kg.version()
        assert kg.backfill() == {"entities": 0} and kg.version() == v
        assert len(kg.load()["entities"]) == 3


def test_build_writes_tables_not_the_file(ws, tmp_path):
    a, _ = ws
    from graph import build
    from graph.config import OUT_DIR
    recs = tmp_path / "x.jsonl"
    recs.write_text("\n".join(json.dumps(r) for r in [
        {"chunk_id": "c1", "entities": [{"name": "Asha Rao", "type": "Person", "description": "lead"},
                                        {"name": "Pune Office", "type": "Organisation", "description": ""}],
         "relations": [{"source": "Asha Rao", "target": "Pune Office", "type": "LEADS",
                        "description": "Asha Rao leads the Pune office.", "strength": 8}]}]), encoding="utf-8")
    with core.tenant_scope(a):
        rep = build.build([str(recs)], tables=False)
        assert rep["stored_in"] == "postgres" and rep["entities"] >= 2
        assert not (OUT_DIR / "_graph_build.json").exists()
        assert {e["name"] for e in kg.load()["entities"]} >= {"Asha Rao", "Pune Office"}


def test_product_never_imports_age():
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parent.parent
    for folder in ("app", "graph", "rag", "semantic", "facts", "connectors"):
        for p in (root / folder).glob("*.py"):
            src = p.read_text(encoding="utf-8")
            assert not re.search(r"graph\.store|from graph import store|experiments\.graph_age|ag_catalog|cypher\(", src), p
            if folder == "graph":
                assert not re.search(r"from \. import store|from \.store import", src), p
    assert not (root / "graph" / "store.py").exists() and not (root / "graph" / "query.py").exists()
