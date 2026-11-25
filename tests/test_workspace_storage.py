"""Per-workspace storage: folders, content-based passage ids, document groups in Postgres, and the shared Qdrant
server (workspace filter, incremental writes, no dropped collection). Needs the test Postgres and Qdrant
(CL_TEST_QDRANT_URL, default http://127.0.0.1:6399); no models."""
import json
import os
import random

import pytest

from app import core
from tests.conftest import needs_pg

pytestmark = needs_pg
QDRANT = os.environ.get("CL_TEST_QDRANT_URL", "http://127.0.0.1:6399")


@pytest.fixture
def two(tmp_path, monkeypatch):
    core.reset_for_tests(tmp_path / "var")
    monkeypatch.setenv("CL_FILES_DIR", str(tmp_path / "files"))
    return core.create_tenant("Acme"), core.create_tenant("Globex")


def test_folders_follow_the_workspace(two, tmp_path):
    from rag.config import DOCS_DIR, OUT_DIR
    from semantic.config import DATA_DIR
    a, b = two
    chunks = OUT_DIR / "2_chunks.json"                       # a module-level constant stays lazy
    with core.tenant_scope(a):
        assert str(DOCS_DIR) == str(tmp_path / "files" / a / "documents")
        assert str(chunks) == str(tmp_path / "files" / a / "outputs" / "2_chunks.json")
        DATA_DIR.mkdir(parents=True)
        (DATA_DIR / "t.csv").write_text("x\n1\n")
    with core.tenant_scope(b):
        assert not (DATA_DIR / "t.csv").exists()
    assert "files" not in str(DOCS_DIR)                       # command line: the repo folders


def test_content_based_chunk_ids():
    from rag.chunk import _chunk_id
    seen = {}
    a = _chunk_id("d.md", ["Leave"], "Annual leave is 24 days.", seen)
    b = _chunk_id("d.md", ["Leave"], "Sick leave is 12 days.", seen)
    dup = _chunk_id("d.md", ["Leave"], "Annual leave is 24 days.", seen)
    assert len({a, b, dup}) == 3
    assert _chunk_id("d.md", ["Leave"], "Annual leave is 24 days.", {}) == a   # same text, same id, any position


def test_document_groups_in_postgres(two):
    from rag import registry
    a, b = two
    with core.tenant_scope(a):
        registry.set_groups("leave.md", ["hr"])
        assert registry.access_map() == {"leave.md": ["hr"]}
    with core.tenant_scope(b):
        assert registry.access_map() == {}


def _vec(seed):
    r = random.Random(seed)
    return [r.random() for _ in range(1024)]


def _write(outputs, docs):
    outputs.mkdir(parents=True, exist_ok=True)
    chunks, vecs = [], []
    for src, texts in docs.items():
        for t in texts:
            cid = f"{src}:{t}"[:40]
            chunks.append({"chunk_id": cid, "source": src, "access": ["public"], "headings": [], "pages": [], "text": t})
            vecs.append({"chunk_id": cid, "text_hash": str(hash(t)), "dense": _vec(t),
                         "sparse": {"indices": [1, 2], "values": [0.5, 0.5]}})
    (outputs / "2_chunks.json").write_text(json.dumps(chunks))
    (outputs / "3_embeddings.json").write_text(json.dumps({"vectors": vecs}))


def test_shared_qdrant_is_incremental_and_isolated(two, monkeypatch):
    import httpx
    try:
        httpx.get(QDRANT + "/collections", timeout=3)
    except Exception:                                     # noqa: BLE001
        pytest.skip("no test Qdrant")
    from rag import store
    from rag.config import OUT_DIR
    from rag.retrieve import build_filter
    monkeypatch.setenv("QDRANT_URL", QDRANT)
    monkeypatch.setattr(store, "_client", None)
    a, b = two
    with core.tenant_scope(a):
        _write(OUT_DIR.resolve_now(), {"a.md": ["alpha one", "alpha two"], "b.md": ["beta"]})
        r1 = store.build_index()
        assert r1["written"] == 3 and r1["deleted"] == 0
        assert store.build_index()["written"] == 0                     # nothing changed: nothing written
        _write(OUT_DIR.resolve_now(), {"a.md": ["alpha one", "alpha two changed"]})
        r3 = store.build_index()
        assert r3["written"] == 1 and r3["deleted"] == 2               # edited passage + removed document
    with core.tenant_scope(b):
        _write(OUT_DIR.resolve_now(), {"g.md": ["gamma"]})
        store.build_index()
        pts, _ = store.client().scroll("docs", scroll_filter=build_filter(None, None), limit=100)
        assert {p.payload["source"] for p in pts} == {"g.md"}          # Globex never sees Acme's passages
    with core.tenant_scope(a):
        pts, _ = store.client().scroll("docs", scroll_filter=build_filter(None, None), limit=100)
        assert {p.payload["text"] for p in pts} == {"alpha one", "alpha two changed"}
