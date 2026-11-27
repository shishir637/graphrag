"""Step 4 - Vector store and index: Qdrant (embedded/local mode) with a dense + sparse (BM25) collection.

Output: outputs/4_index.json
"""
import uuid

from qdrant_client import QdrantClient, models

from .config import COLLECTION, EMBED_DIM, QDRANT_PATH
from .utils import read_json, write_json

_clients: dict[str, QdrantClient] = {}       # embedded Qdrant: one folder (and client) per workspace
_actives: dict[str, str] = {}
_client = _active = None                         # (older callers reset these; the caches above are per workspace)


def active_collection() -> str:
    """The collection searches use. A rebuild fills the other one and switches only when it is complete,
    so answering never waits for (or sees half of) a rebuild."""
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
    key = str(QDRANT_PATH)
    if key not in _clients:
        _clients[key] = QdrantClient(path=key)
    return _clients[key]


def build_index() -> dict:
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
