"""The knowledge graph's system of record: plain Postgres tables per workspace (migrations/013_graph.sql).

  kg_entities          one row per merged entity (name, type, aliases, description, degree, communities)
  kg_relations         one row per weighted link (source, target, type, description, strength, passage ids)
  kg_entity_passages   which passages mention which entity
  kg_communities       Leiden communities (level, parent, members, extractive profile)
  graph_state          the build's version: it moves on every build, so the in-memory cache (graph/search.py)
                       knows when to reload

save() replaces the workspace's graph in one transaction and bumps the version; load() returns the graph in the
shape the builder produces ({"entities", "relations", "communities", "chunk_ids"}), every field included (the
full record is kept in a data column, so per-passage evidence and later additions survive the round trip).
On the command line (no workspace) the graph stays in outputs/_graph_build.json, as before.
"""
import json

from .config import OUT_DIR

FILE = "_graph_build.json"
_ENTITY_COLS = ("id", "name", "type", "aliases", "description", "degree", "community_l0", "community_l1", "chunk_ids")
_REL_COLS = ("source_id", "target_id", "type", "description", "weight", "chunk_ids")


def _tenant() -> str | None:
    from rag.wspath import current_workspace
    return current_workspace()


def _j(v):
    return v if isinstance(v, (list, dict)) or v is None else json.loads(v)


def version():
    """The current build's version (a value that changes with every build), or None when there is no graph."""
    t = _tenant()
    if not t:
        p = OUT_DIR / FILE
        return p.stat().st_mtime if p.exists() else None
    from app.db import db
    r = db().execute("SELECT version FROM graph_state WHERE tenant_id = ?", (t,)).fetchone()
    return ("v", t, r["version"]) if r else None


def exists() -> bool:
    return version() is not None


def save(entities: list[dict], relations: list[dict], communities: list[dict], chunk_ids) -> object:
    """Replace the workspace's graph (one transaction) and bump its version."""
    t = _tenant()
    chunk_ids = sorted(chunk_ids)
    if not t:
        (OUT_DIR / FILE).parent.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / FILE).write_text(json.dumps({"entities": entities, "relations": relations, "communities": communities,
                                                "chunk_ids": chunk_ids}, ensure_ascii=False), encoding="utf-8")
        return version()
    from app.core import now
    from app.db import tx
    ts = now()
    with tx() as cur:
        for table in ("kg_entity_passages", "kg_relations", "kg_entities", "kg_communities"):
            cur.execute(f"DELETE FROM {table} WHERE tenant_id = ?", (t,))
        for i in range(0, len(entities), 500):
            batch = entities[i:i + 500]
            _many(cur, "kg_entities", ("tenant_id", "id", "name", "type", "aliases", "description", "degree",
                                       "community_l0", "community_l1", "data"),
                  [(t, e["id"], e.get("name", ""), e.get("type", ""), json.dumps(e.get("aliases") or []),
                    e.get("description") or "", int(e.get("degree") or 0), e.get("community_l0"), e.get("community_l1"),
                    json.dumps({k: v for k, v in e.items() if k not in _ENTITY_COLS}, ensure_ascii=False, default=str))
                   for e in batch], jsonb=("aliases", "data"))
            pairs = [(t, e["id"], c) for e in batch for c in dict.fromkeys(e.get("chunk_ids") or [])]
            for j in range(0, len(pairs), 2000):
                _many(cur, "kg_entity_passages", ("tenant_id", "entity_id", "chunk_id"), pairs[j:j + 2000])
        for i in range(0, len(relations), 500):
            _many(cur, "kg_relations", ("tenant_id", "position", "source", "target", "type", "description", "strength",
                                        "passage_ids", "data"),
                  [(t, i + k, r["source_id"], r["target_id"], r.get("type", ""), r.get("description") or "",
                    float(r.get("weight") or 0), json.dumps(r.get("chunk_ids") or []),
                    json.dumps({x: v for x, v in r.items() if x not in _REL_COLS}, ensure_ascii=False, default=str))
                   for k, r in enumerate(relations[i:i + 500])], jsonb=("passage_ids", "data"))
        for i in range(0, len(communities), 500):
            _many(cur, "kg_communities", ("tenant_id", "id", "level", "parent", "members", "profile", "data"),
                  [(t, int(c["id"]), int(c.get("level") or 0), c.get("parent"), json.dumps(c.get("entity_ids") or []),
                    c.get("profile") or "",
                    json.dumps({k: v for k, v in c.items() if k not in ("id", "level", "parent", "entity_ids", "profile")},
                               ensure_ascii=False, default=str))
                   for c in communities[i:i + 500]], jsonb=("members", "data"))
        cur.execute("INSERT INTO graph_state (tenant_id, version, chunk_ids, entities, relations, updated_at) "
                    "VALUES (?, 1, ?::jsonb, ?, ?, ?) ON CONFLICT (tenant_id) DO UPDATE SET version = graph_state.version + 1, "
                    "chunk_ids = excluded.chunk_ids, entities = excluded.entities, relations = excluded.relations, "
                    "updated_at = excluded.updated_at", (t, json.dumps(chunk_ids), len(entities), len(relations), ts))
    return version()


def _many(cur, table: str, cols: tuple, rows: list, jsonb: tuple = ()):
    if not rows:
        return
    ph = "(" + ", ".join("?::jsonb" if c in jsonb else "?" for c in cols) + ")"
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES " + ", ".join([ph] * len(rows))
    cur.execute(sql, [v for r in rows for v in r])


def load() -> dict | None:
    """The workspace's graph in the builder's shape, or None."""
    t = _tenant()
    if not t:
        p = OUT_DIR / FILE
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    from app.db import tx
    with tx() as cur:                                    # one snapshot: a build running meanwhile is not half-seen
        st = cur.execute("SELECT chunk_ids FROM graph_state WHERE tenant_id = ?", (t,)).fetchone()
        if not st:
            return None
        chunks: dict[str, list] = {}
        for r in cur.execute("SELECT entity_id, chunk_id FROM kg_entity_passages WHERE tenant_id = ?", (t,)):
            chunks.setdefault(r["entity_id"], []).append(r["chunk_id"])
        entities = []
        for r in cur.execute("SELECT * FROM kg_entities WHERE tenant_id = ? ORDER BY id", (t,)):
            e = {"id": r["id"], "name": r["name"], "type": r["type"], "aliases": _j(r["aliases"]) or [],
                 "description": r["description"], "degree": r["degree"], "community_l0": r["community_l0"],
                 "community_l1": r["community_l1"], "chunk_ids": sorted(chunks.get(r["id"], []))}
            entities.append(e | (_j(r["data"]) or {}))
        relations = [{"source_id": r["source"], "target_id": r["target"], "type": r["type"],
                      "description": r["description"], "weight": r["strength"],
                      "chunk_ids": _j(r["passage_ids"]) or []} | (_j(r["data"]) or {})
                     for r in cur.execute("SELECT * FROM kg_relations WHERE tenant_id = ? ORDER BY position", (t,))]
        comms = [{"id": r["id"], "level": r["level"], "parent": r["parent"], "entity_ids": _j(r["members"]) or [],
                  "profile": r["profile"]} | (_j(r["data"]) or {})
                 for r in cur.execute("SELECT * FROM kg_communities WHERE tenant_id = ? ORDER BY id", (t,))]
    return {"entities": entities, "relations": relations, "communities": comms, "chunk_ids": _j(st["chunk_ids"]) or []}


def stats() -> dict:
    t = _tenant()
    if not t:
        g = load()
        return {"entities": len(g["entities"]), "relations": len(g["relations"])} if g else {}
    from app.db import db
    r = db().execute("SELECT entities, relations, updated_at, version FROM graph_state WHERE tenant_id = ?", (t,)).fetchone()
    return dict(r) if r else {}


def backfill() -> dict:
    """0.7 upgrade: a workspace's outputs/_graph_build.json moved into the graph tables (the file is kept as
    _graph_build.json.pre-0.7 until the cleanup). Idempotent."""
    t = _tenant()
    p = OUT_DIR / FILE
    if not t or not p.exists():
        return {"entities": 0}
    g = json.loads(p.read_text(encoding="utf-8"))
    save(g["entities"], g["relations"], g.get("communities", []), g.get("chunk_ids", []))
    p.replace(p.with_name(FILE + ".pre-0.7"))
    return {"entities": len(g["entities"]), "relations": len(g["relations"])}
