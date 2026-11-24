"""g2 - Entity resolution: decide which extracted names refer to the same real-world thing.

Cheapest signal first. Every candidate pair goes down the tiers below and the first tier that
is sure decides it; only pairs no cheaper tier can settle reach the LLM.

  0 override   your decisions in graph_resolution_overrides.json (always win)
  1 exact      same normalised name and type -> one surface entity, no pair needed
  2 name gate  names carrying different numbers ("v2" vs "v3", "Opus 4" vs "Opus 5.5"), a number on
               one side only ("GPT" vs "GPT-4"), or very long names -> never match
  3 fuzzy      same tokens in a different order or spelling, an acronym of the other name
               ("BAAI" / "Beijing Academy of Artificial Intelligence"), or the same name plus
               only generic words ("Qdrant" / "Qdrant vector database") -> match
  4 embedding  cosine of the names' BGE-M3 vectors: >= RESOLVE_EMBED_MATCH match,
               < RESOLVE_EMBED_NO_MATCH no match, in between ambiguous
  5 cluster    ambiguous pairs inside a tight group (average similarity >= RESOLVE_EMBED_MATCH)
               match without an LLM call
  6 llm        the remaining ambiguous pairs, most similar first, up to RESOLVE_MAX_LLM_PAIRS.
               The LLM answers match / no_match / uncertain; only "match" links.

Matches are stored as LINKS between surface entities, never as destructive merges: every link
says which tier made it and why, so it can be reviewed and undone with an override. Linked
surface entities are grouped under one resolved entity. A pair nobody could decide stays
"undecided" - that is not a rejection. A "no_match" is a cannot-link constraint: a weaker match
that would join the two groups through a third entity is refused.

Output: outputs/g2_resolution.json
"""
import hashlib
import json
import re
from collections import Counter, defaultdict
from typing import Literal

from pydantic import BaseModel
from rapidfuzz import fuzz

from ..config import (RESOLVE_EMBED_MATCH, RESOLVE_EMBED_NO_MATCH, RESOLVE_MAX_LLM_PAIRS, RESOLVE_USE_LLM,
                      ROOT)
from ..models import embed_dense
from ..utils import read_json, write_json
from . import llm

OVERRIDES_FILE = ROOT / "graph_resolution_overrides.json"
LONG_NAME_CHARS = 80
FUZZY_MATCH = 95                          # rapidfuzz token_sort_ratio on normalised names

_TITLES = r"^(mr|mrs|ms|dr|prof|sri|smt|shri)\.?\s+"
_SUFFIXES = r"\s+(pvt|private|ltd|limited|inc|llc|corp|corporation|co|company|technologies|tech)\.?$"
_DIGITS = re.compile(r"\d+(?:\.\d+)*")
_STOP = {"of", "the", "and", "for", "a", "an", "in", "on", "&"}
# Words that describe what kind of thing a name is without changing which thing it is
_GENERIC = {"model", "models", "library", "framework", "database", "db", "vector", "tool", "system", "platform",
            "service", "api", "sdk", "team", "department", "dept", "project", "app", "application", "engine",
            "software", "package", "server", "client", "product", "group", "the"}

TIER_ORDER = ["override", "exact", "fuzzy", "embedding", "cluster", "llm"]   # confidence, highest first


def normalise(name: str) -> str:
    n = name.lower().strip()
    n = re.sub(r"\(([^)]*)\)", r" \1 ", n)          # "Beijing Academy (BAAI)" keeps the acronym as a token
    n = re.sub(_TITLES, "", n)
    n = re.sub(r"[^\w\s.]", " ", n)
    n = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", n)       # keep "5.5", drop sentence dots
    n = " ".join(n.split())
    prev = None
    while prev != n:
        prev, n = n, re.sub(_SUFFIXES, "", n).strip()
    return n or name.lower()


def _initials(n: str) -> str:
    return "".join(t[0] for t in n.split() if t not in _STOP)


def name_gate(a: str, b: str) -> str | None:
    """Reason two different names may never match, or None."""
    da, db = set(_DIGITS.findall(a)), set(_DIGITS.findall(b))
    if da and db and da != db:
        return f"different numbers ({', '.join(sorted(da))} vs {', '.join(sorted(db))})"
    if bool(da) != bool(db):
        # "GPT" vs "GPT-4", "Claude" vs "Claude Opus 5.5": a version on one side usually means a specific
        # thing vs the family. Conservative on purpose; add an override to match such a pair.
        return "number on one side only"
    if max(len(a), len(b)) > LONG_NAME_CHARS:
        return "name too long to compare"
    return None


def fuzzy_rule(a: str, b: str) -> str | None:
    """Reason two names are certainly the same, or None."""
    if fuzz.token_sort_ratio(a, b) >= FUZZY_MATCH:
        return f"same words, minor spelling/order difference ({fuzz.token_sort_ratio(a, b):.0f})"
    for short, long_ in ((a, b), (b, a)):
        compact = short.replace(" ", "")
        if 3 <= len(compact) <= 6 and len(long_.split()) >= 2 and _initials(long_) == compact:
            return f"'{short}' is the acronym of '{long_}'"
        ta, tb = set(short.split()), set(long_.split())
        if ta and ta < tb and (tb - ta) <= _GENERIC:
            return f"same name plus generic words ({', '.join(sorted(tb - ta))})"
        rest = [t for t in long_.split() if t != compact]
        if compact in long_.split() and len(compact) >= 3 and len(rest) >= 2 and _initials(" ".join(rest)) == compact:
            return f"'{short}' is the acronym given inside '{long_}'"
    return None


class _Verdict(BaseModel):
    verdict: Literal["match", "no_match", "uncertain"]
    reason: str


_JUDGE = ("Decide whether two knowledge-graph entities extracted from the same document collection refer to the "
          "same real-world thing. Different people with similar names, different versions of a product, and a "
          "product versus the company that makes it are NOT the same. Answer 'match' only when the names and "
          "descriptions clearly support it, 'no_match' when they clearly do not, otherwise 'uncertain'.")


class _UF:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x


def _cos(a, b):
    return sum(x * y for x, y in zip(a, b))


def _load_overrides() -> tuple[set, set]:
    """graph_resolution_overrides.json: {"match": [["name a", "name b"], ...], "no_match": [...]}.
    Names are compared after normalise(); the type must be the same for a match to apply."""
    if not OVERRIDES_FILE.exists():
        return set(), set()
    raw = json.loads(OVERRIDES_FILE.read_text(encoding="utf-8"))
    pair = lambda p: frozenset((normalise(p[0]), normalise(p[1])))
    return {pair(p) for p in raw.get("match", [])}, {pair(p) for p in raw.get("no_match", [])}


def _average_linkage(nodes: list[int], sim: dict, threshold: float) -> list[list[int]]:
    """Greedy agglomerative clustering: join the two clusters with the highest average pairwise
    similarity while it stays >= threshold. Unscored pairs count as 0."""
    clusters = [[n] for n in nodes]

    def avg(c1, c2):
        return sum(sim.get((min(a, b), max(a, b)), 0.0) for a in c1 for b in c2) / (len(c1) * len(c2))

    while len(clusters) > 1:
        best, bi, bj = -1.0, -1, -1
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                s = avg(clusters[i], clusters[j])
                if s > best:
                    best, bi, bj = s, i, j
        if best < threshold:
            break
        clusters[bi] += clusters.pop(bj)
    return [c for c in clusters if len(c) > 1]


def resolve_all() -> dict:
    ext = read_json("g1_extraction.json")["chunks"]
    forced_match, forced_no = _load_overrides()

    # tier 1: exact - one surface entity per (normalised name, type)
    surf: dict[tuple, dict] = {}
    for rec in ext:
        for e in rec["entities"]:
            k = (normalise(e["name"]), e["type"])
            s = surf.setdefault(k, {"norm": k[0], "type": e["type"], "names": Counter(), "descriptions": [],
                                    "chunk_ids": []})
            s["names"][e["name"]] += 1
            s["descriptions"].append({"chunk_id": rec["chunk_id"], "text": e["description"]})
            if rec["chunk_id"] not in s["chunk_ids"]:
                s["chunk_ids"].append(rec["chunk_id"])
    keys = list(surf)
    S = [surf[k] for k in keys]
    for i, s in enumerate(S):
        s["id"] = "s_" + hashlib.sha1(f"{s['norm']}|{s['type']}".encode()).hexdigest()[:10]
        s["label"] = s["names"].most_common(1)[0][0]
    if not S:
        write_json("g2_resolution.json", {"stats": {"mentions": 0, "entities": 0}, "surface_entities": [],
                                          "links": [], "entities": [], "relations": []})
        return {"entities": 0}

    vecs = embed_dense([s["label"] for s in S])        # names only: identity, not topic

    # blocking: only same-type pairs that share a token, initials/acronym, or are near neighbours
    by_type = defaultdict(list)
    for i, s in enumerate(S):
        by_type[s["type"]].append(i)
    cand = set()
    for idxs in by_type.values():
        block = defaultdict(set)
        for i in idxs:
            toks = S[i]["norm"].split()
            for t in toks:
                if len(t) > 2 and t not in _GENERIC:
                    block["t:" + t].add(i)
            block["a:" + (_initials(S[i]["norm"]) if len(toks) > 1 else S[i]["norm"])].add(i)
        for members in block.values():
            m = sorted(members)
            cand.update((a, b) for x, a in enumerate(m) for b in m[x + 1:])
        for i in idxs:
            near = sorted(((_cos(vecs[i], vecs[j]), j) for j in idxs if j != i), reverse=True)[:3]
            cand.update((min(i, j), max(i, j)) for _, j in near)

    # tiers 0, 2, 3, 4 decide each pair on its own
    links, sim, ambiguous = [], {}, []
    for a, b in sorted(cand):
        na, nb = S[a]["norm"], S[b]["norm"]
        cs = round(_cos(vecs[a], vecs[b]), 3)
        sim[(a, b)] = cs
        base = {"a": a, "b": b, "embedding_sim": cs, "name_sim": round(fuzz.token_sort_ratio(na, nb) / 100, 3)}
        pk = frozenset((na, nb))
        if pk in forced_match:
            links.append({**base, "decision": "match", "tier": "override", "reason": "listed under match in overrides"})
        elif pk in forced_no:
            links.append({**base, "decision": "no_match", "tier": "override", "reason": "listed under no_match in overrides"})
        elif (why := name_gate(na, nb)):
            # only worth recording when the gate stopped something that looked like a match
            if base["name_sim"] >= 0.6 or cs >= RESOLVE_EMBED_NO_MATCH:
                links.append({**base, "decision": "no_match", "tier": "name_gate", "reason": why})
        elif (why := fuzzy_rule(na, nb)):
            links.append({**base, "decision": "match", "tier": "fuzzy", "reason": why})
        elif cs >= RESOLVE_EMBED_MATCH:
            links.append({**base, "decision": "match", "tier": "embedding", "reason": f"name similarity {cs} >= {RESOLVE_EMBED_MATCH}"})
        elif cs < RESOLVE_EMBED_NO_MATCH:
            continue                                     # clearly different: not worth recording
        else:
            ambiguous.append(base)

    # tier 5: cluster - ambiguous pairs inside a tight group of names match without the LLM
    blocked = {(l["a"], l["b"]) for l in links if l["decision"] == "no_match"}
    clustered = set()
    for t, idxs in by_type.items():
        amb_nodes = sorted({x for p in ambiguous for x in (p["a"], p["b"]) if S[x]["type"] == t})
        if len(amb_nodes) < 2:
            continue
        nodes = sorted(set(amb_nodes) | {x for (a, b), s in sim.items() for x in (a, b)
                                         if s >= RESOLVE_EMBED_MATCH and S[a]["type"] == t and (a in amb_nodes or b in amb_nodes)})
        local = {k: v for k, v in sim.items() if k[0] in nodes and k[1] in nodes and k not in blocked}
        for group in _average_linkage(nodes, local, RESOLVE_EMBED_MATCH):
            g = set(group)
            clustered |= {(p["a"], p["b"]) for p in ambiguous if p["a"] in g and p["b"] in g}
    still = []
    for p in ambiguous:
        if (p["a"], p["b"]) in clustered:
            links.append({**p, "decision": "match", "tier": "cluster", "reason": "inside a tight group of similar names"})
        else:
            still.append(p)

    # tier 6: llm - most similar first, within budget; the rest stay undecided
    still.sort(key=lambda p: (-p["embedding_sim"], p["a"], p["b"]))
    use_llm = RESOLVE_USE_LLM and llm.available()
    budget = RESOLVE_MAX_LLM_PAIRS if use_llm else 0
    asked = 0
    for n, p in enumerate(still):
        if n >= budget:
            reason = ("LLM disabled (RESOLVE_USE_LLM=False)" if not RESOLVE_USE_LLM else
                      "no LLM configured" if not use_llm else f"over the LLM budget of {budget} pairs")
            links.append({**p, "decision": "undecided", "tier": "llm", "reason": reason})
            continue
        A, B = S[p["a"]], S[p["b"]]
        da = " ".join(dict.fromkeys(d["text"] for d in A["descriptions"]))[:600]
        db = " ".join(dict.fromkeys(d["text"] for d in B["descriptions"]))[:600]
        asked += 1
        v = llm.structured(_JUDGE, f"Entity A ({A['type']}): {A['label']}\nDescription: {da}\n\n"
                                   f"Entity B ({B['type']}): {B['label']}\nDescription: {db}", _Verdict)
        dec = {"match": "match", "no_match": "no_match"}.get(v.verdict, "undecided")
        links.append({**p, "decision": dec, "tier": "llm", "reason": v.reason})

    # group: strongest links first; refuse a match that would join two groups holding a no_match pair
    uf = _UF(len(S))
    cannot = [(l["a"], l["b"]) for l in links if l["decision"] == "no_match"]
    rank = {t: i for i, t in enumerate(TIER_ORDER)}
    for l in sorted((l for l in links if l["decision"] == "match"),
                    key=lambda l: (rank.get(l["tier"], 9), -l["embedding_sim"])):
        ra, rb = uf.find(l["a"]), uf.find(l["b"])
        if ra == rb:
            l["applied"] = True
            continue
        conflict = next(((x, y) for x, y in cannot if {uf.find(x), uf.find(y)} == {ra, rb}), None)
        if conflict:
            l["applied"] = False
            l["blocked_by"] = f"no_match between '{S[conflict[0]]['label']}' and '{S[conflict[1]]['label']}'"
        else:
            uf.p[ra] = rb
            l["applied"] = True

    groups = defaultdict(list)
    for i in range(len(S)):
        groups[uf.find(i)].append(i)
    entities, surf_to_ent = [], {}
    for members in groups.values():
        names = Counter()
        for i in members:
            names.update(S[i]["names"])
        canonical = max(names, key=lambda n: (names[n], len(n)))
        etype = S[members[0]]["type"]
        eid = "e_" + hashlib.sha1(f"{normalise(canonical)}|{etype}".encode()).hexdigest()[:10]
        descs, chunks = [], []
        for i in members:
            surf_to_ent[i] = eid
            descs += S[i]["descriptions"]
            chunks += [c for c in S[i]["chunk_ids"] if c not in chunks]
        entities.append({"id": eid, "name": canonical, "type": etype, "aliases": sorted(set(names) - {canonical}),
                         "members": [S[i]["id"] for i in members], "descriptions": descs, "chunk_ids": chunks,
                         "mentions": sum(names.values())})

    # relations: mention -> surface -> resolved entity, duplicates merged
    key_index = {k: i for i, k in enumerate(keys)}
    lookup = {}
    for rec in ext:
        for e in rec["entities"]:
            lookup[(rec["chunk_id"], e["name"].lower())] = key_index[(normalise(e["name"]), e["type"])]
    rels: dict[tuple, dict] = {}
    for rec in ext:
        for r in rec["relations"]:
            si, ti = lookup.get((rec["chunk_id"], r["source"].lower())), lookup.get((rec["chunk_id"], r["target"].lower()))
            if si is None or ti is None:
                continue
            s, t = surf_to_ent[si], surf_to_ent[ti]
            if s == t:
                continue
            x = rels.setdefault((s, t, r["type"]), {"source": s, "target": t, "type": r["type"], "descriptions": [],
                                                    "chunk_ids": [], "strengths": []})
            x["descriptions"].append({"chunk_id": rec["chunk_id"], "text": r["description"]})
            x["strengths"].append(r["strength"])
            if rec["chunk_id"] not in x["chunk_ids"]:
                x["chunk_ids"].append(rec["chunk_id"])
    relations = []
    for x in rels.values():
        st = x.pop("strengths")
        relations.append({**x, "strength": round(sum(st) / len(st), 2), "weight": len(st)})

    out_links = [{**{k: v for k, v in l.items() if k not in ("a", "b")}, "a": S[l["a"]]["label"],
                  "b": S[l["b"]]["label"], "a_id": S[l["a"]]["id"], "b_id": S[l["b"]]["id"], "type": S[l["a"]]["type"]}
                 for l in links]
    by = Counter((l["tier"], l["decision"]) for l in links)
    stats = {"mentions": sum(len(r["entities"]) for r in ext), "surface_entities": len(S),
             "entities": len(entities), "relations": len(relations), "pairs_compared": len(cand),
             "by_tier": {f"{t}:{d}": n for (t, d), n in sorted(by.items())},
             "llm_pairs": asked, "llm_available": use_llm,
             "undecided": sum(l["decision"] == "undecided" for l in links),
             "matches_blocked_by_no_match": sum(l.get("applied") is False for l in links),
             "overrides_file": OVERRIDES_FILE.name if OVERRIDES_FILE.exists() else None}
    write_json("g2_resolution.json", {"stats": stats,
                                      "surface_entities": [{"id": s["id"], "name": s["label"], "type": s["type"],
                                                            "variants": dict(s["names"]), "chunk_ids": s["chunk_ids"]}
                                                           for s in S],
                                      "links": out_links, "entities": entities, "relations": relations})
    return stats
