"""Step 4 - Vector store and index: Qdrant (embedded/local mode) with a dense + sparse (BM25) collection.

Output: outputs/4_index.json
"""
import uuid

from qdrant_client import QdrantClient, models

from .config import COLLECTION, EMBED_DIM, QDRANT_PATH
from .utils import read_json, write_json

_client = None


def client() -> QdrantClient:
    global _client
    if _client is None:
        _client = QdrantClient(path=str(QDRANT_PATH))
    return _client


def build_index() -> dict:
    chunks = {c["chunk_id"]: c for c in read_json("2_chunks.json")}
    emb = read_json("3_embeddings.json")
    qc = client()

    if qc.collection_exists(COLLECTION):
        qc.delete_collection(COLLECTION)          # full rebuild keeps things simple
    qc.create_collection(
        COLLECTION,
        vectors_config={"dense": models.VectorParams(
            size=EMBED_DIM, distance=models.Distance.COSINE,
            hnsw_config=models.HnswConfigDiff(m=16, ef_construct=100),
        )},
        sparse_vectors_config={"bm25": models.SparseVectorParams(modifier=models.Modifier.IDF)},
    )
    # payload indexes make filtered search fast (permissions / source)
    qc.create_payload_index(COLLECTION, "access", models.PayloadSchemaType.KEYWORD)
    qc.create_payload_index(COLLECTION, "source", models.PayloadSchemaType.KEYWORD)

    points = []
    for r in emb["vectors"]:
        c = chunks[r["chunk_id"]]
        points.append(models.PointStruct(
            id=str(uuid.uuid5(uuid.NAMESPACE_URL, r["chunk_id"])),
            vector={"dense": r["dense"], "bm25": models.SparseVector(**r["sparse"])},
            payload={k: c[k] for k in ("chunk_id", "source", "access", "headings", "pages", "text")},
        ))
    for i in range(0, len(points), 256):          # batches keep memory and request size modest
        qc.upsert(COLLECTION, points=points[i:i + 256], wait=True)
    # Local Qdrant on a synced folder (OneDrive) can leave old points behind after delete_collection;
    # remove anything that is not in the current chunk set so stale documents cannot be retrieved.
    current = sorted({c["source"] for c in chunks.values()})
    qc.delete(COLLECTION, points_selector=models.FilterSelector(filter=models.Filter(
        must_not=[models.FieldCondition(key="source", match=models.MatchAny(any=current))])), wait=True)

    info = qc.get_collection(COLLECTION)
    report = {
        "collection": COLLECTION,
        "storage": str(QDRANT_PATH),
        "points": qc.count(COLLECTION).count,
        "dense": {"size": EMBED_DIM, "distance": "cosine", "index": "HNSW m=16"},
        "sparse": {"name": "bm25", "modifier": "IDF"},
        "payload_indexes": ["access", "source"],
        "sources": sorted({c["source"] for c in chunks.values()}),
        "status": str(info.status),
    }
    write_json("4_index.json", report)
    print(f"  indexed {report['points']} points into Qdrant collection '{COLLECTION}'")
    return report
