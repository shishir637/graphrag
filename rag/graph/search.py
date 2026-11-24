"""g6 - Answering with the graph.

  local  : find the question's entities -> their neighbourhood in AGE -> the chunks those entities
           and relationships came from -> answer from those chunks + a short "graph facts" passage
  global : community reports -> the LLM picks the relevant points from each (map) -> answer from
           the best points (reduce). For "what are the main themes / overall" questions.
  hybrid : the existing dense+BM25 candidates and the graph's chunks are fused with RRF, re-ranked
           by the same re-ranker, and the top passages go to the same generate() as plain RAG.
           The graph can only ADD candidates; it never replaces source text with summaries.
  auto   : global for overview questions, hybrid for everything else (a rule, no LLM call).

Access: chunks, entities, relationships and reports are filtered by the asking user's groups
before anything reaches the LLM (see summarize.py for the rule on summaries).

Output: outputs/g6_graph_answer.json
"""
import json
import re
import time

from pydantic import BaseModel
from rapidfuzz import fuzz

from ..config import (ENTITY_LINK_MIN, FINAL_TOP_K, FUSED_TOP_K, GLOBAL_TOP_POINTS, GRAPH_CHUNKS, GRAPH_HOPS,
                      OUT_DIR)
from ..generate import generate
from ..models import embed_dense, rerank_scores
from ..utils import read_json, write_json
from . import llm
from .resolve import normalise
from .store import open_store

RRF_K = 60
_GLOBAL = re.compile(r"\b(main|key|overall|common|recurring)\s+(themes?|topics?|points?|ideas?|patterns?)\b|"
                     r"\b(summari[sz]e|overview of|across (all|the) (documents?|files?))\b", re.I)


class _Cache:
    graph = chunks = vectors = reports = None


def _load():
    if _Cache.graph is None:
        _Cache.graph = read_json("g5_graph.json")["graph"]
        _Cache.chunks = {c["chunk_id"]: c for c in read_json("2_chunks.json")}
        _Cache.reports = read_json("g4_summaries.json")["reports"]
        vec_path = OUT_DIR / "g5_entity_vectors.json"
        if vec_path.exists():
            _Cache.vectors = json.loads(vec_path.read_text(encoding="utf-8"))
        else:
            ents = _Cache.graph["entities"]
            vecs = embed_dense([f"{e['name']}: {e['summary']}" for e in ents]) if ents else []
            _Cache.vectors = {e["id"]: v for e, v in zip(ents, vecs)}
            vec_path.write_text(json.dumps(_Cache.vectors), encoding="utf-8")
    return _Cache


def reset_cache():
    _Cache.graph = _Cache.chunks = _Cache.vectors = _Cache.reports = None


def _can_see(chunk_id: str, groups: list[str], sources: list[str] | None) -> bool:
    c = _Cache.chunks.get(chunk_id)
    return bool(c) and bool(set(c["access"]) & set(groups)) and (not sources or c["source"] in sources)


def _entity_text(e: dict, groups, sources) -> str:
    """Merged summary only when every chunk behind it is visible, else the visible raw descriptions."""
    if all(_can_see(ch, groups, sources) for ch in e["chunk_ids"]):
        return e["summary"]
    full = next(x for x in _Cache.graph["entities"] if x["id"] == e["id"])
    return " ".join(d["text"] for d in full["descriptions"] if _can_see(d["chunk_id"], groups, sources))


def link_entities(question: str, groups, sources) -> list[dict]:
    """Question -> graph entities. Name/alias match first (exact, cheap), then embedding similarity."""
    c = _load()
    qn = normalise(question)
    visible = [e for e in c.graph["entities"] if any(_can_see(ch, groups, sources) for ch in e["chunk_ids"])]
    found = {}
    for e in visible:
        for n in [e["name"], *e["aliases"]]:
            nn = normalise(n)
            if len(nn) >= 3 and (re.search(rf"\b{re.escape(nn)}\b", qn) or
                                 (len(nn) >= 5 and fuzz.partial_ratio(nn, qn) >= 90)):
                found[e["id"]] = {"id": e["id"], "name": e["name"], "type": e["type"], "how": f"name match '{n}'",
                                  "score": 1.0}
                break
    if visible:
        qv = embed_dense([question])[0]
        sims = sorted(((sum(a * b for a, b in zip(qv, c.vectors[e["id"]])), e) for e in visible if e["id"] in c.vectors),
                      key=lambda x: -x[0])[:5]
        for s, e in sims:
            if s >= ENTITY_LINK_MIN and e["id"] not in found:
                found[e["id"]] = {"id": e["id"], "name": e["name"], "type": e["type"], "how": "embedding",
                                  "score": round(s, 3)}
    return sorted(found.values(), key=lambda x: -x["score"])[:8]


def graph_context(question: str, groups=None, sources=None) -> dict:
    """Local search without the LLM: entities, neighbourhood, ranked chunks, and a facts passage."""
    groups = groups or ["public"]
    c = _load()
    t0 = time.perf_counter()
    seeds = link_entities(question, groups, sources)
    t1 = time.perf_counter()

    store = open_store()
    try:
        frontier, rels, ent_ids = [s["id"] for s in seeds], [], {s["id"] for s in seeds}
        for _ in range(GRAPH_HOPS):
            if not frontier:
                break
            hop = [r for r in store.neighbourhood(frontier) if any(_can_see(ch, groups, sources) for ch in r["chunk_ids"])]
            rels += hop
            frontier = [r["other"] for r in hop if r["other"] not in ent_ids]
            ent_ids |= set(frontier)
        ents = {e["id"]: e for e in store.entities(sorted(ent_ids))}
    finally:
        store.close()
    t2 = time.perf_counter()

    # chunk scores: where the question's entities and the relationships around them were stated
    seed_ids = {s["id"] for s in seeds}
    score = {}
    for i, e in ents.items():
        for ch in e["chunk_ids"]:
            score[ch] = score.get(ch, 0) + (2.0 if i in seed_ids else 0.5)
    rels.sort(key=lambda r: -(r["weight"] * r["strength"]))
    for r in rels:
        for ch in r["chunk_ids"]:
            score[ch] = score.get(ch, 0) + r["weight"] * r["strength"] / 10
    ranked = [ch for ch, _ in sorted(score.items(), key=lambda x: -x[1]) if _can_see(ch, groups, sources)][:GRAPH_CHUNKS]

    name = {i: e["name"] for i, e in ents.items()}
    fact_lines = [f"{e['name']} ({e['type']}): {_entity_text(e, groups, sources)}"
                  for i, e in ents.items() if i in seed_ids]
    fact_lines += [f"{name.get(r['source'], r['source'])} -[{r['type']}]-> {name.get(r['target'], r['target'])}"
                   + (f": {r['description']}" if _can_see(r["chunk_ids"][0], groups, sources) else "")
                   for r in rels[:25]]
    facts = {"chunk_id": "kg_facts", "source": "knowledge graph", "pages": [], "headings": ["Graph facts"],
             "text": "\n".join(fact_lines)} if fact_lines else None

    return {"linked_entities": seeds,
            "neighbourhood": {"entities": [{"id": i, "name": e["name"], "type": e["type"], "community": e["community"]}
                                           for i, e in ents.items()],
                              "relations": [{k: r[k] for k in ("source", "target", "type", "weight", "strength")}
                                            for r in rels]},
            "chunk_ranking": [{"chunk_id": ch, "graph_score": round(score[ch], 3)} for ch in ranked],
            "facts_passage": facts,
            "timings_s": {"entity_linking": round(t1 - t0, 3), "graph_traversal": round(t2 - t1, 3)}}


def _passage(chunk_id: str) -> dict:
    c = _Cache.chunks[chunk_id]
    return {"chunk_id": chunk_id, "source": c["source"], "pages": c["pages"], "headings": c["headings"], "text": c["text"]}


def _rerank(question, passages):
    if passages:
        for p, s in zip(passages, rerank_scores(question, [p["text"] for p in passages])):
            p["rerank_score"] = round(float(s), 4)
    return sorted(passages, key=lambda p: -p.get("rerank_score", 0))


# ---- global search ----------------------------------------------------------------------------
class _Point(BaseModel):
    community: str
    point: str
    score: int


class _MapOut(BaseModel):
    points: list[_Point]


_MAP_SYS = ("You are given community reports from a knowledge graph and a question. From each report, list "
            "the points that help answer the question, each with a relevance score 0-100. Use only the "
            "reports. Skip reports that do not help. 'community' must be the report id shown.")


def global_context(question: str, groups=None, sources=None, level: int = 0) -> dict:
    groups = groups or ["public"]
    _load()
    reps = [r for r in _Cache.reports if r["level"] == level and
            all(_can_see(ch, groups, sources) for ch in r["from_chunks"])]
    hidden = sum(1 for r in _Cache.reports if r["level"] == level) - len(reps)
    reps.sort(key=lambda r: -r["importance"])
    points = []
    for i in range(0, len(reps), 6):
        batch = reps[i:i + 6]
        text = "\n\n".join(f"[report {r['community']}] {r['title']}\n{r['summary']}\n" +
                           "\n".join(f"- {f['summary']}: {f['explanation']}" for f in r["findings"]) for r in batch)
        out = llm.structured(_MAP_SYS, f"{text}\n\nQuestion: {question}", _MapOut)
        valid = {r["community"] for r in batch}
        points += [p.model_dump() for p in out.points if p.community in valid and p.score > 0]
    points.sort(key=lambda p: -p["score"])
    points = points[:GLOBAL_TOP_POINTS]
    by_comm = {}
    for p in points:
        by_comm.setdefault(p["community"], []).append(p["point"])
    titles = {r["community"]: r["title"] for r in reps}
    passages = [{"chunk_id": f"report_{c}", "source": "community report", "pages": [], "headings": [titles[c]],
                 "text": "\n".join(f"- {x}" for x in pts)} for c, pts in by_comm.items()]
    return {"reports_considered": len(reps), "reports_hidden_by_access": hidden, "points": points,
            "passages": passages}


# ---- entry point --------------------------------------------------------------------------------
def route(question: str) -> str:
    return "global" if _GLOBAL.search(question) else "hybrid"


def ask_graph(question: str, mode: str = "auto", groups=None, sources=None, save: bool = True,
              answer: bool = True) -> dict:
    """answer=False stops before the final LLM call (retrieval only), like the UI's plain-RAG mode."""
    from ..retrieve import retrieve
    groups = groups or ["public"]
    mode = route(question) if mode == "auto" else mode
    calls0, in0, out0 = llm.usage["calls"], llm.usage["input_tokens"], llm.usage["output_tokens"]
    t0 = time.perf_counter()
    trace, timings = {"mode": mode}, {}

    if mode == "global":
        g = global_context(question, groups, sources)
        trace["global"] = {k: g[k] for k in ("reports_considered", "reports_hidden_by_access", "points")}
        passages = g["passages"]
        if not passages:                                       # nothing usable at report level
            mode = trace["mode"] = "hybrid"
            trace["note"] = "no community report was relevant or visible to this user; fell back to hybrid"
    if mode != "global":
        g = graph_context(question, groups, sources)
        timings.update(g["timings_s"])
        trace["local"] = {k: g[k] for k in ("linked_entities", "neighbourhood", "chunk_ranking")}
        graph_ids = [x["chunk_id"] for x in g["chunk_ranking"]]
        if mode == "local":
            passages = _rerank(question, [_passage(ch) for ch in graph_ids])[:FINAL_TOP_K]
        else:                                                  # hybrid
            t = time.perf_counter()
            ret = retrieve(question, groups, sources, save=False)
            timings["text_retrieval"] = round(time.perf_counter() - t, 3)
            rrf = {}
            for rank, h in enumerate(ret["fused"]):
                rrf[h["chunk_id"]] = rrf.get(h["chunk_id"], 0) + 1 / (RRF_K + rank + 1)
            for rank, ch in enumerate(graph_ids):
                rrf[ch] = rrf.get(ch, 0) + 1 / (RRF_K + rank + 1)
            fused = sorted(rrf, key=lambda ch: -rrf[ch])[:FUSED_TOP_K]
            text_ids, graph_set = {h["chunk_id"] for h in ret["fused"]}, set(graph_ids)
            trace["fusion"] = [{"chunk_id": ch, "rrf": round(rrf[ch], 5),
                                "from": "both" if ch in text_ids and ch in graph_set else
                                        ("text" if ch in text_ids else "graph")} for ch in fused]
            t = time.perf_counter()
            passages = _rerank(question, [_passage(ch) for ch in fused])[:FINAL_TOP_K]
            timings["rerank"] = round(time.perf_counter() - t, 3)
            trace["graph_only_in_final"] = [p["chunk_id"] for p in passages
                                            if p["chunk_id"] in graph_set and p["chunk_id"] not in text_ids]
        if g["facts_passage"]:
            passages = passages + [g["facts_passage"]]

    t1 = time.perf_counter()
    gen = generate(question, passages) if answer else {"found": None, "answer": None, "citations": [],
                                                         "model": None, "usage": {"input_tokens": 0, "output_tokens": 0}}
    t2 = time.perf_counter()
    record = {
        "question": question, "mode": mode, **gen,
        "contexts": [{"chunk_id": p["chunk_id"], "source": p["source"], "pages": p["pages"],
                      "rerank_score": p.get("rerank_score"), "text": p["text"]} for p in passages],
        "graph_trace": trace,
        "graph_llm": {"calls": llm.usage["calls"] - calls0, "input_tokens": llm.usage["input_tokens"] - in0,
                      "output_tokens": llm.usage["output_tokens"] - out0},
        "timings_s": {**timings, "retrieval_total": round(t1 - t0, 3), "generation": round(t2 - t1, 3),
                      "total": round(t2 - t0, 3)},
    }
    if save:
        write_json("g6_graph_answer.json", record)
    return record
