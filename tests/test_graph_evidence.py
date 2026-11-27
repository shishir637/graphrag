"""Graph leak fix (0.9): link sentences, entity descriptions, names and aliases come only from passages the caller
may see (evidence kept per passage), in find_connections, explore_entity, overview and the graph explorer. No models."""
import json


def test_graph_shows_only_evidence_from_visible_passages(tmp_path, monkeypatch):
    from graph import search as GS
    from graph.build import aggregate
    rels = [{"s": "a", "t": "b", "type": "LEADS", "strength": 5, "chunk_id": "pub1",
             "description": "Asha leads the Orion team."},
            {"s": "a", "t": "b", "type": "LEADS", "strength": 5, "chunk_id": "hr1",
             "description": "Asha leads Orion while on a performance plan (confidential)."}]
    agg = aggregate(rels, {"a": "e_a", "b": "e_b"})
    assert [e["chunk_id"] for e in agg[0]["evidence"]] == ["pub1", "hr1"]
    build = {"entities": [
        {"id": "e_a", "name": "Asha Rao", "type": "Person", "aliases": ["Agent Falcon"], "chunk_ids": ["pub1", "hr1"],
         "names_evidence": [{"text": "Asha Rao", "chunk_ids": ["pub1"]}, {"text": "Agent Falcon", "chunk_ids": ["hr1"]}],
         "description_evidence": [{"text": "Team lead", "chunk_ids": ["pub1"]},
                                  {"text": "On a performance plan", "chunk_ids": ["hr1"]}]},
        {"id": "e_b", "name": "Orion", "type": "Organisation", "aliases": [], "chunk_ids": ["pub1", "hr1"]}],
        "relations": [agg[0]], "communities": []}
    f = tmp_path / "_graph_build.json"
    f.write_text(json.dumps(build))
    monkeypatch.setattr(GS, "BUILD", f)
    GS._GS.clear()
    access = {"pub1": ["public"], "hr1": ["hr"]}
    vis = lambda c, g: g is None or bool(set(access.get(c, [])) & set(g))          # noqa: E731
    pub = GS.local("Who leads Orion? Asha Rao", ["public"], vis)
    text = json.dumps(pub)
    assert "confidential" not in text and "Agent Falcon" not in text and "Asha leads the Orion team." in text
    e = GS.entity("Asha Rao", ["public"], vis)
    assert e["aliases"] == [] and "performance plan" not in json.dumps(e) and e["description"] == "Team lead"
    hr = GS.entity("Asha Rao", ["public", "hr"], vis)
    assert hr["aliases"] == ["Agent Falcon"] and "performance plan" in hr["description"]


def test_graph_explorer_for_a_viewer(tmp_path, monkeypatch):
    from app import engine
    from graph.access import _BY_PATH
    chunks = [{"chunk_id": "pub1", "source": "a.md", "access": ["public"], "text": "t"},
              {"chunk_id": "hr1", "source": "b.md", "access": ["hr"], "text": "t"}]
    (tmp_path / "2_chunks.json").write_text(json.dumps(chunks))
    build = {"entities": [
        {"id": "e_a", "name": "Asha Rao", "type": "Person", "chunk_ids": ["pub1", "hr1"], "aliases": ["Agent Falcon"],
         "names_evidence": [{"text": "Asha Rao", "chunk_ids": ["pub1"]}, {"text": "Agent Falcon", "chunk_ids": ["hr1"]}],
         "description_evidence": [{"text": "Team lead", "chunk_ids": ["pub1"]},
                                  {"text": "On a performance plan", "chunk_ids": ["hr1"]}]},
        {"id": "e_s", "name": "Secret Project", "type": "Project", "chunk_ids": ["hr1"]}],
        "relations": [{"source_id": "e_a", "target_id": "e_s", "type": "WORKS_ON", "chunk_ids": ["hr1"],
                       "evidence": [{"chunk_id": "hr1", "text": "Asha works on the secret project."}]}],
        "communities": []}
    (tmp_path / "_graph_build.json").write_text(json.dumps(build))
    import rag.config as RC
    monkeypatch.setattr(RC, "OUT_DIR", tmp_path)
    monkeypatch.setattr(engine, "OUT_DIR", tmp_path)
    _BY_PATH.clear()
    viewer = json.dumps(engine.graph_explore(groups=["public"]))
    assert "Secret Project" not in viewer and "performance plan" not in viewer and "Team lead" in viewer
    admin = json.dumps(engine.graph_explore())
    assert "Secret Project" in admin and "performance plan" in admin
