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
from .store import client
from .utils import timer, write_json

GRAPH_CANDIDATES = 12
RRF_K = 5                       # final ranking: reciprocal rank fusion of hybrid rank and re-ranker rank

# Evidence quality. The re-ranker returns a logit; sigmoid(logit) is its probability that the passage answers
# the question. Passages below MIN_RELEVANCE are dropped so an agent is never handed unrelated text as
# "evidence" (the main cause of confident wrong answers); the rest are labelled so the agent can weigh them.
import math  # noqa: E402
import os  # noqa: E402

MIN_RELEVANCE = float(os.environ.get("CL_MIN_RELEVANCE", "0.02"))
STRONG, PARTIAL = 0.5, 0.15                 # relevance >= STRONG: answers it; >= PARTIAL: related/partial
DUPLICATE_JACCARD = 0.8                     # 5-word-shingle overlap above which two passages are the same text


def relevance(score: float | None) -> float | None:
    if score is None:
        return None
    return round(1 / (1 + math.exp(-max(-30.0, min(30.0, float(score))))), 4)


def label(rel: float | None) -> str:
    if rel is None:
        return "unscored"
    return "strong" if rel >= STRONG else "partial" if rel >= PARTIAL else "weak"


def evidence_strength(passages: list[dict]) -> str:
    """strong | partial | weak | none | unscored - the best passage decides."""
    if not passages:
        return "none"
    rels = [p.get("relevance") for p in passages if p.get("relevance") is not None]
    return label(max(rels)) if rels else "unscored"


def _shingles(text: str, n: int = 5) -> set:
    w = re.findall(r"\w+", text.lower())
    return {" ".join(w[i:i + n]) for i in range(max(1, len(w) - n + 1))}


def drop_duplicates(hits: list[dict]) -> list[dict]:
    """Keep the first (best-ranked) of near-identical passages, e.g. the same paragraph in two versions of a file."""
    kept, sigs = [], []
    for h in hits:
        s = _shingles(h["text"])
        if any(len(s & t) / max(1, len(s | t)) >= DUPLICATE_JACCARD for t in sigs):
            continue
        kept.append(h)
        sigs.append(s)
    return kept


def screen(hits: list[dict]) -> list[dict]:
    """Attach relevance/label, drop near-duplicates and passages the re-ranker judges unrelated."""
    out = []
    for h in drop_duplicates(hits):
        rel = relevance(h.get("rerank_score"))
        if rel is not None and rel < MIN_RELEVANCE:
            continue
        out.append(dict(h, relevance=rel, evidence=label(rel)))
    return out


def _fuse() -> bool:
    import os
    return os.environ.get("CL_RERANK_FUSION", "1") != "0"           # graph passages added to the re-ranker's list (beyond the hybrid ones)
_RERANK_CACHE: dict[tuple[str, str], float] = {}


def build_filter(groups: list[str] | None, sources: list[str] | None,
                 headings: list[str] | None = None) -> models.Filter | None:
    from .wspath import tenant_key
    must = []
    if tenant_key():   # the app: only this workspace's passages (all workspaces share one collection)
        must.append(models.FieldCondition(key="tenant", match=models.MatchValue(value=tenant_key())))
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
            "text": p.payload.get("text"), "access": p.payload.get("access") or []}


def _hits(points, score_key="score"):
    return [_hit(p, score_key) for p in points]


def fill_texts(hits: list[dict]) -> list[dict]:
    """Passage texts from the passages repository, in one query (the search index keeps only ids and filters).
    A hit whose passage no longer exists is dropped."""
    need = [h["chunk_id"] for h in hits if h.get("text") is None]
    if not need:
        return hits
    from . import passages
    t = passages.texts(need)
    for h in hits:
        if h.get("text") is None:
            h["text"] = t.get(h["chunk_id"])
    return [h for h in hits if h.get("text") is not None]


def point_id(chunk_id: str) -> str:
    from .wspath import tenant_key
    t = tenant_key()
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{t}/{chunk_id}" if t else chunk_id))


def fetch_chunks(chunk_ids: list[str], groups: list[str] | None, sources: list[str] | None = None) -> list[dict]:
    """Passages by id, re-checking access and source filters (graph candidates come from outside Qdrant)."""
    if not chunk_ids:
        return []
    pts = client().retrieve(COLLECTION, ids=[point_id(c) for c in chunk_ids], with_payload=True)
    from .wspath import tenant_key
    out = []
    for p in pts:
        if (p.payload.get("tenant") or "") != tenant_key():
            continue
        if groups and not set(p.payload.get("access") or []) & set(groups):
            continue
        if sources and p.payload["source"] not in sources:
            continue
        out.append(_hit(p, "graph_score"))
    out = fill_texts(out)
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
            COLLECTION,
            prefetch=[
                models.Prefetch(query=qd, using="dense", filter=flt, limit=CANDIDATES_PER_RETRIEVER),
                models.Prefetch(query=qs, using="bm25", filter=flt, limit=CANDIDATES_PER_RETRIEVER),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            limit=FUSED_TOP_K, with_payload=True,
        ).points
    with timer("texts", timings):
        fused_hits = fill_texts(_hits(fused, "rrf_score"))

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
        if use_rerank:
            ranked = screen(ranked)
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
    return {"passages": final, "timings_s": timings, "stage_ids": stage_ids, "graph": graph_info,
            "evidence": evidence_strength(final)}


# ---------- surrounding context ----------
from .wspath import PerFile  # noqa: E402

_CHUNK_INDEX = PerFile()                    # per workspace


def _chunk_index() -> dict:
    """chunk_id -> (source, position) from the passages repository, reloaded when the passages change."""
    from . import passages
    from .config import OUT_DIR
    mtime = passages.stamp()
    if mtime is None:
        return {"by_id": {}, "by_source": {}}
    e = _CHUNK_INDEX.entry(OUT_DIR / "2_chunks.json")
    if e["mtime"] != mtime:
        by_id, by_source = {}, {}
        for c in passages.all():
            lst = by_source.setdefault(c["source"], [])
            by_id[c["chunk_id"]] = (c["source"], len(lst))
            lst.append(c)
        e.update(mtime=mtime, by_id=by_id, by_source=by_source)
    return e


def neighbours(chunk_id: str, groups: list[str] | None, max_chars: int = 800) -> dict:
    """The passages just before and after this one when they belong to the same section and the caller may
    see them: a fragment cut by the chunker gets its missing half back (e.g. a list split over two passages)."""
    idx = _chunk_index()
    loc = idx["by_id"].get(chunk_id)
    if not loc:
        return {}
    source, pos = loc
    chunks = idx["by_source"][source]
    me = chunks[pos]
    out = {}
    for key, j in (("before", pos - 1), ("after", pos + 1)):
        if not 0 <= j < len(chunks):
            continue
        c = chunks[j]
        if (c.get("headings") or []) != (me.get("headings") or []):
            continue                                            # a different section: not this passage's context
        if groups and not set(c.get("access") or []) & set(groups):
            continue
        t = c["text"]
        out[key] = {"id": c["chunk_id"], "text": (t[-max_chars:] if key == "before" else t[:max_chars])}
    return out


_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")


def highlights(question: str, text: str, n: int = 2) -> list[str]:
    """The sentences of a passage that share the most (rare-ish) words with the question: an extractive
    pointer for the agent and the Playground, not an answer."""
    q = {w for w in re.findall(r"\w+", question.lower()) if len(w) > 3}
    sents = [s.strip() for s in _SENT.split(text) if len(s.strip()) > 20]
    scored = sorted(((len(q & set(re.findall(r"\w+", s.lower()))), i, s) for i, s in enumerate(sents)), reverse=True)
    best = sorted((i, s) for sc, i, s in scored[:n] if sc > 0)
    return [s[:400] for _, s in best]
