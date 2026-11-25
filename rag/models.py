"""Lazily loaded models shared by indexing and querying (loaded once per process).

Dense:  BGE-M3 via Hugging Face transformers (CLS pooling + L2 norm = the model's official dense output).
Sparse: BM25 via fastembed (IDF applied by Qdrant).
Rerank: bge-reranker-v2-m3 via fastembed (ONNX).
(sentence-transformers is avoided because Windows Application Control blocks a pyarrow DLL it pulls in.)
"""
from functools import lru_cache

import os

from .config import EMBED_MODEL, RERANK_MAX_CHARS, RERANK_MODEL, SPARSE_MODEL


@lru_cache
def dense_model():
    import torch
    from transformers import AutoModel, AutoTokenizer
    tok = AutoTokenizer.from_pretrained(EMBED_MODEL)
    model = AutoModel.from_pretrained(EMBED_MODEL).eval()
    device = "cuda" if torch.cuda.is_available() else "cpu"
    return tok, model.to(device), device


@lru_cache
def sparse_model():
    from fastembed import SparseTextEmbedding
    return SparseTextEmbedding(SPARSE_MODEL)


@lru_cache
def reranker(name: str = RERANK_MODEL):
    from fastembed.rerank.cross_encoder import TextCrossEncoder
    return TextCrossEncoder(name, threads=os.cpu_count())


def embed_dense(texts: list[str], batch_size: int = 8) -> list[list[float]]:
    """Batches are formed from texts of similar length (less padding = less wasted compute);
    results are returned in the original order. Prints progress for large inputs."""
    import time

    import torch
    tok, model, device = dense_model()
    order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
    out: list = [None] * len(texts)
    t0, report_every = time.perf_counter(), max(1, len(texts) // 20)
    with torch.inference_mode():
        for i in range(0, len(order), batch_size):
            idx = order[i:i + batch_size]
            batch = tok([texts[j] for j in idx], padding=True, truncation=True, max_length=8192,
                        return_tensors="pt").to(device)
            cls = model(**batch).last_hidden_state[:, 0]
            for j, v in zip(idx, torch.nn.functional.normalize(cls, dim=-1).cpu().tolist()):
                out[j] = v
            done = i + len(idx)
            if len(texts) > 200 and (done // report_every != (done - len(idx)) // report_every or done == len(texts)):
                print(f"    dense {done}/{len(texts)} ({time.perf_counter() - t0:.0f}s)", flush=True)
    return out


def embed_sparse(texts: list[str], query: bool = False) -> list[dict]:
    m = sparse_model()
    gen = m.query_embed(texts) if query else m.embed(texts)
    return [{"indices": e.indices.tolist(), "values": e.values.tolist()} for e in gen]


def rerank_scores(question: str, texts: list[str], model: str | None = None) -> list[float]:
    name = model or os.environ.get("CL_RERANKER") or RERANK_MODEL
    return [float(s) for s in reranker(name).rerank(question, [t[:RERANK_MAX_CHARS] for t in texts], batch_size=4)]
