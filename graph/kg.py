"""The knowledge graph's system of record (0.9): plain Postgres tables per workspace, no Apache AGE.

  graph_entities          one row per merged entity (name, type, aliases, description, degree, communities)
  graph_relations         one row per weighted link (source, target, type, description, strength, passage ids)
  graph_entity_passages   which passages mention which entity
  graph_communities       Leiden communities (level, parent, members, extractive profile)
  graph_state             the build's version: it moves on every build, so the in-memory cache knows when to reload

save() replaces the workspace's graph in one transaction and bumps the version; load() returns the graph in the
shape the builder produces ({"entities", "relations", "communities", "chunk_ids"}), every field included (the full
record is kept in a data column, so per-passage evidence and later additions survive the round trip). Answering
(graph/search.py) keeps one loaded graph per workspace and version in memory; networkx() gives the same graph as a
NetworkX MultiDiGraph (cached the same way) for graph algorithms.

Without the Postgres app database (CL_DATABASE_URL), and on the command line, the graph stays in
outputs/_graph_build.json as before. Every workspace table has tenant_id and row-level security (app/db.py).
Ported from dev graph/kg.py (tables renamed graph_*: the old AGE side tables kg_* may still exist in a database).
"""
import json

from .config import OUT_DIR

FILE = "_graph_build.json"
SCHEMA = """
CREATE TABLE IF NOT EXISTS graph_entities (
  id TEXT NOT NULL, name TEXT NOT NULL, type TEXT NOT NULL DEFAULT '', aliases TEXT NOT NULL DEFAULT '[]',
  description TEXT NOT NULL DEFAULT '', degree INTEGER NOT NULL DEFAULT 0, community_l0 INTEGER, community_l1 INTEGER,
  data TEXT NOT NULL DEFAULT '{}'{tenant}, PRIMARY KEY ({tk}id));
CREATE TABLE IF NOT EXISTS graph_relations (
  position INTEGER NOT NULL, source TEXT NOT NULL, target TEXT NOT NULL, type TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '', strength {real} NOT NULL DEFAULT 0, passage_ids TEXT NOT NULL DEFAULT '[]',
  data TEXT NOT NULL DEFAULT '{}'{tenant}, PRIMARY KEY ({tk}position));
CREATE TABLE IF NOT EXISTS graph_entity_passages (
  entity_id TEXT NOT NULL, chunk_id TEXT NOT NULL{tenant}, PRIMARY KEY ({tk}entity_id, chunk_id));
CREATE TABLE IF NOT EXISTS graph_communities (
  id INTEGER NOT NULL, level INTEGER NOT NULL DEFAULT 0, parent INTEGER, members TEXT NOT NULL DEFAULT '[]',
  profile TEXT NOT NULL DEFAULT '', data TEXT NOT NULL DEFAULT '{}'{tenant}, PRIMARY KEY ({tk}id));
CREATE TABLE IF NOT EXISTS graph_state (
  one INTEGER NOT NULL DEFAULT 1, version INTEGER NOT NULL DEFAULT 0, chunk_ids TEXT NOT NULL DEFAULT '[]',
  entities INTEGER NOT NULL DEFAULT 0, relations INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL{tenant},
  PRIMARY KEY ({tk}one));
"""
_ENTITY_COLS = ("id", "name", "type", "aliases", "description", "degree", "community_l0", "community_l1", "chunk_ids")
_REL_COLS = ("source_id", "target_id", "type", "description", "weight", "chunk_ids")
_NX: dict = {}


def in_db() -> bool:
    """The graph lives in Postgres when the app runs on it (not on the command line)."""
    try:
        from app import db as D
    except ImportError:
        return False
    return D.postgres()


def _db():
    from app import core
    return core.db_with(SCHEMA)


def _j(v, default):
    try:
        return json.loads(v) if isinstance(v, str) else (v if v is not None else default)
    except ValueError:
        return default


def _workspace() -> str:
    from rag.wspath import workspace_key
    return workspace_key() or "default"


def version():
    """A value that changes with every build of this workspace's graph, or None when there is no graph."""
    if not in_db():
        p = OUT_DIR / FILE
        return ("file", str(p), p.stat().st_mtime) if p.exists() else None
    r = _db().execute("SELECT version FROM graph_state").fetchone()
    return ("db", _workspace(), r["version"]) if r else None


def exists() -> bool:
    return version() is not None


def save(entities: list[dict], relations: list[dict], communities: list[dict], chunk_ids) -> object:
    """Replace the workspace's graph (one transaction) and bump its version. Without Postgres: the build file."""
    chunk_ids = sorted(chunk_ids)
    if not in_db():
        p = OUT_DIR / FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps({"entities": entities, "relations": relations, "communities": communities,
                                   "chunk_ids": chunk_ids}, ensure_ascii=False), encoding="utf-8")
        import os
        os.replace(tmp, p)
        return version()
    from app import core
    _db()
    with core.tx() as cur:
        for table in ("graph_entity_passages", "graph_relations", "graph_entities", "graph_communities"):
            cur.execute(f"DELETE FROM {table}")
        for i in range(0, len(entities), 500):
            batch = entities[i:i + 500]
            _many(cur, "graph_entities", ("id", "name", "type", "aliases", "description", "degree", "community_l0",
                                          "community_l1", "data"),
                  [(e["id"], e.get("name", ""), e.get("type", ""), json.dumps(e.get("aliases") or []),
                    e.get("description") or "", int(e.get("degree") or 0), e.get("community_l0"), e.get("community_l1"),
                    json.dumps({k: v for k, v in e.items() if k not in _ENTITY_COLS}, ensure_ascii=False, default=str))
                   for e in batch])
            pairs = [(e["id"], c) for e in batch for c in dict.fromkeys(e.get("chunk_ids") or [])]
            for j in range(0, len(pairs), 2000):
                _many(cur, "graph_entity_passages", ("entity_id", "chunk_id"), pairs[j:j + 2000])
        for i in range(0, len(relations), 500):
            _many(cur, "graph_relations", ("position", "source", "target", "type", "description", "strength",
                                           "passage_ids", "data"),
                  [(i + k, r["source_id"], r["target_id"], r.get("type", ""), r.get("description") or "",
                    float(r.get("weight") or 0), json.dumps(r.get("chunk_ids") or []),
                    json.dumps({x: v for x, v in r.items() if x not in _REL_COLS}, ensure_ascii=False, default=str))
                   for k, r in enumerate(relations[i:i + 500])])
        for i in range(0, len(communities), 500):
            _many(cur, "graph_communities", ("id", "level", "parent", "members", "profile", "data"),
                  [(int(c["id"]), int(c.get("level") or 0), c.get("parent"), json.dumps(c.get("entity_ids") or []),
                    c.get("profile") or "",
                    json.dumps({k: v for k, v in c.items() if k not in ("id", "level", "parent", "entity_ids", "profile")},
                               ensure_ascii=False, default=str))
                   for c in communities[i:i + 500]])
        from app import db as D
        cur.execute(f"INSERT INTO graph_state (one, version, chunk_ids, entities, relations, updated_at) "
                    f"VALUES (1, 1, ?, ?, ?, ?) ON CONFLICT {D.ck('one')} DO UPDATE SET version = graph_state.version + 1, "
                    "chunk_ids = excluded.chunk_ids, entities = excluded.entities, relations = excluded.relations, "
                    "updated_at = excluded.updated_at", (json.dumps(chunk_ids), len(entities), len(relations), core.now()))
    return version()


def _many(cur, table: str, cols: tuple, rows: list):
    if not rows:
        return
    ph = "(" + ", ".join("?" for _ in cols) + ")"
    cur.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES " + ", ".join([ph] * len(rows)),
                [v for r in rows for v in r])


def load() -> dict | None:
    """The workspace's graph in the builder's shape, or None."""
    if not in_db():
        p = OUT_DIR / FILE
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    from app import core
    _db()
    with core.tx() as cur:                               # one snapshot: a build running meanwhile is not half-seen
        st = cur.execute("SELECT chunk_ids FROM graph_state").fetchone()
        if not st:
            return None
        chunks: dict[str, list] = {}
        for r in cur.execute("SELECT entity_id, chunk_id FROM graph_entity_passages").fetchall():
            chunks.setdefault(r["entity_id"], []).append(r["chunk_id"])
        entities = []
        for r in cur.execute("SELECT * FROM graph_entities ORDER BY id").fetchall():
            e = {"id": r["id"], "name": r["name"], "type": r["type"], "aliases": _j(r["aliases"], []),
                 "description": r["description"], "degree": r["degree"], "community_l0": r["community_l0"],
                 "community_l1": r["community_l1"], "chunk_ids": sorted(chunks.get(r["id"], []))}
            entities.append(e | _j(r["data"], {}))
        relations = [{"source_id": r["source"], "target_id": r["target"], "type": r["type"],
                      "description": r["description"], "weight": r["strength"],
                      "chunk_ids": _j(r["passage_ids"], [])} | _j(r["data"], {})
                     for r in cur.execute("SELECT * FROM graph_relations ORDER BY position").fetchall()]
        comms = [{"id": r["id"], "level": r["level"], "parent": r["parent"], "entity_ids": _j(r["members"], []),
                  "profile": r["profile"]} | _j(r["data"], {})
                 for r in cur.execute("SELECT * FROM graph_communities ORDER BY id").fetchall()]
    return {"entities": entities, "relations": relations, "communities": comms, "chunk_ids": _j(st["chunk_ids"], [])}


def networkx():
    """The workspace's graph as a NetworkX MultiDiGraph (entity ids as nodes with their fields, links as edges),
    cached per workspace and version."""
    import networkx as nx
    v = version()
    if v is None:
        return None
    if _NX.get(v[:2]) and _NX[v[:2]][0] == v:
        return _NX[v[:2]][1]
    g = load() or {"entities": [], "relations": []}
    G = nx.MultiDiGraph()
    for e in g["entities"]:
        G.add_node(e["id"], **{k: e.get(k) for k in ("name", "type", "degree", "community_l0")})
    for r in g["relations"]:
        G.add_edge(r["source_id"], r["target_id"], type=r.get("type"), weight=r.get("weight", 1))
    _NX[v[:2]] = (v, G)
    return G


def stats() -> dict:
    if not in_db():
        g = load()
        return {"entities": len(g["entities"]), "relations": len(g["relations"])} if g else {}
    r = _db().execute("SELECT entities, relations, updated_at, version FROM graph_state").fetchone()
    return dict(r) if r else {}


def backfill() -> dict:
    """Upgrade: a workspace's outputs/_graph_build.json copied into the graph tables (the file stays as the build
    cache). Idempotent: does nothing when the tables already hold a graph."""
    p = OUT_DIR / FILE
    if not in_db() or not p.exists() or version() is not None:
        return {"entities": 0}
    g = json.loads(p.read_text(encoding="utf-8"))
    save(g["entities"], g["relations"], g.get("communities", []), g.get("chunk_ids", []))
    return {"entities": len(g["entities"]), "relations": len(g["relations"])}
