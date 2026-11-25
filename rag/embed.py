"""Step 3 - Embedding: BGE-M3 dense vectors + BM25 sparse vectors for every chunk.

Incremental: a passage whose text is unchanged since the last run keeps its vectors (matched by a hash of
the text that is embedded), so adding or editing one document re-embeds only that document's passages.

Output: outputs/3_embeddings.json
"""
import hashlib

from .config import EMBED_DIM, EMBED_MODEL, OUT_DIR, SPARSE_MODEL
from .models import embed_dense, embed_sparse
from .utils import read_json, timer, write_json


def _h(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def embed_all() -> list[dict]:
    chunks = read_json("2_chunks.json")
    old = {}
    if (OUT_DIR / "3_embeddings.json").exists():
        try:
            prev = read_json("3_embeddings.json")
            if prev.get("dense_model") == EMBED_MODEL and prev.get("sparse_model") == SPARSE_MODEL:
                old = {v.get("text_hash"): v for v in prev["vectors"] if v.get("text_hash")}
                if not old:   # file from before hashes were stored: match by passage id (one-time migration)
                    by_id = {c["chunk_id"]: _h(c["embed_text"]) for c in chunks}
                    old = {by_id[v["chunk_id"]]: v for v in prev["vectors"] if v["chunk_id"] in by_id}
        except Exception:                                      # noqa: BLE001 - a broken cache is rebuilt
            old = {}
    hashes = [_h(c["embed_text"]) for c in chunks]
    todo = [i for i, h in enumerate(hashes) if h not in old]
    timings = {"reused": len(chunks) - len(todo), "embedded": len(todo)}
    texts = [chunks[i]["embed_text"] for i in todo]
    with timer("dense_seconds", timings):
        dense = embed_dense(texts) if texts else []
    with timer("sparse_seconds", timings):
        sparse = embed_sparse(texts) if texts else []
    new = {hashes[i]: {"dense": d, "sparse": s} for i, d, s in zip(todo, dense, sparse)}

    records = []
    for c, h in zip(chunks, hashes):
        v = new.get(h) or old[h]
        records.append({"chunk_id": c["chunk_id"], "text_hash": h, "dense": v["dense"], "sparse": v["sparse"]})
    write_json("3_embeddings.json", {
        "dense_model": EMBED_MODEL, "dense_dim": EMBED_DIM, "sparse_model": SPARSE_MODEL,
        "count": len(records), **timings, "vectors": records,
    })
    print(f"  embedded {len(todo)} new passages, reused {len(chunks) - len(todo)} ({timings})")
    return records
