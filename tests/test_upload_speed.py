"""Uploads from a chat: answers keep working during the rebuild, the rebuild only reads the new files,
build_status shows progress, and graph names never run across lines."""
import json
import threading
import time

import pytest

from app import engine, jobs


def test_index_rebuild_switches_collections_without_a_gap(tmp_path, monkeypatch):
    import rag.store as S
    import rag.utils as U
    from qdrant_client import models
    monkeypatch.setattr(U, "OUT_DIR", tmp_path)
    monkeypatch.setattr(S, "QDRANT_PATH", tmp_path / "q")
    monkeypatch.setattr(S, "_client", None)
    monkeypatch.setattr(S, "_active", None)
    monkeypatch.setattr(S, "EMBED_DIM", 4)

    def write(n):
        chunks = [{"chunk_id": f"c{i}", "source": "a.md", "access": ["public"], "headings": [], "pages": [],
                   "text": f"passage {i}"} for i in range(n)]
        (tmp_path / "2_chunks.json").write_text(json.dumps(chunks))
        (tmp_path / "3_embeddings.json").write_text(json.dumps({"vectors": [
            {"chunk_id": c["chunk_id"], "dense": [1.0, 0.0, 0.0, float(i)],
             "sparse": {"indices": [i], "values": [1.0]}} for i, c in enumerate(chunks)]}))

    write(3)
    S.build_index()
    first = S.active_collection()
    assert S.client().count(first).count == 3
    write(5)
    S.build_index()
    second = S.active_collection()
    assert second != first and S.client().count(second).count == 5
    assert not S.client().collection_exists(first)                 # the old one is removed after the switch
    assert json.loads((tmp_path / "4_index.json").read_text())["collection"] == second
    q = S.client().query_points(second, query=[1.0, 0, 0, 0], using="dense", limit=2,
                                query_filter=models.Filter(must=[models.FieldCondition(
                                    key="access", match=models.MatchAny(any=["public"]))]))
    assert len(q.points) == 2


def test_answers_do_not_wait_for_a_running_build(monkeypatch):
    from app import mcp_server as M
    jobs.LOCK.acquire()                                            # a rebuild is running
    try:
        t = time.time()
        out = M.run({"id": "x", "name": "t", "capabilities": [], "groups": ["public"]}, "list_documents", {},
                    lambda: {"documents": []})
        assert out == {"documents": []} and time.time() - t < 2
    finally:
        jobs.LOCK.release()
    # metric questions wait only while metric tables are rebuilt, and briefly
    engine.DATA_IDLE.clear()
    threading.Timer(0.3, engine.DATA_IDLE.set).start()
    t = time.time()
    M._wait()
    assert 0.2 < time.time() - t < 5


def test_upload_reads_only_the_new_files_for_the_graph(tmp_path, monkeypatch):
    import graph.build as B
    import graph.nlp_extract as N
    chunks = [{"chunk_id": "old1", "source": "old.md", "headings": [], "text": "Asha Rao leads the Pune office."},
              {"chunk_id": "old2", "source": "gone.md", "headings": [], "text": "Ravi Kumar works in Delhi."},
              {"chunk_id": "new1", "source": "new.md", "headings": [], "text": "Meera Iyer works in Chennai."}]
    out = tmp_path / "gr_extract_fast.jsonl"
    out.write_text(json.dumps({"chunk_id": "old1", "entities": [{"name": "Asha Rao", "type": "Person"}],
                               "relations": []}) + "\n" +
                   json.dumps({"chunk_id": "old2", "entities": [{"name": "Ravi Kumar", "type": "Person"}],
                               "relations": []}) + "\n")
    chunks = [c for c in chunks if c["source"] != "gone.md"]       # gone.md was deleted
    (tmp_path / "2_chunks.json").write_text(json.dumps(chunks))
    monkeypatch.setattr(engine, "OUT_DIR", tmp_path)
    monkeypatch.setattr(N, "CHUNKS", tmp_path / "2_chunks.json")
    monkeypatch.setattr(engine, "graph_mode", lambda: "fast")
    seen, built = [], []
    real = N.Extractor.run
    monkeypatch.setattr(N.Extractor, "run", lambda self, c: seen.append(c["chunk_id"]) or real(self, c))
    monkeypatch.setattr(B, "build", lambda paths: built.append(paths))
    engine.update_graph(["new.md"])
    recs = {json.loads(x)["chunk_id"] for x in out.read_text().splitlines()}
    assert seen == ["new1"] and recs == {"old1", "new1"} and built       # only new passages read; deleted one dropped


def test_upload_skips_graph_when_it_was_never_built_for_these_passages(tmp_path, monkeypatch, capsys):
    import graph.nlp_extract as N
    chunks = [{"chunk_id": f"c{i}", "source": "old.md", "headings": [], "text": "x"} for i in range(20)]
    chunks.append({"chunk_id": "n", "source": "new.md", "headings": [], "text": "Meera Iyer works in Chennai."})
    (tmp_path / "2_chunks.json").write_text(json.dumps(chunks))
    (tmp_path / "gr_extract_fast.jsonl").write_text(json.dumps({"chunk_id": "c0", "entities": [], "relations": []}) + "\n")
    monkeypatch.setattr(engine, "OUT_DIR", tmp_path)
    monkeypatch.setattr(N, "CHUNKS", tmp_path / "2_chunks.json")
    monkeypatch.setattr(N.Extractor, "run", lambda self, c: pytest.fail("must not read every passage"))
    engine.update_graph(["new.md"])
    assert "skipped" in capsys.readouterr().out


def test_build_status_shows_steps():
    job = {"id": "j", "log": [], "status": "running"}
    log = jobs._Log(job)
    print_lines = ["Rebuilding for 1 new file(s): a.md", "[1/4] Reading documents", "  unchanged big.docx (parsed before)",
                   "[11/25/26 11:10:25] INFO     HTTP Request: GET", "[graph] reading 3 new passages", "  50/120 passages (40s)"]
    for line in print_lines:
        log.write(line + "\n")
    steps = [s.split(" ", 1)[1] for s in job["steps"]]
    assert steps == ["Rebuilding for 1 new file(s): a.md", "[1/4] Reading documents", "unchanged big.docx (parsed before)",
                     "[graph] reading 3 new passages", "50/120 passages (40s)"]


def test_graph_names_never_span_lines():
    from graph.build import _clean_name
    from graph.nlp_extract import clean
    assert clean("Tomas Rivera\n\nlumora_incidents") == "Tomas Rivera"
    assert clean("owner: Meera Kulkarni") == "Meera Kulkarni"
    assert _clean_name("Priya Nair\n\nlumora_incidents") == "Priya Nair"
    assert clean("The Grid Analytics") == "Grid Analytics"
