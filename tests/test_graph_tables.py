"""The knowledge graph in Postgres tables (0.9, no Apache AGE): save and load keep every field (per-passage evidence
included), the version moves with each build and the in-memory graph follows it, workspaces never see each other's
graph, an older build file is moved in once, and a NetworkX view is cached per version. Without Postgres the graph
stays in the build file. Postgres parts need CL_TEST_DATABASE_URL."""
import json
import os

import pytest

from app import core
from graph import kg
from graph import search as GS

pg = pytest.mark.skipif(not os.environ.get("CL_TEST_DATABASE_URL"), reason="no test Postgres (CL_TEST_DATABASE_URL)")

ENTS = [{"id": "e_a", "name": "Asha Rao", "type": "Person", "aliases": ["A. Rao"], "description": "Team lead",
         "degree": 1, "community_l0": 0, "chunk_ids": ["c1", "c2"], "origin": ["document"],
         "names_evidence": [{"text": "Asha Rao", "chunk_ids": ["c1"]}]},
        {"id": "e_o", "name": "Orion", "type": "Organisation", "aliases": [], "description": "", "degree": 1,
         "chunk_ids": ["c1"], "origin": ["document"]}]
RELS = [{"source_id": "e_a", "target_id": "e_o", "type": "LEADS", "description": "Asha leads Orion.", "weight": 2.0,
         "chunk_ids": ["c1"], "evidence": [{"chunk_id": "c1", "text": "Asha leads Orion."}]}]
COMMS = [{"id": 0, "level": 0, "parent": None, "entity_ids": ["e_a", "e_o"], "profile": "Asha, Orion"}]


@pg
def test_save_load_version_and_workspaces(tmp_path, monkeypatch):
    core.reset_for_tests(tmp_path / "var")
    monkeypatch.setenv("CL_FILES_DIR", str(tmp_path / "workspaces"))
    assert kg.in_db() and kg.version() is None and not GS.ready()
    v1 = kg.save(ENTS, RELS, COMMS, {"c1", "c2"})
    g = kg.load()
    assert g["entities"][0]["names_evidence"] == ENTS[0]["names_evidence"]          # the data column round trip
    assert g["relations"][0]["evidence"][0]["text"] == "Asha leads Orion." and g["relations"][0]["weight"] == 2.0
    assert g["communities"][0]["entity_ids"] == ["e_a", "e_o"] and g["chunk_ids"] == ["c1", "c2"]
    assert GS.ready() and GS.local("Who leads Orion? Asha Rao")["entities"]
    nx1 = kg.networkx()
    assert nx1.number_of_nodes() == 2 and kg.networkx() is nx1                       # cached per version
    v2 = kg.save(ENTS[:1], [], [], {"c1"})
    assert v2 != v1 and len(GS.graph()["ents"]) == 1 and kg.networkx() is not nx1   # reloaded after the build
    with core.tenant_scope("other-ws"):
        assert kg.version() is None and kg.load() is None                           # another workspace: no graph


@pg
def test_older_build_file_is_moved_in(tmp_path, monkeypatch):
    core.reset_for_tests(tmp_path / "var")
    monkeypatch.setenv("CL_FILES_DIR", str(tmp_path / "workspaces"))
    with core.tenant_scope("acme-1"):
        from rag.config import OUT_DIR
        from pathlib import Path
        Path(OUT_DIR).mkdir(parents=True, exist_ok=True)
        (Path(OUT_DIR) / kg.FILE).write_text(json.dumps({"entities": ENTS, "relations": RELS, "communities": COMMS,
                                                         "chunk_ids": ["c1", "c2"]}))
        assert GS.ready() and kg.stats()["entities"] == 2
        assert kg.backfill() == {"entities": 0}                                     # once


def test_file_mode_without_postgres(tmp_path, monkeypatch):
    monkeypatch.delenv("CL_DATABASE_URL", raising=False)
    core.reset_for_tests(tmp_path / "var")
    import rag.config as RC
    import graph.kg as K
    monkeypatch.setattr(K, "OUT_DIR", tmp_path)
    assert not kg.in_db()
    kg.save(ENTS, RELS, COMMS, {"c1"})
    assert kg.load()["entities"][1]["name"] == "Orion" and (tmp_path / kg.FILE).exists()
    assert RC  # noqa: B018
