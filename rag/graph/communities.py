"""g3 - Community detection with Leiden (NetworkX's built-in implementation, BSD licence - no GPL
igraph/leidenalg needed). Hierarchical: level 0 runs on the whole graph; any community larger than
MAX_COMMUNITY_SIZE is split again into level-1 communities, and so on.

Output: outputs/g3_communities.json
"""
import networkx as nx

from ..config import COMMUNITY_MIN_SIZE, LEIDEN_RESOLUTION, MAX_COMMUNITY_SIZE
from ..utils import read_json, write_json

SEED = 42


def build_nx(res: dict) -> nx.Graph:
    g = nx.Graph()
    for e in res["entities"]:
        g.add_node(e["id"], name=e["name"], type=e["type"])
    for r in res["relations"]:
        w = r["weight"] * r["strength"] / 10
        if g.has_edge(r["source"], r["target"]):
            g[r["source"]][r["target"]]["weight"] += w
        else:
            g.add_edge(r["source"], r["target"], weight=w)
    return g


def _leiden(g: nx.Graph) -> list[set]:
    if g.number_of_edges() == 0:
        return [{n} for n in g.nodes]
    # metric="modularity": NetworkX defaults to CPM, which with resolution 1.0 and edge weights
    # below 1 leaves every node in its own community
    return [set(c) for c in nx.community.leiden_communities(g, weight="weight", resolution=LEIDEN_RESOLUTION,
                                                            metric="modularity", seed=SEED)]


def detect_all() -> dict:
    res = read_json("g2_resolution.json")
    g = build_nx(res)
    communities, counter = [], [0]

    def split(nodes: set, level: int, parent: str | None):
        parts = _leiden(g.subgraph(nodes)) if level else _leiden(g)
        if level and len(parts) <= 1:
            return []
        ids = []
        for part in sorted(parts, key=len, reverse=True):
            cid = f"c{level}_{counter[0]}"
            counter[0] += 1
            sub = g.subgraph(part)
            communities.append({"id": cid, "level": level, "parent": parent, "size": len(part),
                                "entity_ids": sorted(part), "internal_edges": sub.number_of_edges(),
                                "children": []})
            ids.append(cid)
            if len(part) > MAX_COMMUNITY_SIZE:
                communities[[c["id"] for c in communities].index(cid)]["children"] = split(part, level + 1, cid)
        return ids

    split(set(g.nodes), 0, None)
    top = [c for c in communities if c["level"] == 0]
    stats = {"entities": g.number_of_nodes(), "edges": g.number_of_edges(),
             "levels": 1 + max((c["level"] for c in communities), default=0),
             "communities": len(communities),
             "level0_communities": len(top),
             "reportable": sum(c["size"] >= COMMUNITY_MIN_SIZE for c in communities),
             "modularity_level0": round(nx.community.modularity(g, [set(c["entity_ids"]) for c in top],
                                                                weight="weight"), 3) if g.number_of_edges() else None}
    write_json("g3_communities.json", {"stats": stats, "communities": communities})
    return stats
