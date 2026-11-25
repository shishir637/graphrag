"""Graph search without a language model, over the merged graph the builder saves (outputs/_graph_build.json).

The calling agent does the reasoning; this module finds the evidence:

  link       entities named in the question (names and aliases, longest first; typo-tolerant)
  local      the question's entities, the links between them and their neighbours, with the sentence behind
             each link and the passages that connect them (multi-hop evidence)
  overview   the communities (Leiden clusters) closest to the question, each summarised extractively:
             main entities, strongest links and the most representative passages

Every passage, link and entity is checked against the caller's access groups: a link counts only if a
passage behind it is visible, and community summaries are built from visible passages only.
The graph is loaded once per process and reloaded when the build file changes. Apache AGE (Postgres)
keeps the same graph for Cypher queries and the explorer; answering does not depend on it being up.
"""
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path

from rapidfuzz import fuzz, process

ROOT = Path(__file__).resolve().parent.parent
from .config import OUT_DIR  # noqa: E402

BUILD = OUT_DIR / "_graph_build.json"           # per workspace (rag/wspath.py)
STOP = set("the a an of in on at to for and or is are was were be by with from as that this which what who whom "
           "how when where why do does did has have had it its their there than then into about between".split())
from rag.wspath import PerFile  # noqa: E402

_GS = PerFile(32)                               # one loaded graph per build file (i.e. per workspace)


def _st(w: str) -> str:
    """Light stemmer for word overlap: heads/headed -> head, owns/owned -> own, departments -> department."""
    w = w.lower()
    for suf in ("ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[: -len(suf)]
    return w


def _words(text: str) -> set[str]:
    return {_st(w) for w in re.findall(r"\w+", re.sub(r"['’]s\b", "", text.lower()))}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s.lower())).strip()


def ready() -> bool:
    return BUILD.exists()


def graph() -> dict:
    m = BUILD.stat().st_mtime if BUILD.exists() else None
    if m is None:
        raise FileNotFoundError("The knowledge graph has not been built yet.")
    _G = _GS.entry(BUILD)
    if _G["mtime"] != m:
        data = json.loads(BUILD.read_text(encoding="utf-8"))
        ents = {e["id"]: e for e in data["entities"]}
        names: dict[str, set] = defaultdict(set)
        for e in ents.values():
            for n in [e["name"], *(e.get("aliases") or [])]:
                k = _norm(n)
                if len(k) >= 3 and k not in STOP:
                    names[k].add(e["id"])
        adj: dict[str, list] = defaultdict(list)
        for i, r in enumerate(data["relations"]):
            adj[r["source_id"]].append((r["target_id"], i))
            adj[r["target_id"]].append((r["source_id"], i))
        chunk_ents: dict[str, set] = defaultdict(set)
        for e in ents.values():
            for c in e.get("chunk_ids") or []:
                chunk_ents[c].add(e["id"])
        df = Counter(len(e.get("chunk_ids") or []) for e in ents.values())
        texts, wdf = {}, Counter()
        cpath = OUT_DIR / "2_chunks.json"
        if cpath.exists():
            for c in json.loads(cpath.read_text(encoding="utf-8")):
                ws = _words(c["text"])
                texts[c["chunk_id"]] = ws
                wdf.update(ws)
        _G.update(texts=texts, wdf=wdf, mtime=m, ents=ents, rels=data["relations"], comms=data.get("communities", []), names=dict(names),
                  name_keys=list(names), adj=adj, chunk_ents=chunk_ents, n_chunks=max(1, len(chunk_ents)), df=df)
    return _G


def _visible_chunks(ids, groups, visible):
    return [c for c in ids or [] if visible(c, groups)]


def _seen(chunk_ids, groups, visible) -> bool:
    """Evidence from data tables (no passage) is shown; evidence from passages only when one is visible."""
    return not chunk_ids or any(visible(c, groups) for c in chunk_ids)


def rel_evidence(r: dict, groups, visible, limit: int = 300) -> str:
    """The sentences behind a link, only from passages the caller can see (older builds: the merged text)."""
    ev = r.get("evidence")
    if ev is None:
        return (r.get("description") or "")[:limit]
    texts = list(dict.fromkeys(e["text"] for e in ev if _seen([e["chunk_id"]] if e["chunk_id"] else [], groups, visible)))
    return " ".join(texts[:3])[:limit]


def ent_view(e: dict, groups, visible) -> dict:
    """An entity's name, aliases and description built only from what the caller may see."""
    ne, de = e.get("names_evidence"), e.get("description_evidence")
    if ne is None:
        return {"name": e["name"], "aliases": e.get("aliases") or [], "description": e.get("description") or ""}
    names = [x["text"] for x in ne if _seen(x["chunk_ids"], groups, visible)]
    name = e["name"] if e["name"] in names else (names[0] if names else e["name"])
    desc = " ".join(list(dict.fromkeys(x["text"] for x in de or [] if _seen(x["chunk_ids"], groups, visible)))[:4])
    return {"name": name, "aliases": [n for n in names if n != name][:20], "description": desc}


# ---------------------------------------------------------------- linking
def link(question: str, limit: int = 8) -> list[dict]:
    """Entity names in the question: exact word n-grams first (longest wins), then near-spellings."""
    G = graph()
    words = re.findall(r"[\w-]+", re.sub(r"['’]s\b", "", question.lower()))
    taken: list[tuple[int, int]] = []
    found: dict[str, dict] = {}
    for n in range(min(6, len(words)), 0, -1):
        for i in range(len(words) - n + 1):
            if any(i < b and a < i + n for a, b in taken):
                continue
            gram = " ".join(words[i:i + n])
            if gram in STOP or len(gram) < 3:
                continue
            ids = G["names"].get(_norm(gram))
            how = "name"
            if not ids and len(gram) >= 6 and n <= 4:
                best = process.extractOne(_norm(gram), G["name_keys"], scorer=fuzz.ratio, score_cutoff=90)
                if best:
                    ids, how = G["names"][best[0]], f"near '{best[0]}'"
            if ids:
                taken.append((i, i + n))
                # several entities share a name: keep the most mentioned
                eid = max(ids, key=lambda x: len(G["ents"][x].get("chunk_ids") or []))
                e = G["ents"][eid]
                found.setdefault(eid, {"id": eid, "name": e["name"], "type": e["type"], "how": how, "pos": i,
                                       "passages": len(e.get("chunk_ids") or [])})
    return sorted(found.values(), key=lambda x: x["pos"])[:limit]


# ---------------------------------------------------------------- local search
def local(question: str, groups=None, visible=lambda c, g: True, max_relations: int = 30,
          max_passages: int = 20) -> dict:
    """Evidence that connects the question's entities. Passage score:
       + 1 per question entity it names (idf-weighted: rare entities count more)
       + 0.6 per neighbour it names that links two question entities (a bridge for multi-hop)
       + 0.3 per link of a question entity whose evidence sentence is in it"""
    G = graph()
    seeds = link(question)
    seeds = [s for s in seeds if _visible_chunks(G["ents"][s["id"]].get("chunk_ids"), groups, visible)]
    seed_ids = {s["id"] for s in seeds}
    idf = {s["id"]: math.log(1 + G["n_chunks"] / (1 + s["passages"])) for s in seeds}

    rels, neighbours = [], Counter()
    for s in seed_ids:
        for other, ri in G["adj"].get(s, []):
            r = G["rels"][ri]
            chunks = _visible_chunks(r.get("chunk_ids"), groups, visible)
            if not chunks and r.get("chunk_ids"):
                continue
            rels.append((ri, r, chunks))
            if other not in seed_ids:
                neighbours[other] += 1
    bridges = {n for n, c in neighbours.items() if c >= 2}            # linked to two or more question entities
    direct = [x for x in rels if x[1]["source_id"] in seed_ids and x[1]["target_id"] in seed_ids]
    rest = sorted((x for x in rels if x not in direct),
                  key=lambda x: (-(x[1]["source_id"] in bridges or x[1]["target_id"] in bridges), -x[1].get("weight", 1)))
    chosen, seen = [], set()
    for ri, r, ch in direct + rest:
        if ri in seen:
            continue
        seen.add(ri)
        chosen.append((r, ch))
        if len(chosen) >= max_relations:
            break

    def overlap(c):
        ws = G["texts"].get(c, set())
        return sum(widf[w] for w in qwords if w in ws)

    # passages: for each question entity, its passages that best match the REST of the question
    # (idf-weighted word overlap), plus passages naming two question entities or a bridge entity
    seed_words = {w for s in seeds for w in _words(s["name"])}
    qwords = {_st(w) for w in re.findall(r"\w+", re.sub(r"['’]s\b", "", question.lower()))
              if w not in STOP and len(w) > 2} - seed_words
    N = max(1, len(G["texts"]))
    widf = {w: math.log(1 + N / (1 + G["wdf"].get(w, 0))) for w in qwords}

    # second hop: from the neighbours whose link sentence best matches the rest of the question
    # ("who heads the department that owns GridSense" -> GridSense -> Grid Analytics -> its head)
    hop2: dict[str, float] = {}
    if seed_ids and len(direct) == 0:
        def ev_overlap(r):
            ws = _words(r.get("description") or "")
            return sum(widf[w] for w in qwords if w in ws)
        near = Counter()
        for ri, r, ch in rels:
            other = r["target_id"] if r["source_id"] in seed_ids else r["source_id"]
            if other not in seed_ids and r["type"] != "MENTIONS":
                near[other] += r.get("weight", 1) * (1 + ev_overlap(r))
        for n, _ in near.most_common(3):
            for other, ri in sorted(G["adj"].get(n, []), key=lambda x: -G["rels"][x[1]].get("weight", 1))[:200]:
                r = G["rels"][ri]
                if other in seed_ids or r["type"] == "MENTIONS":
                    continue
                ch = _visible_chunks(r.get("chunk_ids"), groups, visible)
                if not ch:
                    continue
                o = ev_overlap(r)
                if o > 0:
                    hop2[ri] = o
        for ri in sorted(hop2, key=lambda x: -hop2[x])[:8]:
            r = G["rels"][ri]
            chosen.append((dict(r, hop=2), _visible_chunks(r.get("chunk_ids"), groups, visible)))

    score: dict[str, float] = defaultdict(float)
    per_entity = 4 if len(seed_ids) > 1 else 8
    for s in seed_ids:
        chunks = _visible_chunks(G["ents"][s].get("chunk_ids"), groups, visible)
        for c in sorted(chunks, key=lambda c: -overlap(c))[:per_entity]:
            score[c] += idf[s] + overlap(c)
    for c, ents_here in G["chunk_ents"].items():
        k = len(ents_here & seed_ids)
        b = len(ents_here & bridges)
        if (k >= 2 or (k >= 1 and b >= 1)) and visible(c, groups):
            score[c] += k * 1.0 + b * 0.6 + overlap(c)
    for r, ch in chosen:
        for c in ch:
            if c in score:
                score[c] += 0.3
            elif r.get("hop") == 2:
                score[c] += 0.5 + overlap(c)
    ranked = sorted(score.items(), key=lambda kv: -kv[1])[:max_passages]
    E = G["ents"]
    V = lambda eid: ent_view(E[eid], groups, visible)                    # noqa: E731
    return {
        "entities": [{k: s[k] for k in ("id", "type", "how")} | {"name": V(s["id"])["name"]} for s in seeds],
        "bridges": [{"id": b, "name": V(b)["name"], "type": E[b]["type"]} for b in list(bridges)[:10]],
        "relations": [{"source": V(r["source_id"])["name"], "type": r["type"], "target": V(r["target_id"])["name"],
                       "weight": r.get("weight", 1), "evidence": rel_evidence(r, groups, visible),
                       "passages": ch[:3], **({"hop": 2} if r.get("hop") == 2 else {})} for r, ch in chosen],
        "passage_ids": [c for c, _ in ranked],
        "passage_scores": {c: round(v, 3) for c, v in ranked},
        "entity_passages": {s["id"]: _visible_chunks(E[s["id"]].get("chunk_ids"), groups, visible) for s in seeds},
    }


# ---------------------------------------------------------------- overview (global) search
def overview(question: str, groups=None, visible=lambda c, g: True, top: int = 5) -> dict:
    """Communities closest to the question, by overlap of question words with the community's entity names,
    weighted by how central those entities are; each summarised from visible passages only."""
    G = graph()
    q = {w for w in re.findall(r"\w+", question.lower()) if w not in STOP and len(w) > 2}
    E = G["ents"]
    scored = []
    for c in G["comms"]:
        if c.get("level", 0) != 0:
            continue
        members = [E[i] for i in c["entity_ids"] if i in E]
        vis = [m for m in members if _visible_chunks(m.get("chunk_ids"), groups, visible)]
        if len(vis) < 3:
            continue
        s = 0.0
        for m in vis:
            words = set(re.findall(r"\w+", m["name"].lower()))
            if words & q:
                s += len(words & q) / len(words) * (1 + math.log(1 + m.get("degree", 0)))
        if s > 0:
            scored.append((s, c, vis))
    scored.sort(key=lambda x: -x[0])
    out = []
    for s, c, vis in scored[:top]:
        vis.sort(key=lambda m: -m.get("degree", 0))
        ids = {m["id"] for m in vis}
        links = sorted((r for r in G["rels"] if r["source_id"] in ids and r["target_id"] in ids
                        and _visible_chunks(r.get("chunk_ids"), groups, visible)),
                       key=lambda r: -r.get("weight", 1))[:8]
        pas = Counter()
        for m in vis[:30]:
            for ch in _visible_chunks(m.get("chunk_ids"), groups, visible):
                pas[ch] += 1
        V = lambda e: ent_view(e, groups, visible)["name"]                  # noqa: E731
        out.append({"community": c["id"], "title": ", ".join(V(m) for m in vis[:3]), "size": len(vis),
                    "score": round(s, 2),
                    "main_entities": [{"name": V(m), "type": m["type"], "links": m.get("degree", 0)} for m in vis[:12]],
                    "key_links": [{"source": V(E[r["source_id"]]), "type": r["type"], "target": V(E[r["target_id"]]),
                                   "evidence": rel_evidence(r, groups, visible, 200)} for r in links],
                    "passage_ids": [p for p, _ in pas.most_common(4)]})
    return {"communities": out}


def entity(name: str, groups=None, visible=lambda c, g: True, limit: int = 25) -> dict | None:
    """One entity with its links (each with evidence) for the explore tool."""
    G = graph()
    k = _norm(name)
    ids = G["names"].get(k)
    if not ids:
        best = process.extractOne(k, G["name_keys"], scorer=fuzz.WRatio, score_cutoff=88)
        ids = G["names"].get(best[0]) if best else None
    if not ids:
        return None
    eid = max(ids, key=lambda x: len(G["ents"][x].get("chunk_ids") or []))
    e = G["ents"][eid]
    if not _visible_chunks(e.get("chunk_ids"), groups, visible) and e.get("chunk_ids"):
        return None
    links = []
    for other, ri in sorted(G["adj"].get(eid, []), key=lambda x: -G["rels"][x[1]].get("weight", 1)):
        r = G["rels"][ri]
        ch = _visible_chunks(r.get("chunk_ids"), groups, visible)
        if r.get("chunk_ids") and not ch:
            continue
        links.append({"entity": ent_view(G["ents"][other], groups, visible)["name"], "type": r["type"],
                      "direction": "out" if r["source_id"] == eid else "in", "weight": r.get("weight", 1),
                      "evidence": rel_evidence(r, groups, visible), "passages": ch[:3]})
        if len(links) >= limit:
            break
    me = ent_view(e, groups, visible)
    return {"id": eid, "name": me["name"], "type": e["type"], "aliases": me["aliases"][:10],
            "description": me["description"][:400],
            "passages": _visible_chunks(e.get("chunk_ids"), groups, visible)[:20], "links": links,
            "community": e.get("community_l0")}
