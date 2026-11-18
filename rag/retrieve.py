"""Steps 5, 8, 9, 10 - Retrieval with permission filters inside the Qdrant query,
hybrid dense + BM25 search, RRF fusion, then re-ranking with bge-reranker-v2-m3.

Outputs: outputs/5_retrieval.json, outputs/6_rerank.json
"""
from qdrant_client import models

from .config import CANDIDATES_PER_RETRIEVER, COLLECTION, FINAL_TOP_K, FUSED_TOP_K
from .models import embed_dense, embed_sparse, rerank_scores
from .store import client
from .utils import timer, write_json


def build_filter(groups: list[str] | None, sources: list[str] | None) -> models.Filter | None:
    must = []
    if groups:   # user may only see chunks whose "access" contains one of their groups
        must.append(models.FieldCondition(key="access", match=models.MatchAny(any=groups)))
    if sources:
        must.append(models.FieldCondition(key="source", match=models.MatchAny(any=sources)))
    return models.Filter(must=must) if must else None


def _hits(points, score_key="score"):
    return [{"chunk_id": p.payload["chunk_id"], "source": p.payload["source"], "pages": p.payload["pages"],
             "headings": p.payload["headings"], score_key: round(p.score, 4), "text": p.payload["text"]}
            for p in points]


def retrieve(question: str, groups: list[str] | None = None, sources: list[str] | None = None,
             save: bool = True) -> dict:
    qc, timings = client(), {}
    groups = groups or ["public"]          # default user only sees public documents
    flt = build_filter(groups, sources)

    with timer("embed_query", timings):
        qd = embed_dense([question])[0]
        qs = models.SparseVector(**embed_sparse([question], query=True)[0])

    with timer("search", timings):
        # The filter is applied INSIDE each search (pre-filtering), never after.
        dense = qc.query_points(COLLECTION, query=qd, using="dense", query_filter=flt,
                                limit=CANDIDATES_PER_RETRIEVER, with_payload=True).points
        sparse = qc.query_points(COLLECTION, query=qs, using="bm25", query_filter=flt,
                                 limit=CANDIDATES_PER_RETRIEVER, with_payload=True).points
        fused = qc.query_points(
            COLLECTION,
            prefetch=[
                models.Prefetch(query=qd, using="dense", filter=flt, limit=CANDIDATES_PER_RETRIEVER),
                models.Prefetch(query=qs, using="bm25", filter=flt, limit=CANDIDATES_PER_RETRIEVER),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            limit=FUSED_TOP_K, with_payload=True,
        ).points

    fused_hits = _hits(fused, "rrf_score")
    with timer("rerank", timings):
        if fused_hits:
            scores = rerank_scores(question, [h["text"] for h in fused_hits])
            for h, s in zip(fused_hits, scores):
                h["rerank_score"] = round(float(s), 4)
        reranked = sorted(fused_hits, key=lambda h: h.get("rerank_score", 0), reverse=True)

    result = {
        "question": question, "filter": {"groups": groups, "sources": sources}, "timings_s": timings,
        "dense_top": _hits(dense)[:10], "bm25_top": _hits(sparse)[:10], "rrf_fused": fused_hits,
    }
    final = reranked[:FINAL_TOP_K]
    if save:
        write_json("5_retrieval.json", result)
        write_json("6_rerank.json", {"question": question, "rerank_seconds": timings["rerank"],
                                     "top_k": FINAL_TOP_K, "passages": final})
    return {"passages": final, "timings_s": timings}
