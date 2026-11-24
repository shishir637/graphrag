"""Answering with the knowledge graph.

  route   : the small model classifies the question -> vector RAG (single fact), local graph search
            (connections / multi-hop), global search (big-picture), or the semantic layer (sales numbers)
  local   : 1. link the question to entities (exact name match + BGE-M3 search in Qdrant 'graph_entities')
            2. walk the graph in AGE: 1-hop edges + 2-hop paths that connect the question's entities
            3. collect the source chunks behind those entities/edges, rerank, keep the best
            4. Claude answers from graph facts + source chunks, citing chunk ids
  global  : 1. pick the communities closest to the question (Qdrant 'graph_communities')
            2. summarise each community the first time it is needed, cache it in Postgres (lazy)
            3. map: the small model writes a partial answer + usefulness score per community
            4. reduce: Claude combines the useful partial answers
"""
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

from pydantic import BaseModel, Field

from rag.generate import SYSTEM, Answer, format_passages
from rag.llm import claude

from . import store
from .config import (ANSWER_MODEL, COMMUNITY_COLLECTION, ENTITY_COLLECTION, FINAL_CHUNKS, GLOBAL_COMMUNITIES,
                     MAX_CHUNKS_RERANK, MAX_RELATIONS, SEED_ENTITIES, SMALL_MODEL)

_names = None


def _usage(resp) -> dict:
    return {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}


def _add_usage(total: dict, u: dict):
    for k, v in u.items():
        total[k] = total.get(k, 0) + v


# ---------- entity linking ----------
def _entity_names():
    global _names
    if _names is None:
        rows = store.conn().execute("SELECT id, name, type, aliases, degree FROM public.kg_entities").fetchall()
        _names = [(i, n, t, a or [], d or 0) for i, n, t, a, d in rows]
    return _names


def link_entities(question: str, qv) -> list[dict]:
    """Exact mentions of entity names/aliases first (longest first), then nearest entities by meaning."""
    from rag.store import client
    found, q = {}, question.lower()
    for eid, name, typ, aliases, deg in sorted(_entity_names(), key=lambda x: -len(x[1])):
        for n in [name, *aliases]:
            if len(n) >= 4 and re.search(rf"(?<!\w){re.escape(n.lower())}(?!\w)", q):
                if not any(n.lower() in f["name"].lower() for f in found.values()):   # skip parts of longer matches
                    found[eid] = {"id": eid, "name": name, "type": typ, "how": "name", "degree": deg}
                break
    hits = client().query_points(ENTITY_COLLECTION, query=qv, using="dense", limit=SEED_ENTITIES * 2,
                                 with_payload=True).points
    for h in hits:
        if len(found) >= SEED_ENTITIES:
            break
        found.setdefault(h.payload["id"], {"id": h.payload["id"], "name": h.payload["name"],
                                           "type": h.payload["type"], "how": f"vector {h.score:.2f}",
                                           "degree": h.payload.get("degree", 0)})
    return list(found.values())[:SEED_ENTITIES + 2]


# ---------- local search ----------
# ---------- access groups ----------
_ACCESS = {"mtime": None, "map": {}, "groups": set()}


def chunk_access() -> dict:
    """chunk_id -> access groups, from the document pipeline's chunks file (reloaded when it changes)."""
    from rag.config import OUT_DIR as RAG_OUT
    p = RAG_OUT / "2_chunks.json"
    if not p.exists():
        return {}
    m = p.stat().st_mtime
    if _ACCESS["mtime"] != m:
        chunks = json.loads(p.read_text(encoding="utf-8"))
        _ACCESS.update(mtime=m, map={c["chunk_id"]: c.get("access", ["public"]) for c in chunks},
                       groups={g for c in chunks for g in c.get("access", ["public"])})
    return _ACCESS["map"]


def visible(chunk_id: str, groups: list[str] | None) -> bool:
    """None = no restriction (CLI / owner). Unknown chunks are hidden from restricted callers."""
    if groups is None:
        return True
    return bool(set(chunk_access().get(chunk_id, [])) & set(groups))


def sees_everything(groups: list[str] | None) -> bool:
    chunk_access()
    return groups is None or _ACCESS["groups"] <= set(groups)


def _chunks(ids: list[str]) -> dict:
    rows = store.conn().execute("SELECT chunk_id, source, headings, text FROM public.kg_chunks WHERE chunk_id = ANY(%s)",
                                (ids,)).fetchall()
    return {r[0]: {"chunk_id": r[0], "source": r[1], "headings": r[2], "pages": [], "text": r[3]} for r in rows}


def local_search(question: str, groups: list[str] | None = None) -> dict:
    from rag.models import embed_dense, rerank_scores
    t0 = time.perf_counter()
    qv = embed_dense([question])[0]
    seeds = link_entities(question, qv)
    if groups is not None and seeds:            # only entities the caller can see a source passage for
        rows = store.conn().execute("SELECT entity_id, chunk_id FROM public.kg_entity_chunks WHERE entity_id = ANY(%s)",
                                    ([s["id"] for s in seeds],)).fetchall()
        ok = {eid for eid, cid in rows if visible(cid, groups)}
        seeds = [s for s in seeds if s["id"] in ok]
    t1 = time.perf_counter()
    edges = store.neighbourhood([s["id"] for s in seeds]) if seeds else []
    if groups is not None:                      # a relation counts only if a passage behind it is visible
        edges = [dict(e, chunk_ids=[c for c in e["chunk_ids"] if visible(c, groups)]) for e in edges]
        edges = [e for e in edges if e["chunk_ids"]]
    seen, ranked = set(), []
    for e in sorted(edges, key=lambda e: (e["hop"] != 2, -float(e["weight"] or 0))):
        k = (e["source"], e["type"], e["target"])
        if k not in seen:
            seen.add(k)
            ranked.append(e)
    ranked = ranked[:MAX_RELATIONS]
    t2 = time.perf_counter()

    # source chunks: behind the chosen edges (2-hop paths count double) and behind the seed entities
    votes: dict[str, float] = {}
    for e in ranked:
        for c in e["chunk_ids"]:
            votes[c] = votes.get(c, 0) + (2 if e["hop"] == 2 else 1)
    if seeds:
        for eid, cid in store.conn().execute(
                "SELECT entity_id, chunk_id FROM public.kg_entity_chunks WHERE entity_id = ANY(%s)",
                ([s["id"] for s in seeds],)).fetchall():
            votes[cid] = votes.get(cid, 0) + 0.5
    votes = {c: v for c, v in votes.items() if visible(c, groups)}
    cand_ids = [c for c, _ in sorted(votes.items(), key=lambda kv: -kv[1])[:MAX_CHUNKS_RERANK * 3]]
    chunks = _chunks(cand_ids)
    cands = [chunks[c] for c in cand_ids if c in chunks]
    # cheap pre-filter by vote, then the cross-encoder on the best few
    cands = cands[:MAX_CHUNKS_RERANK]
    if cands:
        for c, s in zip(cands, rerank_scores(question, [c["text"] for c in cands])):
            c["rerank_score"] = round(s, 4)
    passages = sorted(cands, key=lambda c: -c.get("rerank_score", -99))[:FINAL_CHUNKS]
    t3 = time.perf_counter()

    facts = "\n".join(f"- {e['source']} -[{e['type']}]-> {e['target']}: {e['description'] or ''}"
                      + (f" (path via {e['via']})" if e.get("via") else "") for e in ranked)
    ents = "\n".join(f"- {s['name']} ({s['type']})" for s in seeds)
    user = (f"<graph_entities_in_question>\n{ents}\n</graph_entities_in_question>\n"
            f"<graph_facts>\n{facts or '(none)'}\n</graph_facts>\n"
            f"<passages>\n{format_passages(passages)}\n</passages>\n\nQuestion: {question}")
    system = SYSTEM + ("\n- graph_facts are relations extracted from the passages; use them to connect facts across "
                       "passages (multi-hop), but cite only passage ids, and prefer the passage text if they disagree.")
    resp = claude().messages.parse(model=ANSWER_MODEL, max_tokens=8000, system=system,
                                   messages=[{"role": "user", "content": user}], output_format=Answer)
    parsed = resp.parsed_output or Answer(found=False, answer="(no answer)", citations=[])
    valid = {p["chunk_id"] for p in passages}
    t4 = time.perf_counter()
    return {
        "mode": "local", "question": question, **parsed.model_dump(),
        "citations": [c for c in parsed.citations if c in valid],
        "contexts": [{"chunk_id": p["chunk_id"], "source": p["source"], "pages": [], "headings": p["headings"],
                      "rerank_score": p.get("rerank_score"), "text": p["text"]} for p in passages],
        "graph": {"seeds": seeds, "relations": [{k: e[k] for k in ("source", "type", "target", "hop")} for e in ranked],
                  "candidate_chunks": len(votes)},
        "usage": _usage(resp),
        "timings_s": {"link": round(t1 - t0, 3), "graph_walk": round(t2 - t1, 3), "rerank": round(t3 - t2, 3),
                      "generation": round(t4 - t3, 3), "total": round(t4 - t0, 3)},
    }


# ---------- global search (lazy community summaries) ----------
class _Summary(BaseModel):
    title: str
    summary: str = Field(description="what this group of entities is about and its key facts, under 200 words")


class _Partial(BaseModel):
    score: int = Field(description="0-100: how useful this community is for answering the question")
    partial_answer: str = Field(description="what this community contributes to the answer; empty if nothing")


def community_summary(cid: int, usage: dict) -> dict:
    c = store.conn()
    row = c.execute("SELECT title, summary, entity_ids, level FROM public.kg_communities WHERE id = %s", (cid,)).fetchone()
    title, summary, ids, level = row
    if summary:                                          # cached: summarised by an earlier question
        return {"id": cid, "title": title, "summary": summary, "cached": True}
    ents = c.execute("SELECT name, type, description FROM public.kg_entities WHERE id = ANY(%s) "
                     "ORDER BY degree DESC LIMIT 40", (ids,)).fetchall()
    rels = store.cypher("MATCH (a)-[r]->(b) WHERE a.id IN $ids AND b.id IN $ids "
                        "RETURN a.name, label(r), b.name, r.description ORDER BY r.weight DESC LIMIT 60",
                        {"ids": ids}, columns=("a", "t", "b", "d"))
    text = ("Entities:\n" + "\n".join(f"- {n} ({t}): {(d or '')[:250]}" for n, t, d in ents) +
            "\n\nRelations:\n" + "\n".join(f"- {a} -[{t}]-> {b}: {d or ''}" for a, t, b, d in rels))
    resp = claude().messages.parse(model=SMALL_MODEL, max_tokens=2000, output_format=_Summary,
                                   messages=[{"role": "user", "content": "Summarise this community of a knowledge "
                                              f"graph about India, using only the facts given.\n\n{text}"}])
    _add_usage(usage, _usage(resp))
    s = resp.parsed_output
    c.execute("UPDATE public.kg_communities SET title = %s, summary = %s, summarised_at = now() WHERE id = %s",
              (s.title, s.summary, cid))
    return {"id": cid, "title": s.title, "summary": s.summary, "cached": False}


def global_search(question: str) -> dict:
    from rag.models import embed_dense
    from rag.store import client
    t0 = time.perf_counter()
    usage: dict = {}
    qv = embed_dense([question])[0]
    hits = client().query_points(COMMUNITY_COLLECTION, query=qv, using="dense", limit=GLOBAL_COMMUNITIES).points
    with ThreadPoolExecutor(4) as pool:
        summaries = list(pool.map(lambda h: community_summary(h.payload["id"], usage), hits))
    t1 = time.perf_counter()

    def mapper(s):
        r = claude().messages.parse(
            model=SMALL_MODEL, max_tokens=1500, output_format=_Partial,
            messages=[{"role": "user", "content": f"Community: {s['title']}\n{s['summary']}\n\nQuestion: {question}\n"
                                                  "Using only this community summary, what does it contribute?"}])
        _add_usage(usage, _usage(r))
        return s | r.parsed_output.model_dump()
    with ThreadPoolExecutor(4) as pool:
        partials = list(pool.map(mapper, summaries))
    useful = sorted([p for p in partials if p["score"] > 0 and p["partial_answer"].strip()], key=lambda p: -p["score"])
    t2 = time.perf_counter()
    notes = "\n\n".join(f"[community {p['id']}: {p['title']}] (usefulness {p['score']})\n{p['partial_answer']}"
                        for p in useful)
    resp = claude().messages.create(
        model=ANSWER_MODEL, max_tokens=4000,
        system="Answer the question using ONLY the community notes. Give a well-organised overview, naming "
               "specific states/entities. If the notes do not cover it, say so.",
        messages=[{"role": "user", "content": f"<notes>\n{notes or '(no useful notes)'}\n</notes>\n\nQuestion: {question}"}])
    _add_usage(usage, _usage(resp))
    answer = "".join(b.text for b in resp.content if b.type == "text").strip()
    return {
        "mode": "global", "question": question, "answer": answer, "found": bool(useful), "citations": [],
        "contexts": [{"chunk_id": f"community-{p['id']}", "source": "graph", "pages": [], "text": f"{p['title']}: {p['summary']}"}
                     for p in useful],
        "graph": {"communities_read": len(summaries), "summaries_written_now": sum(not s["cached"] for s in summaries),
                  "useful": len(useful)},
        "usage": usage,
        "timings_s": {"summaries": round(t1 - t0, 3), "map": round(t2 - t1, 3),
                      "reduce": round(time.perf_counter() - t2, 3), "total": round(time.perf_counter() - t0, 3)},
    }


# ---------- router ----------
class _Route(BaseModel):
    route: Literal["lookup", "connections", "overview", "sales_numbers"]
    reason: str


def route(question: str) -> str:
    resp = claude().messages.parse(
        model=SMALL_MODEL, max_tokens=500, output_format=_Route,
        messages=[{"role": "user", "content":
                   "Classify the question.\n- lookup: one fact about one thing (a date, number, name, place).\n"
                   "- connections: needs linking facts across things (who owns, reports to, uses or depends on what; "
                   "chains like 'the manager of the team that built X'; comparisons of two things).\n"
                   "- overview: a big-picture or thematic question across many documents or topics.\n"
                   "- sales_numbers: a number to compute from the company's data tables (totals, counts, averages, "
                   "trends, rankings of metrics such as revenue, orders or customers).\n\n"
                   f"Question: {question}"}])
    return {"lookup": "vector", "connections": "local", "overview": "global",
            "sales_numbers": "data"}[resp.parsed_output.route]


def answer(question: str, mode: str = "auto", headings: list[str] | None = None,
           groups: list[str] | None = None, allow_data: bool = True) -> dict:
    """groups=None: no access restriction (CLI). With groups, every passage and relation is filtered, and
    overview (global) search is used only by callers who can see every group, because community summaries
    are written from all documents."""
    t0 = time.perf_counter()
    chosen = route(question) if mode == "auto" else mode
    note = None
    if chosen == "global" and not sees_everything(groups):
        chosen, note = "vector", "overview search needs access to every group; answered from searchable passages"
    if chosen == "data" and not allow_data:
        chosen, note = "vector", "this caller has no access to metrics; answered from documents"
    if chosen == "local":
        r = local_search(question, groups)
    elif chosen == "global":
        r = global_search(question)
    elif chosen == "data":
        from semantic.answer import answer_data_question
        r = answer_data_question(question) | {"mode": "data"}
    else:
        from rag.generate import ask
        r = ask(question, groups=groups, save=False, headings=headings) | {"mode": "vector"}
    r["routed"] = mode == "auto"
    if note:
        r["note"] = note
    r.setdefault("timings_s", {})["with_routing"] = round(time.perf_counter() - t0, 3)
    return r


def print_answer(r: dict):
    print(f"\n[{r['mode']}] {r['answer']}")
    g = r.get("graph", {})
    if g.get("seeds"):
        print("  entities: " + ", ".join(f"{s['name']} ({s['how']})" for s in g["seeds"]))
    if g.get("relations"):
        print("  graph facts used: " + "; ".join(f"{e['source']} {e['type']} {e['target']}" for e in g["relations"][:8]))
    for c in r.get("contexts", []):
        if c["chunk_id"] in r.get("citations", []):
            print(f"  [{c['chunk_id']}] {' > '.join(c.get('headings') or [])[:100]}")
    print(f"  ({r['timings_s'].get('total')}s)")


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    q = sys.argv[1]
    mode = sys.argv[2] if len(sys.argv) > 2 else "auto"
    print_answer(answer(q, mode))
