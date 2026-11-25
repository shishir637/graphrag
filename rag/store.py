"""Step 4 - Vector store and index: Qdrant with a dense + sparse (BM25) collection.

Qdrant runs as a server (QDRANT_URL, e.g. the Docker service) shared by all workspaces: one collection, every point
carries its workspace ("tenant", indexed as Qdrant's tenant key) and every search filters on it. Without QDRANT_URL
an embedded Qdrant in qdrant_data/ is used (command line, tests).

Incremental: only passages that are new or whose text changed are written; passages of removed or edited
documents are deleted. The collection is never dropped, so questions keep working during a rebuild.

Output: outputs/4_index.json
"""
import os
import uuid

from qdrant_client import QdrantClient, models

from .config import COLLECTION, EMBED_DIM, QDRANT_PATH
from .utils import read_json, write_json
from .wspath import tenant_key

_client = None


def client() -> QdrantClient:
    global _client
    if _client is None:
        url = os.environ.get("QDRANT_URL")
        _client = QdrantClient(url=url, api_key=os.environ.get("QDRANT_API_KEY") or None, timeout=60,
                               https=url.startswith("https")) if url else QdrantClient(path=str(QDRANT_PATH))
    return _client


def ensure_collection(qc: QdrantClient):
    if not qc.collection_exists(COLLECTION):
        qc.create_collection(
            COLLECTION,
            vectors_config={"dense": models.VectorParams(
                size=EMBED_DIM, distance=models.Distance.COSINE,
                hnsw_config=models.HnswConfigDiff(m=16, ef_construct=100),
            )},
            sparse_vectors_config={"bm25": models.SparseVectorParams(modifier=models.Modifier.IDF)},
        )
    # payload indexes make filtered search fast (workspace / permissions / source)
    try:
        qc.create_payload_index(COLLECTION, "tenant", models.KeywordIndexParams(type="keyword", is_tenant=True))
    except Exception:                                      # noqa: BLE001 - older servers / embedded mode
        qc.create_payload_index(COLLECTION, "tenant", models.PayloadSchemaType.KEYWORD)
    qc.create_payload_index(COLLECTION, "access", models.PayloadSchemaType.KEYWORD)
    qc.create_payload_index(COLLECTION, "source", models.PayloadSchemaType.KEYWORD)


def _existing(qc: QdrantClient, tenant: str) -> dict[str, str]:
    """point id -> text hash of this workspace's points."""
    flt = models.Filter(must=[models.FieldCondition(key="tenant", match=models.MatchValue(value=tenant))]) if tenant         else None
    out, offset = {}, None
    while True:
        pts, offset = qc.scroll(COLLECTION, scroll_filter=flt, limit=1024, offset=offset,
                                with_payload=["text_hash", "tenant"], with_vectors=False)
        for p in pts:
            if not tenant and p.payload.get("tenant"):
                continue
            out[str(p.id)] = p.payload.get("text_hash") or ""
        if offset is None:
            return out


def build_index() -> dict:
    from .retrieve import point_id
    chunks = {c["chunk_id"]: c for c in read_json("2_chunks.json")}
    emb = read_json("3_embeddings.json")
    qc = client()
    ensure_collection(qc)
    tenant = tenant_key()
    have = _existing(qc, tenant)

    points, keep = [], set()
    for r in emb["vectors"]:
        c = chunks[r["chunk_id"]]
        pid = point_id(r["chunk_id"])
        keep.add(pid)
        version = f"{r.get('text_hash', '')}|{'.'.join(sorted(c['access']))}"
        if have.get(pid) == version:
            continue                                  # unchanged passage and access: nothing to write
        points.append(models.PointStruct(
            id=pid, vector={"dense": r["dense"], "bm25": models.SparseVector(**r["sparse"])},
            payload={k: c[k] for k in ("chunk_id", "source", "access", "headings", "pages", "text")} |
                    {"tenant": tenant, "text_hash": version},
        ))
    for i in range(0, len(points), 256):          # batches keep memory and request size modest
        qc.upsert(COLLECTION, points=points[i:i + 256], wait=True)
    stale = [pid for pid in have if pid not in keep]
    for i in range(0, len(stale), 1000):
        qc.delete(COLLECTION, points_selector=models.PointIdsList(points=stale[i:i + 1000]), wait=True)

    report = {
        "collection": COLLECTION,
        "storage": os.environ.get("QDRANT_URL") or str(QDRANT_PATH),
        "points": len(keep), "written": len(points), "deleted": len(stale), "unchanged": len(keep) - len(points),
        "dense": {"size": EMBED_DIM, "distance": "cosine", "index": "HNSW m=16"},
        "sparse": {"name": "bm25", "modifier": "IDF"},
        "payload_indexes": ["tenant", "access", "source"],
        "sources": sorted({c["source"] for c in chunks.values()}),
    }
    write_json("4_index.json", report)
    print(f"  index: {len(points)} passages written, {len(stale)} removed, {report['unchanged']} unchanged")
    return report
