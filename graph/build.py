"""Build the knowledge graph from extraction records.

  1. collect   entity mentions + relations from the extraction .jsonl files
  2. tables    map structured data (data/*.csv) to entities and relations - no LLM
  3. resolve   merge duplicates: exact normalised name, then RapidFuzz near-spellings within the same type
               family (no language model; unclear pairs stay separate, which is the safe default)
  4. relations aggregate repeated relations into weighted edges
  5. communities  Leiden (NetworkX, modularity), two levels; summaries are extractive, built at query time
  6. store     outputs/_graph_build.json (answering reads this) + Apache AGE when the database is reachable

  python -m graph.build outputs/gr_extract_south.jsonl [--tables]

Outputs: outputs/gr_2_resolution.json, outputs/gr_3_graph.json, outputs/gr_4_communities.json
"""
import argparse
import hashlib
import json
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict

import numpy as np
from rapidfuzz import fuzz, process

from rag.utils import write_json

from .config import AUTO_MERGE_SCORE, OUT_DIR, ROOT

PLACE_TYPES = {"State", "City", "District", "Place", "Country", "GeoFeature"}


def norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower().replace("&", " and ")
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"^(the)\s+", "", s.strip())
    return re.sub(r"\s+", " ", s).strip()


def _eid(typ: str, key: str) -> str:
    return "e_" + hashlib.sha1(f"{typ}|{key}".encode()).hexdigest()[:12]


# ---------- 1. collect ----------
def collect(paths: list[str]):
    groups: dict[str, dict] = {}            # normalised name -> mention group
    rels, chunk_ids = [], set()
    for p in paths:
        for line in open(p, encoding="utf-8"):
            r = json.loads(line)
            chunk_ids.add(r["chunk_id"])
            local = {}
            for e in r["entities"]:
                k = norm(e["name"])
                if not k:
                    continue
                g = groups.setdefault(k, {"key": k, "names": Counter(), "types": Counter(), "descriptions": [],
                                          "chunk_ids": set(), "origin": "document"})
                g["names"][e["name"]] += 1
                g["types"][e["type"]] += 1
                if e.get("description") and e["description"] not in g["descriptions"]:
                    g["descriptions"].append(e["description"])
                g["chunk_ids"].add(r["chunk_id"])
                local[e["name"]] = k
            for x in r["relations"]:
                s, t = local.get(x["source"]), local.get(x["target"])
                if s and t and s != t:
                    rels.append({"s": s, "t": t, "type": x["type"], "description": x.get("description", ""),
                                 "strength": x.get("strength", 5), "chunk_id": r["chunk_id"]})
    return groups, rels, chunk_ids


# ---------- 2. structured tables ----------
def map_tables(groups: dict, rels: list):
    """Rows of dimension tables become entities, foreign keys become relations. Orders/amounts stay in the
    semantic layer (numbers are answered there, not by the graph)."""
    import pandas as pd
    from semantic.config import DATA_DIR as data
    if not (data / "regions.csv").exists():
        return 0
    regions = pd.read_csv(data / "regions.csv")
    customers = pd.read_csv(data / "customers.csv")
    products = pd.read_csv(data / "products.csv")
    added = 0

    def ent(name, typ, desc, source):
        nonlocal added
        k = norm(name)
        g = groups.setdefault(k, {"key": k, "names": Counter(), "types": Counter(), "descriptions": [],
                                  "chunk_ids": set(), "origin": "table"})
        g["names"][name] += 1
        g["types"][typ] += 1
        if desc not in g["descriptions"]:
            g["descriptions"].append(desc)
        g.setdefault("tables", set()).add(source)
        added += 1
        return k

    region_key = {}
    for _, r in regions.iterrows():
        region_key[r.region_id] = ent(f"{r.region_name} sales region", "SalesRegion",
                                      f"Sales region '{r.region_name}' ({r.country}) in the sales data.", "regions")
    city_region = customers.groupby("city")["region_id"].agg(lambda s: s.mode()[0])
    city_n = customers.groupby("city").size()
    for city, rid in city_region.items():
        k = ent(city, "City", f"{city}: city with {city_n[city]} customers in the sales data.", "customers")
        rels.append({"s": k, "t": region_key[rid], "type": "PART_OF", "description": f"{city} belongs to a sales region.",
                     "strength": 10, "chunk_id": None})
    for cat in sorted(products.category.unique()):
        ent(f"{cat} products", "ProductCategory", f"Product category '{cat}' in the sales catalogue.", "products")
    for _, p in products.iterrows():
        k = ent(p.product_name, "Product", f"{p.product_name}: {p.category} product, unit price INR {p.unit_price:,.0f}.",
                "products")
        rels.append({"s": k, "t": norm(f"{p.category} products"), "type": "PART_OF",
                     "description": f"{p.product_name} is in category {p.category}.", "strength": 10, "chunk_id": None})
    return added


# ---------- 3. resolve ----------
def resolve(groups: dict):
    """Merge mention groups that name the same thing. Exact normalised names were already grouped by collect();
    here near-spellings ("Pondicherry"/"Puducherry" stays apart, "Arunachal Pradesh"/"Arunachal Pradesh state"
    merges) are joined when RapidFuzz token_sort_ratio >= AUTO_MERGE_SCORE within one type family and the numbers
    in the names agree. Comparisons are blocked by the first letter so this stays fast with 50,000 names."""
    items = list(groups.values())
    for g in items:
        g["type"] = g["types"].most_common(1)[0][0]
        g["name"] = max(g["names"].items(), key=lambda kv: (kv[1], len(kv[0])))[0]
    t0 = time.perf_counter()
    parent = list(range(len(items)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    auto = []
    blocks = defaultdict(list)
    for i, g in enumerate(items):
        fam = "place" if g["type"] in PLACE_TYPES else g["type"]
        blocks[(fam, g["key"][:1])].append(i)
    for idx in blocks.values():
        if len(idx) < 2:
            continue
        names = [items[i]["key"] for i in idx]
        scores = process.cdist(names, names, scorer=fuzz.token_sort_ratio, workers=-1, score_cutoff=AUTO_MERGE_SCORE)
        for a, b in zip(*np.nonzero(np.triu(scores, 1))):
            i, j = idx[a], idx[b]
            if re.findall(r"\d+", items[i]["key"]) != re.findall(r"\d+", items[j]["key"]):
                continue
            if items[i]["type"] != items[j]["type"] and scores[a][b] < 99:
                continue
            auto.append((i, j))
    for i, j in auto:
        parent[find(i)] = find(j)

    merged: dict[int, dict] = {}
    for i, g in enumerate(items):
        m = merged.setdefault(find(i), {"members": []})
        m["members"].append(g)
    entities, key_to_id = [], {}
    for m in merged.values():
        mem = m["members"]
        names = Counter()
        types = Counter()
        for g in mem:
            names.update(g["names"])
            types.update(g["types"])
        doc_types = Counter({t: c for g in mem if g["origin"] == "document" for t, c in g["types"].items()})
        typ = (doc_types or types).most_common(1)[0][0]
        name = max(names.items(), key=lambda kv: (kv[1], len(kv[0])))[0]
        eid = _eid(typ, norm(name))
        descs = list(dict.fromkeys(d for g in mem for d in g["descriptions"]))
        entities.append({"id": eid, "name": name, "type": typ, "aliases": sorted(set(names) - {name})[:20],
                         "description": " ".join(descs[:4]), "chunk_ids": sorted({c for g in mem for c in g["chunk_ids"]}),
                         "mentions": sum(names.values()),
                         "origin": sorted({g["origin"] for g in mem} | ({"table"} if any(g.get("tables") for g in mem) else set()))})
        for g in mem:
            key_to_id[g["key"]] = eid
    report = {"mention_groups": len(items), "entities": len(entities), "auto_merges": len(auto),
              "merge_examples": [(items[i]["name"], items[j]["name"]) for i, j in auto[:40]],
              "seconds": round(time.perf_counter() - t0, 1)}
    return entities, key_to_id, report


# ---------- 4. relations ----------
def aggregate(rels: list, key_to_id: dict) -> list[dict]:
    agg: dict[tuple, dict] = {}
    for r in rels:
        s, t = key_to_id.get(r["s"]), key_to_id.get(r["t"])
        if not s or not t or s == t:
            continue
        if r["type"] == "RELATED_TO" and t < s:                # co-occurrence has no direction: one edge per pair
            s, t = t, s
        a = agg.setdefault((s, r["type"], t), {"source_id": s, "target_id": t, "type": r["type"], "count": 0,
                                               "strength": 0, "descriptions": [], "chunk_ids": []})
        a["count"] += 1
        a["strength"] += r["strength"]
        if r["description"] and r["description"] not in a["descriptions"]:
            a["descriptions"].append(r["description"])
        if r["chunk_id"] and r["chunk_id"] not in a["chunk_ids"]:
            a["chunk_ids"].append(r["chunk_id"])
    out = []
    for a in agg.values():
        a["weight"] = round(a["strength"] / 10, 2)              # repeated + strong relations weigh more
        a["description"] = " ".join(a.pop("descriptions")[:3])
        del a["strength"]
        out.append(a)
    return out


# ---------- 5. communities ----------
def communities(entities: list[dict], relations: list[dict]) -> list[dict]:
    """Leiden with the modularity objective (NetworkX, BSD licence; igraph/leidenalg are GPL and would
    bind a distributed product). Two levels: communities over 25 entities are split again."""
    import networkx as nx
    g = nx.Graph()
    g.add_nodes_from(e["id"] for e in entities)
    for r in relations:
        a, b, w = r["source_id"], r["target_id"], r["weight"]
        if a == b:
            continue
        if g.has_edge(a, b):
            g[a][b]["weight"] += w
        else:
            g.add_edge(a, b, weight=w)
    for e in entities:
        e["degree"] = g.degree(e["id"])

    def leiden(graph):
        if graph.number_of_edges() == 0:
            return [{n} for n in graph.nodes]
        return nx.community.leiden_communities(graph, weight="weight", resolution=1.0, metric="modularity", seed=7)

    comms, next_id = [], 0
    by_id = {e["id"]: e for e in entities}

    def profile(member_ids):
        mem = sorted((by_id[m] for m in member_ids), key=lambda e: -e["degree"])
        return "; ".join(f"{e['name']} ({e['type']})" for e in mem[:25])

    for members in sorted(leiden(g), key=len, reverse=True):
        if len(members) < 3:                                    # isolated bits: not a community
            continue
        ids0 = sorted(members)
        cid0 = next_id
        next_id += 1
        comms.append({"id": cid0, "level": 0, "parent": None, "entity_ids": ids0, "profile": profile(ids0)})
        for m in ids0:
            by_id[m]["community_l0"] = cid0
        if len(members) > 25:                                   # level 1: split big communities again
            for sm in sorted(leiden(g.subgraph(members)), key=len, reverse=True):
                if len(sm) < 3 or len(sm) == len(members):
                    continue
                ids1 = sorted(sm)
                comms.append({"id": next_id, "level": 1, "parent": cid0, "entity_ids": ids1, "profile": profile(ids1)})
                for m in ids1:
                    by_id[m]["community_l1"] = next_id
                next_id += 1
    return comms


# ---------- 6. store ----------
def build(paths: list[str], tables: bool = True, reuse: bool = False):
    from . import store
    from rag.utils import read_json
    t0 = time.perf_counter()
    cache = OUT_DIR / "_graph_build.json"          # merged graph before storage: lets a storage failure be retried fast
    if reuse and cache.exists():
        saved = json.loads(cache.read_text(encoding="utf-8"))
        entities, relations, comms, chunk_ids = (saved["entities"], saved["relations"], saved["communities"],
                                                 set(saved["chunk_ids"]))
        print(f"  reusing merged graph from {cache.name}", flush=True)
        return _store(store, read_json, entities, relations, comms, chunk_ids, t0)
    groups, rels, chunk_ids = collect(paths)
    n_table = map_tables(groups, rels) if tables else 0
    print(f"  collected {len(groups)} name groups, {len(rels)} relation mentions from {len(chunk_ids)} chunks"
          f" (+{n_table} table rows)", flush=True)
    entities, key_to_id, rep = resolve(groups)
    write_json("gr_2_resolution.json", rep)
    print(f"  resolved to {len(entities)} entities ({rep['auto_merges']} near-spelling merges)", flush=True)
    relations = aggregate(rels, key_to_id)
    comms = communities(entities, relations)
    print(f"  {len(relations)} edges, {len(comms)} communities "
          f"({sum(c['level'] == 0 for c in comms)} top-level)", flush=True)
    cache.write_text(json.dumps({"entities": entities, "relations": relations, "communities": comms,
                                 "chunk_ids": sorted(chunk_ids)}, ensure_ascii=False), encoding="utf-8")
    return _store(store, read_json, entities, relations, comms, chunk_ids, t0)


def _store(store, read_json, entities, relations, comms, chunk_ids, t0):
    chunks = [c for c in read_json("2_chunks.json") if c["chunk_id"] in chunk_ids]
    age = {"stored_in_age": False}
    try:                                   # Apache AGE copy for Cypher queries; answering works without it
        store.reset_graph()
        loaded = store.load(entities, relations, chunks)
        print(f"  graph database copy: {loaded['age_edges']} strongest links in {loaded['age_seconds']}s", flush=True)
        c = store.conn()
        with c.cursor() as cur:
            cur.executemany("UPDATE kg_entities SET community_l0 = %s, community_l1 = %s WHERE id = %s",
                            [(e.get("community_l0"), e.get("community_l1"), e["id"]) for e in entities])
            cur.executemany("INSERT INTO kg_communities (id, level, parent, size, entity_ids, profile) "
                            "VALUES (%s, %s, %s, %s, %s, %s)",
                            [(x["id"], x["level"], x["parent"], len(x["entity_ids"]), x["entity_ids"], x["profile"])
                             for x in comms])
        age = store.stats() | loaded | {"stored_in_age": True}
    except Exception as e:                                         # noqa: BLE001 - reported, not fatal
        age["age_error"] = f"{type(e).__name__}: {str(e).splitlines()[0][:200]}"
        print(f"  graph database not used ({age['age_error']}); answering reads the build file", flush=True)

    deg = sorted(entities, key=lambda e: -e.get("degree", 0))
    report = age | {
        "nodes": len(entities), "edges": len(relations),
        "entities": len(entities), "relations": len(relations), "chunks": len(chunks),
        "entities_by_type": dict(Counter(e["type"] for e in entities).most_common()),
        "relations_by_type": dict(Counter(r["type"] for r in relations).most_common()),
        "isolated_entities": sum(1 for e in entities if not e.get("degree")),
        "from_tables": sum(1 for e in entities if "table" in e["origin"]),
        "linked_table_and_document": [e["name"] for e in entities if len(e["origin"]) == 2][:50],
        "top_degree": [(e["name"], e["type"], e["degree"]) for e in deg[:20]],
        "communities": len(comms),
        "seconds": round(time.perf_counter() - t0, 1),
    }
    write_json("gr_3_graph.json", report)
    write_json("gr_4_communities.json", {"communities": [
        {"id": x["id"], "level": x["level"], "parent": x["parent"], "size": len(x["entity_ids"]),
         "profile": x["profile"][:300]} for x in comms]})
    print(f"  graph: {report['nodes']} entities / {report['edges']} links / {len(comms)} communities; "
          f"{report['isolated_entities']} isolated; {report['seconds']}s", flush=True)
    return report


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--no-tables", action="store_true")
    ap.add_argument("--reuse", action="store_true", help="skip merging; reload outputs/_graph_build.json")
    a = ap.parse_args()
    build(a.paths, tables=not a.no_tables, reuse=a.reuse)
