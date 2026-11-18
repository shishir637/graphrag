"""Step 3 - Embedding: BGE-M3 dense vectors + BM25 sparse vectors for every chunk.

Output: outputs/3_embeddings.json
"""
from .config import EMBED_DIM, EMBED_MODEL, SPARSE_MODEL
from .models import embed_dense, embed_sparse
from .utils import read_json, timer, write_json


def embed_all() -> list[dict]:
    chunks = read_json("2_chunks.json")
    texts = [c["embed_text"] for c in chunks]
    timings = {}
    with timer("dense_seconds", timings):
        dense = embed_dense(texts)
    with timer("sparse_seconds", timings):
        sparse = embed_sparse(texts)

    records = [
        {"chunk_id": c["chunk_id"], "dense": d, "sparse": s}
        for c, d, s in zip(chunks, dense, sparse)
    ]
    write_json("3_embeddings.json", {
        "dense_model": EMBED_MODEL, "dense_dim": EMBED_DIM, "sparse_model": SPARSE_MODEL,
        "count": len(records), **timings, "vectors": records,
    })
    print(f"  embedded {len(records)} chunks ({timings})")
    return records
