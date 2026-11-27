"""Step 4 - Vector store and index: Qdrant with a dense + sparse (BM25) collection.

Two ways to run Qdrant:
  server     QDRANT_URL (e.g. http://qdrant:6333 in Docker) and QDRANT_API_KEY (or QDRANT_API_KEY_FILE). Every
             workspace's passages are in one collection ("docs"), each point tagged with its workspace ("tenant",
             indexed) and searched only with that filter; point ids include the workspace. A build updates the
             collection in place: only new or changed passages are written (a content hash per point), then points of
             passages that are gone are deleted, so answers keep working during a rebuild and never see a half-empty
             index (old passages stay searchable until their replacements are written).
  embedded   no QDRANT_URL: a local folder per workspace (QDRANT_PATH), as before: a rebuild fills the other of two
             collections and switches when it is complete.

Output: outputs/4_index.json
"""
import uuid

from qdrant_client import QdrantClient, models

import hashlib
import os

from .config import COLLECTION, EMBED_DIM, QDRANT_PATH
from .utils import read_json, write_json

_clients: dict[str, QdrantClient] = {}       # embedded Qdrant: one folder (and client) per workspace
_actives: dict[str, str] = {}
_client = _active = None                         # (older callers reset these; the caches above are per workspace)


def server_url() -> str:
    return (os.environ.get("QDRANT_URL") or "").strip()


def server() -> bool:
    return bool(server_url())


def tenant() -> str:
    """The workspace tag of points in the shared server collection."""
    from .wspath import workspace_key
    return workspace_key() or "default"


def collection_name(base: str) -> str:
    """Other collections (graph entities, the metric catalog): one per workspace on a shared server."""
    from .wspath import workspace_key
    k = workspace_key()
    return f"{base}__{k}" if server() and k else base


def active_collection() -> str:
    """The collection searches use. Embedded: a rebuild fills the other one and switches only when it is complete,
    so answering never waits for (or sees half of) a rebuild. Server: always the shared collection."""
    if server():
        return COLLECTION
    key = str(QDRANT_PATH)
    if key not in _actives:
        import json
        from . import utils
        f = utils.OUT_DIR / "4_index.json"
        try:
            a = (json.loads(f.read_text(encoding="utf-8")).get("collection") if f.exists() else None) or COLLECTION
        except (OSError, ValueError, AttributeError):
            a = COLLECTION
        if a != COLLECTION and not client().collection_exists(a):
            a = COLLECTION
        _actives[key] = a
    return _actives[key]


def client() -> QdrantClient:
    if server():
        key = "server:" + server_url()
        if key not in _clients:
            from app.envfile import env
            _clients[key] = QdrantClient(url=server_url(), api_key=env("QDRANT_API_KEY") or None, timeout=60)
        return _clients[key]
    key = str(QDRANT_PATH)
    if key not in _clients:
        _clients[key] = QdrantClient(path=key)
    return _clients[key]


def _vectors_config():
    return ({"dense": models.VectorParams(size=EMBED_DIM, distance=models.Distance.COSINE,
                                          hnsw_config=models.HnswConfigDiff(m=16, ef_construct=100))},
            {"bm25": models.SparseVectorParams(modifier=models.Modifier.IDF)})


def _content_hash(c: dict, r: dict) -> str:
    h = hashlib.sha1()
    for k in ("source", "text"):
        h.update(str(c.get(k)).encode())
    h.update(repr((sorted(c.get("access") or []), c.get("headings") or [], c.get("pages") or [])).encode())
    h.update(repr((r["dense"][:8], r["sparse"].get("indices", [])[:8])).encode())
    return h.hexdigest()[:20]


def build_index_server() -> dict:
    """Incremental update of this workspace's points in the shared server collection (see the module docstring)."""
    from .retrieve import point_id
    chunks = {c["chunk_id"]: c for c in read_json("2_chunks.json")}
    emb = read_json("3_embeddings.json")
    qc, t = client(), tenant()
    if not qc.collection_exists(COLLECTION):
        dense, sparse = _vectors_config()
        qc.create_collection(COLLECTION, vectors_config=dense, sparse_vectors_config=sparse)
        for f in ("tenant", "access", "source"):
            qc.create_payload_index(COLLECTION, f, models.PayloadSchemaType.KEYWORD)
    mine = models.Filter(must=[models.FieldCondition(key="tenant", match=models.MatchValue(value=t))])
    have: dict[str, str] = {}
    offset = None
    while True:
        pts, offset = qc.scroll(COLLECTION, scroll_filter=mine, limit=1024, offset=offset, with_payload=["chunk_id", "h"],
                                with_vectors=False)
        for p in pts:
            have[str(p.id)] = (p.payload or {}).get("h", "")
        if offset is None:
            break
    want, points = set(), []
    for r in emb["vectors"]:
        c = chunks.get(r["chunk_id"])
        if c is None:
            continue
        pid, h = point_id(r["chunk_id"]), _content_hash(c, r)
        want.add(pid)
        if have.get(pid) == h:
            continue                                   # unchanged: not written again
        points.append(models.PointStruct(
            id=pid, vector={"dense": r["dense"], "bm25": models.SparseVector(**r["sparse"])},
            payload={k: c[k] for k in ("chunk_id", "source", "access", "headings", "pages", "text")} | {"tenant": t, "h": h}))
    for i in range(0, len(points), 256):              # new and changed passages first ...
        qc.upsert(COLLECTION, points=points[i:i + 256], wait=True)
    gone = [pid for pid in have if pid not in want]
    for i in range(0, len(gone), 1000):                # ... then the ones that are gone
        qc.delete(COLLECTION, points_selector=models.PointIdsList(points=gone[i:i + 1000]), wait=True)
    report = {"collection": COLLECTION, "storage": server_url(), "tenant": t,
              "points": qc.count(COLLECTION, count_filter=mine).count, "written": len(points), "deleted": len(gone),
              "unchanged": len(want) - len(points),
              "dense": {"size": EMBED_DIM, "distance": "cosine", "index": "HNSW m=16"},
              "sparse": {"name": "bm25", "modifier": "IDF"}, "payload_indexes": ["tenant", "access", "source"],
              "sources": sorted({c["source"] for c in chunks.values()}), "status": "incremental"}
    write_json("4_index.json", report)
    print(f"  Qdrant server: {len(points)} passages written, {len(gone)} removed, {report['unchanged']} unchanged")
    return report


def build_index() -> dict:
    if server():
        return build_index_server()
    chunks = {c["chunk_id"]: c for c in read_json("2_chunks.json")}
    emb = read_json("3_embeddings.json")
    qc = client()
    old = active_collection() if qc.collection_exists(active_collection()) else None
    target = f"{COLLECTION}_b" if old == f"{COLLECTION}_a" else f"{COLLECTION}_a"
    if qc.collection_exists(target):
        qc.delete_collection(target)
    qc.create_collection(
        target,
        vectors_config={"dense": models.VectorParams(
            size=EMBED_DIM, distance=models.Distance.COSINE,
            hnsw_config=models.HnswConfigDiff(m=16, ef_construct=100),
        )},
        sparse_vectors_config={"bm25": models.SparseVectorParams(modifier=models.Modifier.IDF)},
    )
    # payload indexes make filtered search fast (permissions / source)
    qc.create_payload_index(target, "access", models.PayloadSchemaType.KEYWORD)
    qc.create_payload_index(target, "source", models.PayloadSchemaType.KEYWORD)

    points = []
    for r in emb["vectors"]:
        c = chunks[r["chunk_id"]]
        points.append(models.PointStruct(
            id=str(uuid.uuid5(uuid.NAMESPACE_URL, r["chunk_id"])),
            vector={"dense": r["dense"], "bm25": models.SparseVector(**r["sparse"])},
            payload={k: c[k] for k in ("chunk_id", "source", "access", "headings", "pages", "text")},
        ))
    for i in range(0, len(points), 256):          # batches keep memory and request size modest
        qc.upsert(target, points=points[i:i + 256], wait=True)
    # Local Qdrant on a synced folder (OneDrive) can leave old points behind after delete_collection;
    # remove anything that is not in the current chunk set so stale documents cannot be retrieved.
    current = sorted({c["source"] for c in chunks.values()})
    qc.delete(target, points_selector=models.FilterSelector(filter=models.Filter(
        must_not=[models.FieldCondition(key="source", match=models.MatchAny(any=current))])), wait=True)

    info = qc.get_collection(target)
    report = {
        "collection": target,
        "storage": str(QDRANT_PATH),
        "points": qc.count(target).count,
        "dense": {"size": EMBED_DIM, "distance": "cosine", "index": "HNSW m=16"},
        "sparse": {"name": "bm25", "modifier": "IDF"},
        "payload_indexes": ["access", "source"],
        "sources": sorted({c["source"] for c in chunks.values()}),
        "status": str(info.status),
    }
    write_json("4_index.json", report)
    _actives[str(QDRANT_PATH)] = target           # switch: new searches use the new collection
    if old and old != target:
        try:
            qc.delete_collection(old)
        except Exception as e:                    # noqa: BLE001 - a search may still hold it; removed next time
            print(f"  old collection {old} kept for now: {e}")
    print(f"  indexed {report['points']} points into Qdrant collection '{target}'")
    return report
