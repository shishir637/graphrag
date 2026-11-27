"""Qdrant as its own server (0.9): one shared collection, every point tagged with its workspace and searched only
with that filter; point ids include the workspace; a build writes only new or changed passages, then deletes the
ones that are gone (searches keep working during a rebuild). Needs a throwaway Qdrant server: CL_TEST_QDRANT_URL
(default http://127.0.0.1:6398) and CL_TEST_QDRANT_KEY (default addon-test-key), e.g.
  docker run -d --name cl-addon-qdrant -p 127.0.0.1:6398:6333 -e QDRANT__SERVICE__API_KEY=addon-test-key qdrant/qdrant:v1.19.1
"""
import json
import os
import secrets

import pytest

URL = os.environ.get("CL_TEST_QDRANT_URL", "http://127.0.0.1:6398")
KEY = os.environ.get("CL_TEST_QDRANT_KEY", "addon-test-key")


def _up() -> bool:
    try:
        import httpx
        return httpx.get(URL + "/collections", headers={"api-key": KEY}, timeout=3).status_code == 200
    except Exception:                                      # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _up(), reason="no test Qdrant server")


@pytest.fixture
def qd(tmp_path, monkeypatch):
    from rag import store
    monkeypatch.setenv("QDRANT_URL", URL)
    monkeypatch.setenv("QDRANT_API_KEY", KEY)
    monkeypatch.setenv("CL_FILES_DIR", str(tmp_path / "workspaces"))
    name = "docs_test_" + secrets.token_hex(4)
    monkeypatch.setattr(store, "COLLECTION", name)
    monkeypatch.setattr(store, "EMBED_DIM", 4)
    store._clients.clear()
    yield store, name
    store.client().delete_collection(name)
    store._clients.clear()


def write(texts: dict[str, str], access=("public",)):
    from rag.config import OUT_DIR
    from pathlib import Path
    out = Path(OUT_DIR)
    out.mkdir(parents=True, exist_ok=True)
    chunks = [{"chunk_id": cid, "source": f"{cid.split('-')[0]}.md", "access": list(access), "headings": [], "pages": [],
               "text": t} for cid, t in texts.items()]
    (out / "2_chunks.json").write_text(json.dumps(chunks))
    (out / "3_embeddings.json").write_text(json.dumps({"vectors": [
        {"chunk_id": c["chunk_id"], "dense": [1.0, 0.0, float(i), 0.5], "sparse": {"indices": [i], "values": [1.0]}}
        for i, c in enumerate(chunks)]}))


def search_ids(store):
    from rag.retrieve import build_filter
    pts = store.client().query_points(store.active_collection(), query=[1.0, 0, 0, 0], using="dense", limit=20,
                                      query_filter=build_filter(["public"], None)).points
    return sorted(p.payload["chunk_id"] for p in pts)


def test_workspaces_share_one_collection_but_never_see_each_other(qd):
    from app.db import tenant_scope
    store, name = qd
    with tenant_scope("acme-1"):
        write({"a-1": "Acme leave policy", "a-2": "Acme pricing"})
        assert store.build_index()["written"] == 2
    with tenant_scope("globex-2"):
        write({"a-1": "Globex secret plan"})                        # same passage id in another workspace
        store.build_index()
        assert search_ids(store) == ["a-1"]
        from rag.retrieve import fetch_chunks
        assert [c["text"] for c in fetch_chunks(["a-1", "a-2"], ["public"])] == ["Globex secret plan"]
    with tenant_scope("acme-1"):
        assert search_ids(store) == ["a-1", "a-2"]
        from rag.retrieve import fetch_chunks
        assert {c["text"] for c in fetch_chunks(["a-1"], ["public"])} == {"Acme leave policy"}
    assert store.client().count(name).count == 3


def test_incremental_rebuild_writes_only_changes(qd):
    from app.db import tenant_scope
    store, _ = qd
    with tenant_scope("acme-1"):
        write({"a-1": "one", "a-2": "two", "b-1": "three"})
        store.build_index()
        write({"a-1": "one", "a-2": "two changed", "c-1": "four"})
        r = store.build_index()
        assert (r["written"], r["deleted"], r["unchanged"]) == (2, 1, 1)
        assert search_ids(store) == ["a-1", "a-2", "c-1"]
        r = store.build_index()
        assert (r["written"], r["deleted"]) == (0, 0)                # nothing changed: nothing written
        write({"a-1": "one", "a-2": "two changed", "c-1": "four"}, access=("hr",))
        assert store.build_index()["written"] == 3                   # access changed: points rewritten
        assert search_ids(store) == []                               # public no longer sees them
