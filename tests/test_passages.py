"""Passages in Postgres (0.7): the chunking writes them to the passages table, unchanged passages are not touched,
every reader goes through rag/passages.py, the search index keeps no text (it is fetched by id in one query), and
old workspaces are moved over from outputs/2_chunks.json automatically and idempotently."""
import json
import os

import pytest

from app import core, jobs, vault
from rag import passages
from tests.conftest import needs_pg

pytestmark = needs_pg


def mk(src, texts, access=("public",)):
    return [{"chunk_id": f"{src}-{i}", "source": src, "access": list(access), "headings": ["H"], "pages": [1],
             "text": t, "embed_text": f"H\n{t}"} for i, t in enumerate(texts)]


@pytest.fixture
def ws(tmp_path):
    jobs.stop_workers()
    core.reset_for_tests(tmp_path / "var")
    vault.reset_for_tests()
    return core.create_tenant("Acme"), core.create_tenant("Globex")


def test_save_is_incremental_and_versioned(ws):
    a, b = ws
    with core.tenant_scope(a):
        assert passages.stamp() is None and not passages.exists()
        r = passages.save_all(mk("a.md", ["one", "two"]) + mk("b.md", ["three"]))
        assert (r["added"], r["removed"]) == (3, 0)
        v1 = passages.stamp()
        assert passages.save_all(mk("a.md", ["one", "two"]) + mk("b.md", ["three"]))["unchanged"] == 3
        assert passages.stamp() == v1                                  # nothing changed: same version
        r = passages.save_all(mk("a.md", ["one", "two edited"]))
        assert (r["changed"], r["removed"], r["unchanged"]) == (1, 1, 1) and passages.stamp() != v1
        allp = passages.all()
        assert [p["chunk_id"] for p in allp] == ["a.md-0", "a.md-1"] and allp[1]["text"] == "two edited"
        assert allp[0]["headings"] == ["H"] and allp[0]["embed_text"] == "H\none" and allp[0]["position"] == 0
        assert passages.texts(["a.md-1", "nope"]) == {"a.md-1": "two edited"}
        assert passages.counts_by_source() == {"a.md": 2}
    with core.tenant_scope(b):                                         # row-level security: other workspace
        assert passages.all() == [] and passages.by_ids(["a.md-0"]) == {}


def test_readers_use_the_repository(ws):
    a, _ = ws
    from app import engine
    from graph.access import visible
    from rag.retrieve import fill_texts, neighbours
    with core.tenant_scope(a):
        passages.save_all(mk("a.md", ["intro", "rule one", "rule two"]) + mk("hr.md", ["secret"], ("hr",)))
        assert visible("hr.md-0", ["hr"]) and not visible("hr.md-0", ["public"])
        assert engine.passage("a.md-1", ["public"])["text"] == "rule one"
        assert engine.passage("hr.md-0", ["public"]) is None
        assert neighbours("a.md-1", ["public"])["after"]["id"] == "a.md-2"
        hits = fill_texts([{"chunk_id": "a.md-2", "text": None}, {"chunk_id": "gone", "text": None}])
        assert hits == [{"chunk_id": "a.md-2", "text": "rule two"}]      # a passage that no longer exists is dropped
        from app.answerable import vocabulary
        assert "secret" in vocabulary() and "rule" in vocabulary()


def test_backfill_from_the_old_file(ws):
    a, _ = ws
    from rag.config import OUT_DIR
    with core.tenant_scope(a):
        out = OUT_DIR.resolve_now()
        out.mkdir(parents=True)
        (out / "2_chunks.json").write_text(json.dumps(mk("old.md", ["kept text"])), encoding="utf-8")
        assert passages.backfill() == {"passages": 1}
        assert passages.all()[0]["text"] == "kept text"
        assert not (out / "2_chunks.json").exists() and (out / "2_chunks.json.pre-0.7").exists()
        v = passages.stamp()
        assert passages.backfill() == {"passages": 0} and passages.stamp() == v


def test_search_index_keeps_no_text(ws, monkeypatch):
    import httpx
    url = os.environ.get("CL_TEST_QDRANT_URL", "http://127.0.0.1:6399")
    try:
        httpx.get(url + "/collections", timeout=3)
    except Exception:                                                  # noqa: BLE001
        pytest.skip("no test Qdrant")
    from rag import store
    from rag.config import OUT_DIR
    from rag.retrieve import fetch_chunks, point_id
    monkeypatch.setenv("QDRANT_URL", url)
    monkeypatch.setattr(store, "_client", None)
    a, _ = ws
    with core.tenant_scope(a):
        cs = mk("a.md", ["alpha passage", "beta passage"])
        passages.save_all(cs)
        out = OUT_DIR.resolve_now()
        out.mkdir(parents=True, exist_ok=True)
        (out / "3_embeddings.json").write_text(json.dumps({"vectors": [
            {"chunk_id": c["chunk_id"], "text_hash": c["chunk_id"], "dense": [0.1 + i / 10] * 1024,
             "sparse": {"indices": [1], "values": [1.0]}} for i, c in enumerate(cs)]}))
        store.build_index()
        p = store.client().retrieve("docs", ids=[point_id("a.md-0")], with_payload=True)[0]
        assert "text" not in p.payload and p.payload["chunk_id"] == "a.md-0"
        got = fetch_chunks(["a.md-1", "a.md-0"], ["public"])
        assert [(h["chunk_id"], h["text"]) for h in got] == [("a.md-1", "beta passage"), ("a.md-0", "alpha passage")]
