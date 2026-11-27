"""Steps 5, 8, 9, 10 - Retrieval with permission filters inside the Qdrant query,
hybrid dense + BM25 search, RRF fusion, optional knowledge-graph candidates, then re-ranking with
bge-reranker-v2-m3 and entity coverage.

  hybrid     dense (BGE-M3) and BM25 top-k each, fused with reciprocal rank fusion inside Qdrant
  graph      passages that name the question's entities and the entities that connect them (graph/search.py);
             they join the candidates before re-ranking, so the cross-encoder decides on one list
  rerank     bge-reranker-v2-m3 scores (question, passage) pairs; scores are cached per process
  coverage   a question about two or more entities ("compare Kerala and Goa") gets at least one passage per
             entity in the final list when one exists among the candidates

Outputs: outputs/5_retrieval.json, outputs/6_rerank.json (when save=True)
"""
import re
import uuid

from qdrant_client import models

from .config import CANDIDATES_PER_RETRIEVER, COLLECTION, FINAL_TOP_K, FUSED_TOP_K
from .models import embed_dense, embed_sparse, rerank_scores
from .store import active_collection, client
from .utils import timer, write_json

GRAPH_CANDIDATES = 12
RRF_K = 5                       # final ranking: reciprocal rank fusion of hybrid rank and re-ranker rank


def _fuse() -> bool:
    import os
    return os.environ.get("CL_RERANK_FUSION", "1") != "0"           # graph passages added to the re-ranker's list (beyond the hybrid ones)
_RERANK_CACHE: dict[tuple[str, str], float] = {}


def build_filter(groups: list[str] | None, sources: list[str] | None,
                 headings: list[str] | None = None) -> models.Filter | None:
    must = []
    if headings:   # restrict to chunks under any of these headings (e.g. a set of regions)
        must.append(models.FieldCondition(key="headings", match=models.MatchAny(any=headings)))
    if groups:   # user may only see chunks whose "access" contains one of their groups
        must.append(models.FieldCondition(key="access", match=models.MatchAny(any=groups)))
    if sources:
        must.append(models.FieldCondition(key="source", match=models.MatchAny(any=sources)))
    return models.Filter(must=must) if must else None


def _hit(p, score_key="score") -> dict:
    return {"chunk_id": p.payload["chunk_id"], "source": p.payload["source"], "pages": p.payload.get("pages") or [],
            "headings": p.payload.get("headings") or [],
            score_key: round(p.score, 4) if getattr(p, "score", None) is not None else None,
            "text": p.payload["text"], "access": p.payload.get("access") or []}


def _hits(points, score_key="score"):
    return [_hit(p, score_key) for p in points]


def point_id(chunk_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, chunk_id))


def fetch_chunks(chunk_ids: list[str], groups: list[str] | None, sources: list[str] | None = None) -> list[dict]:
    """Passages by id, re-checking access and source filters (graph candidates come from outside Qdrant)."""
    if not chunk_ids:
        return []
    pts = client().retrieve(active_collection(), ids=[point_id(c) for c in chunk_ids], with_payload=True)
    out = []
    for p in pts:
        if groups and not set(p.payload.get("access") or []) & set(groups):
            continue
        if sources and p.payload["source"] not in sources:
            continue
        out.append(_hit(p, "graph_score"))
    order = {c: i for i, c in enumerate(chunk_ids)}
    return sorted(out, key=lambda h: order.get(h["chunk_id"], 1e9))


def rerank(question: str, hits: list[dict]) -> list[dict]:
    import os
    from .config import RERANK_MODEL
    model = os.environ.get("CL_RERANKER") or RERANK_MODEL
    if model.lower() == "none":
        return hits
    todo = [h for h in hits if (model, question, h["chunk_id"]) not in _RERANK_CACHE]
    if todo:
        for h, s in zip(todo, rerank_scores(question, [h["text"] for h in todo], model)):
            _RERANK_CACHE[(model, question, h["chunk_id"])] = round(float(s), 4)
    for h in hits:
        h["rerank_score"] = _RERANK_CACHE[(model, question, h["chunk_id"])]
    return sorted(hits, key=lambda h: h["rerank_score"], reverse=True)


def _mentions(text: str, name: str) -> bool:
    return re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text, re.I) is not None


def ensure_coverage(ranked: list[dict], entities: list[dict], k: int) -> list[dict]:
    """Keep the top-k, but make sure every question entity that has a candidate passage is represented:
    its best-ranked passage replaces the lowest-ranked passage that is not the only one for another entity."""
    if len(entities) < 2:
        return ranked[:k]
    final = ranked[:k]
    for e in entities:
        names = [e["name"]] + e.get("aliases", [])
        if any(any(_mentions(h["text"], n) for n in names) for h in final):
            continue
        best = next((h for h in ranked[k:] if any(_mentions(h["text"], n) for n in names)), None)
        if best is None:
            continue
        for i in range(len(final) - 1, -1, -1):
            others = [x for x in entities if x is not e]
            needed = any(sum(any(_mentions(h["text"], n) for n in [x["name"]] + x.get("aliases", [])) for h in final) == 1
                         and any(_mentions(final[i]["text"], n) for n in [x["name"]] + x.get("aliases", [])) for x in others)
            if not needed:
                best = dict(best, coverage_for=e["name"])
                final[i] = best
                break
    return final


def retrieve(question: str, groups: list[str] | None = None, sources: list[str] | None = None,
             save: bool = True, headings: list[str] | None = None, top_k: int = FINAL_TOP_K,
             use_graph: bool = True, use_rerank: bool = True, coverage: bool = True) -> dict:
    qc, timings = client(), {}
    groups = groups or ["public"]          # default caller only sees public documents
    flt = build_filter(groups, sources, headings)

    with timer("embed_query", timings):
        qd = embed_dense([question])[0]
        qs = models.SparseVector(**embed_sparse([question], query=True)[0])

    with timer("search", timings):
        # The filter is applied INSIDE each search (pre-filtering), never after.
        fused = qc.query_points(
            active_collection(),
            prefetch=[
                models.Prefetch(query=qd, using="dense", filter=flt, limit=CANDIDATES_PER_RETRIEVER),
                models.Prefetch(query=qs, using="bm25", filter=flt, limit=CANDIDATES_PER_RETRIEVER),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            limit=FUSED_TOP_K, with_payload=True,
        ).points
    fused_hits = _hits(fused, "rrf_score")

    graph_info, entities = None, []
    if use_graph and not headings:
        with timer("graph", timings):
            try:
                from graph import search as GS
                from graph.access import visible
                if GS.ready():
                    graph_info = GS.local(question, groups, visible)
                    entities = graph_info["entities"]
                    have = {h["chunk_id"] for h in fused_hits}
                    extra = [c for c in graph_info["passage_ids"] if c not in have][:GRAPH_CANDIDATES]
                    fused_hits += fetch_chunks(extra, groups, sources)
            except FileNotFoundError:
                graph_info = None

    with timer("rerank", timings):
        ranked = rerank(question, fused_hits) if (use_rerank and fused_hits) else fused_hits
        if use_rerank and fused_hits and _fuse():
            # the re-ranker and the hybrid search each vote by rank (reciprocal rank fusion):
            # either one alone can be fooled; together they are steadier
            first = {h["chunk_id"]: i for i, h in enumerate(fused_hits)}
            score = {h["chunk_id"]: 1 / (RRF_K + i) + 1 / (RRF_K + first[h["chunk_id"]]) for i, h in enumerate(ranked)}
            ranked = sorted(ranked, key=lambda h: -score[h["chunk_id"]])
    if coverage and entities:
        ents = [{"name": e["name"], "aliases": []} for e in entities]
        final = ensure_coverage(ranked, ents, top_k)
    else:
        final = ranked[:top_k]

    result = {"question": question, "filter": {"groups": groups, "sources": sources}, "timings_s": timings,
              "rrf_fused": fused_hits}
    if save:
        write_json("5_retrieval.json", result)
        write_json("6_rerank.json", {"question": question, "rerank_seconds": timings.get("rerank"),
                                     "top_k": top_k, "passages": final})
    stage_ids = {"rrf": [h["chunk_id"] for h in fused_hits[:FUSED_TOP_K]], "candidates": [h["chunk_id"] for h in fused_hits],
                 "reranked": [h["chunk_id"] for h in ranked], "final": [h["chunk_id"] for h in final]}
    return {"passages": final, "timings_s": timings, "stage_ids": stage_ids, "graph": graph_info}


_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")


def highlights(question: str, text: str, n: int = 2) -> list[str]:
    """The sentences of a passage that share the most (rare-ish) words with the question: an extractive
    pointer for the agent and the Playground, not an answer."""
    q = {w for w in re.findall(r"\w+", question.lower()) if len(w) > 3}
    sents = [s.strip() for s in _SENT.split(text) if len(s.strip()) > 20]
    scored = sorted(((len(q & set(re.findall(r"\w+", s.lower()))), i, s) for i, s in enumerate(sents)), reverse=True)
    best = sorted((i, s) for sc, i, s in scored[:n] if sc > 0)
    return [s[:400] for _, s in best]
