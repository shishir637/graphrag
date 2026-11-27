"""Graph storage in PostgreSQL + Apache AGE.

  graph 'india_kg' (openCypher):  one vertex label per entity type (State, City, Dynasty, ...),
                            one edge label per relation type (RULED, LOCATED_IN, ...)
  SQL tables (same DB):     kg_entities, kg_entity_chunks, kg_communities, kg_chunks

All values reach Cypher as a JSON parameter ($rows), never pasted into the query text, so
names containing quotes or '$$' cannot break or inject into a query.
"""
import json
import re
from collections import defaultdict

import psycopg

from graph.config import GRAPH_NAME as DEFAULT_GRAPH, PG_DSN

_conn = None


def graph_name() -> str:
    """Each workspace has its own AGE graph and kg_* tables (schema kg_<workspace>); 'india_kg' + public outside."""
    from rag.wspath import tenant_key
    t = tenant_key()
    return f"kg_{re.sub(r'[^a-z0-9_]', '_', t.lower())}" if t else DEFAULT_GRAPH


def _schema() -> str:
    from rag.wspath import tenant_key
    return graph_name() if tenant_key() else "public"
_LABEL = re.compile(r"^[A-Z][A-Za-z0-9_]*$")


def conn() -> psycopg.Connection:
    global _conn
    if _conn is None or _conn.closed:
        _conn = psycopg.connect(PG_DSN, autocommit=True)
        _conn.execute("CREATE EXTENSION IF NOT EXISTS age")
        _conn.execute("LOAD 'age'")
    sch = _schema()
    _conn.execute(f'SET search_path = "{sch}", ag_catalog')        # kg_* tables are created and read in sch
    return _conn


def _label(name: str) -> str:
    if not _LABEL.match(name):                     # labels cannot be parameters - whitelist their shape
        raise ValueError(f"invalid label {name!r}")
    return name


def cypher(query: str, params: dict | None = None, columns: tuple[str, ...] = ("r",)) -> list[tuple]:
    """Run openCypher through AGE; params are passed as one agtype JSON parameter."""
    cols = ", ".join(f"{c} agtype" for c in columns)
    sql = f"SELECT * FROM cypher('{graph_name()}', $$ {query} $$" + (", %s" if params is not None else "") + f") AS ({cols})"
    rows = conn().execute(sql, (json.dumps(params),) if params is not None else None,
                          prepare=params is not None).fetchall()
    return [tuple(_agtype(v) for v in row) for row in rows]


def _agtype(v):
    if v is None:
        return None
    s = re.sub(r"::(vertex|edge|path)$", "", str(v))
    try:
        return json.loads(s)
    except ValueError:
        return s


def reset_graph():
    c = conn()
    if c.execute("SELECT count(*) FROM ag_graph WHERE name = %s", (graph_name(),)).fetchone()[0]:
        c.execute("SELECT drop_graph(%s, true)", (graph_name(),))
    c.execute("SELECT create_graph(%s)", (graph_name(),))
    c.execute("""
        DROP TABLE IF EXISTS kg_entities, kg_entity_chunks, kg_communities, kg_chunks;
        CREATE TABLE kg_entities (id text PRIMARY KEY, name text, type text, description text,
                                         aliases text[], degree int, community_l0 int, community_l1 int);
        CREATE TABLE kg_entity_chunks (entity_id text, chunk_id text, PRIMARY KEY (entity_id, chunk_id));
        CREATE TABLE kg_communities (id int PRIMARY KEY, level int, parent int, size int,
                                            entity_ids text[], profile text,
                                            title text, summary text, summarised_at timestamptz);
        CREATE TABLE kg_chunks (chunk_id text PRIMARY KEY, source text, headings text[], text text);
    """)


AGE_MAX_EDGES = 20000          # the Cypher copy keeps the strongest links; answering reads the full build file
AGE_TIME_BUDGET_S = 180        # never let the copy block the app for long


def load(entities: list[dict], relations: list[dict], chunks: list[dict], batch: int = 500) -> dict:
    """Bulk load: vertices grouped by label, edges grouped by (type, source label, target label) so each MATCH
    uses that label's id index. Only links seen in 2+ sentences (or typed links) are copied, strongest first,
    up to AGE_MAX_EDGES, within AGE_TIME_BUDGET_S. Returns what was loaded."""
    import time as _t
    t0 = _t.perf_counter()
    c = conn()
    label_of = {e["id"]: _label(e["type"]) for e in entities}
    by_type: dict[str, list] = {}
    for e in entities:
        by_type.setdefault(e["type"], []).append(
            {"id": e["id"], "name": e["name"], "description": e["description"][:1500], "degree": e.get("degree", 0)})
    for typ, rows in by_type.items():
        c.execute("SELECT create_vlabel(%s, %s)", (graph_name(), _label(typ)))
        for i in range(0, len(rows), batch):
            cypher(f"UNWIND $rows AS r CREATE (:{_label(typ)} {{id: r.id, name: r.name, description: r.description, "
                   f"degree: r.degree}})", {"rows": rows[i:i + batch]})
        # property index on id for this label (AGE stores properties as agtype)
        c.execute(f'CREATE INDEX IF NOT EXISTS "{typ}_id" ON {graph_name()}."{_label(typ)}" '
                  f"(ag_catalog.agtype_access_operator(properties, '\"id\"'::agtype))")

    keep = [r for r in relations if r["type"] != "MENTIONS" and (r["type"] != "RELATED_TO" or r.get("count", 1) >= 2)]
    keep = sorted(keep, key=lambda r: -float(r.get("weight") or 0))[:AGE_MAX_EDGES]
    groups: dict[tuple, list] = {}
    for r in keep:
        key = (_label(r["type"]), label_of.get(r["source_id"]), label_of.get(r["target_id"]))
        if key[1] and key[2]:
            groups.setdefault(key, []).append(
                {"s": r["source_id"], "t": r["target_id"], "weight": r["weight"], "mentions": r["count"],
                 "description": r["description"][:800], "chunk_ids": r["chunk_ids"][:20]})
    loaded, stopped = 0, False
    made = set()
    for (typ, la, lb), rows in groups.items():
        if typ not in made:
            c.execute("SELECT create_elabel(%s, %s)", (graph_name(), typ))
            made.add(typ)
        for i in range(0, len(rows), batch):
            if _t.perf_counter() - t0 > AGE_TIME_BUDGET_S:
                stopped = True
                break
            cypher(f"UNWIND $rows AS r MATCH (a:{la} {{id: r.s}}), (b:{lb} {{id: r.t}}) "
                   f"CREATE (a)-[:{typ} {{weight: r.weight, mentions: r.mentions, description: r.description, "
                   f"chunk_ids: r.chunk_ids}}]->(b)", {"rows": rows[i:i + batch]})
            loaded += len(rows[i:i + batch])
        if stopped:
            break

    with c.cursor() as cur:
        cur.executemany("INSERT INTO kg_entities (id, name, type, description, aliases, degree) "
                        "VALUES (%s, %s, %s, %s, %s, %s)",
                        [(e["id"], e["name"], e["type"], e["description"], e.get("aliases", []), e.get("degree", 0))
                         for e in entities])
        cur.executemany("INSERT INTO kg_entity_chunks VALUES (%s, %s) ON CONFLICT DO NOTHING",
                        [(e["id"], cid) for e in entities for cid in e["chunk_ids"]])
        cur.executemany("INSERT INTO kg_chunks VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                        [(ch["chunk_id"], ch["source"], ch["headings"], ch["text"]) for ch in chunks])
    return {"age_vertices": len(entities), "age_edges": loaded, "age_edges_skipped": len(relations) - loaded,
            "age_stopped_at_time_budget": stopped, "age_seconds": round(_t.perf_counter() - t0, 1)}


def neighbourhood(seed_ids: list[str], limit: int = 200) -> list[dict]:
    """1-hop edges around the seeds, plus 2-hop paths that connect two seeds.

    Uses AGE's own storage directly: the seeds' internal graph ids are looked up once, then their edges
    are read through the start_id/end_id indexes AGE keeps on every edge table. The equivalent Cypher
    (MATCH (a)-[r]-(b) WHERE a.id IN [...]) matches on a JSON property across all vertex tables and took
    ~35 s on this graph; this takes ~0.1 s. Connecting paths (seed - m - other seed) are found in Python
    from the same 1-hop edges, so no second, more expensive query is needed."""
    if not seed_ids:
        return []
    c = conn()
    g = graph_name()
    gid_rows = c.execute(
        f"SELECT id::text, properties::text FROM {g}._ag_label_vertex "
        f"WHERE ag_catalog.agtype_access_operator(properties, '\"id\"'::agtype) = ANY(%s::agtype[])",
        ([json.dumps(s) for s in seed_ids],)).fetchall()
    gids = [r[0] for r in gid_rows]
    rows = c.execute(f"""
        SELECT e.tableoid::regclass::text, e.properties::text, vs.properties::text, ve.properties::text
        FROM {g}._ag_label_edge e
        JOIN {g}._ag_label_vertex vs ON vs.id = e.start_id
        JOIN {g}._ag_label_vertex ve ON ve.id = e.end_id
        WHERE e.start_id = ANY(%s::graphid[]) OR e.end_id = ANY(%s::graphid[])""", (gids, gids)).fetchall()
    seeds = set(seed_ids)
    one, touching = [], defaultdict(list)          # middle entity -> edges linking it to seeds
    for table, eprops, sprops, tprops in rows:
        ep, sp, tp = json.loads(eprops), json.loads(sprops), json.loads(tprops)
        edge = {"source": sp["name"], "type": table.split(".", 1)[1].strip('"'), "target": tp["name"],
                "weight": float(ep.get("weight") or 1), "description": ep.get("description"),
                "chunk_ids": ep.get("chunk_ids") or [], "hop": 1, "ids": (sp["id"], tp["id"])}
        one.append(edge)
        for seed, other in ((sp, tp), (tp, sp)):
            if seed["id"] in seeds and other["id"] not in seeds:
                touching[other["id"]].append((seed["id"], edge, other["name"]))
    two = []
    for mid, links in touching.items():
        if len({s for s, _, _ in links}) < 2:
            continue                                 # a connecting path needs two different seeds
        for _, edge, mname in links[:4]:
            two.append(edge | {"hop": 2, "weight": 5.0, "via": mname})
    one.sort(key=lambda e: -e["weight"])
    return two[:100] + one[:limit]


def stats() -> dict:
    c = conn()
    nodes = cypher("MATCH (n) RETURN count(n)")[0][0]
    edges = cypher("MATCH ()-[r]->() RETURN count(r)")[0][0]
    by_label = c.execute("SELECT type, count(*) FROM kg_entities GROUP BY type ORDER BY 2 DESC").fetchall()
    return {"nodes": nodes, "edges": edges, "entities_by_type": dict(by_label)}
