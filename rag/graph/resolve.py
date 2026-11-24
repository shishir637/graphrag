"""g2 - Entity resolution: decide which mentions are the same real-world thing.

Code first, LLM only where code is unsure:
  1. normalise names (case, punctuation, titles, company suffixes)  -> exact key matches merge
  2. blocking: only compare mentions of the same type that share a token, an initial or are
     near neighbours by embedding (avoids comparing every pair)
  3. score = 0.5 * name similarity (rapidfuzz) + 0.5 * cosine of "name: description" embeddings
     >= RESOLVE_AUTO_MERGE           -> merge
     RESOLVE_REVIEW .. AUTO_MERGE    -> ask the LLM (if RESOLVE_USE_LLM) else list for review
  4. union-find -> one canonical entity with aliases, all descriptions and all source chunks

Output: outputs/g2_resolution.json (entities, relations, every merge decision and why)
"""
import hashlib
import re
from collections import Counter, defaultdict

from pydantic import BaseModel
from rapidfuzz import fuzz

from ..config import RESOLVE_AUTO_MERGE, RESOLVE_REVIEW, RESOLVE_USE_LLM
from ..models import embed_dense
from ..utils import read_json, write_json
from . import llm

_TITLES = r"^(mr|mrs|ms|dr|prof|sri|smt|shri)\.?\s+"
_SUFFIXES = r"\s+(pvt|private|ltd|limited|inc|llc|corp|corporation|co|company|technologies|tech)\.?$"


def normalise(name: str) -> str:
    n = name.lower().strip()
    n = re.sub(_TITLES, "", n)
    n = re.sub(r"[^\w\s]", " ", n)
    n = " ".join(n.split())
    prev = None
    while prev != n:
        prev, n = n, re.sub(_SUFFIXES, "", n).strip()
    return n or name.lower()


class _Same(BaseModel):
    same: bool
    reason: str


_JUDGE = ("Decide whether two knowledge-graph entities extracted from the same document collection refer "
          "to the same real-world thing. Different people with similar names, or a product and the company "
          "that makes it, are NOT the same. Answer same=true only when the descriptions support it.")


class _UF:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        self.p[self.find(a)] = self.find(b)


def _cos(a, b):
    return sum(x * y for x, y in zip(a, b))


def resolve_all() -> dict:
    ext = read_json("g1_extraction.json")["chunks"]

    # one "mention group" per (normalised name, type): exact matches are merged without scoring
    groups: dict[tuple, dict] = {}
    for rec in ext:
        for e in rec["entities"]:
            k = (normalise(e["name"]), e["type"])
            g = groups.setdefault(k, {"names": Counter(), "type": e["type"], "descriptions": [], "chunks": []})
            g["names"][e["name"]] += 1
            g["descriptions"].append({"chunk_id": rec["chunk_id"], "text": e["description"]})
            if rec["chunk_id"] not in g["chunks"]:
                g["chunks"].append(rec["chunk_id"])
    keys = list(groups)
    if not keys:
        write_json("g2_resolution.json", {"stats": {"mentions": 0, "entities": 0}, "entities": [],
                                          "relations": [], "decisions": []})
        return {"entities": 0}

    texts = [f"{groups[k]['names'].most_common(1)[0][0]}: {groups[k]['descriptions'][0]['text']}" for k in keys]
    vecs = embed_dense(texts)

    # blocking
    by_type = defaultdict(list)
    for i, k in enumerate(keys):
        by_type[k[1]].append(i)
    candidates = set()
    for idxs in by_type.values():
        block = defaultdict(set)
        for i in idxs:
            toks = keys[i][0].split()
            for t in toks:
                if len(t) > 2:
                    block["t:" + t].add(i)
            block["i:" + "".join(t[0] for t in toks)].add(i)      # initials: "ini8 labs" ~ "il"
        for members in block.values():
            m = sorted(members)
            candidates.update((a, b) for x, a in enumerate(m) for b in m[x + 1:])
        for i in idxs:                                            # nearest neighbours by embedding
            sims = sorted(((_cos(vecs[i], vecs[j]), j) for j in idxs if j != i), reverse=True)[:3]
            candidates.update((min(i, j), max(i, j)) for _, j in sims)

    uf, decisions = _UF(len(keys)), []
    for a, b in sorted(candidates):
        na, nb = keys[a][0], keys[b][0]
        name_sim = max(fuzz.token_sort_ratio(na, nb), fuzz.partial_ratio(na, nb) if min(len(na), len(nb)) > 3 else 0) / 100
        emb_sim = _cos(vecs[a], vecs[b])
        score = round(0.5 * name_sim + 0.5 * emb_sim, 3)
        if score < RESOLVE_REVIEW:
            continue
        d = {"a": texts[a], "b": texts[b], "type": keys[a][1], "name_sim": round(name_sim, 3),
             "embedding_sim": round(emb_sim, 3), "score": score}
        if score >= RESOLVE_AUTO_MERGE:
            d.update(decision="merge", by="score")
        elif RESOLVE_USE_LLM:
            v = llm.structured(_JUDGE, f"Entity A ({keys[a][1]}): {texts[a]}\nEntity B ({keys[b][1]}): {texts[b]}", _Same)
            d.update(decision="merge" if v.same else "keep apart", by="llm", reason=v.reason)
        else:
            d.update(decision="review", by="score")
        if d["decision"] == "merge":
            uf.union(a, b)
        decisions.append(d)

    # canonical entities
    clusters = defaultdict(list)
    for i in range(len(keys)):
        clusters[uf.find(i)].append(i)
    entities, key_to_id = [], {}
    for members in clusters.values():
        names = Counter()
        for i in members:
            names.update(groups[keys[i]]["names"])
        canonical = max(names, key=lambda n: (names[n], len(n)))
        etype = keys[members[0]][1]
        eid = "e_" + hashlib.sha1(f"{normalise(canonical)}|{etype}".encode()).hexdigest()[:10]
        descs, chunks = [], []
        for i in members:
            key_to_id[keys[i]] = eid
            descs += groups[keys[i]]["descriptions"]
            chunks += [c for c in groups[keys[i]]["chunks"] if c not in chunks]
        entities.append({"id": eid, "name": canonical, "type": etype, "aliases": sorted(set(names) - {canonical}),
                         "descriptions": descs, "chunk_ids": chunks, "mentions": sum(names.values())})

    # relations: map mention names to canonical ids, merge duplicates
    name_type = {}
    for rec in ext:
        for e in rec["entities"]:
            name_type[(rec["chunk_id"], e["name"].lower())] = (normalise(e["name"]), e["type"])
    rels: dict[tuple, dict] = {}
    for rec in ext:
        for r in rec["relations"]:
            s = key_to_id.get(name_type.get((rec["chunk_id"], r["source"].lower())))
            t = key_to_id.get(name_type.get((rec["chunk_id"], r["target"].lower())))
            if not s or not t or s == t:
                continue
            k = (s, t, r["type"])
            x = rels.setdefault(k, {"source": s, "target": t, "type": r["type"], "descriptions": [],
                                    "chunk_ids": [], "strengths": []})
            x["descriptions"].append({"chunk_id": rec["chunk_id"], "text": r["description"]})
            x["strengths"].append(r["strength"])
            if rec["chunk_id"] not in x["chunk_ids"]:
                x["chunk_ids"].append(rec["chunk_id"])
    relations = []
    for x in rels.values():
        st = x.pop("strengths")
        relations.append({**x, "strength": round(sum(st) / len(st), 2), "weight": len(st)})

    mentions = sum(len(r["entities"]) for r in ext)
    stats = {"mentions": mentions, "name_groups": len(keys), "entities": len(entities),
             "relations": len(relations), "pairs_compared": len(candidates),
             "merged_by_score": sum(d["decision"] == "merge" and d["by"] == "score" for d in decisions),
             "merged_by_llm": sum(d["decision"] == "merge" and d["by"] == "llm" for d in decisions),
             "kept_apart_by_llm": sum(d["decision"] == "keep apart" for d in decisions),
             "needs_review": sum(d["decision"] == "review" for d in decisions)}
    write_json("g2_resolution.json", {"stats": stats, "entities": entities, "relations": relations,
                                      "decisions": decisions})
    return stats
