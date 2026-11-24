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

from .config import GRAPH_NAME, PG_DSN

_conn = None
_LABEL = re.compile(r"^[A-Z][A-Za-z0-9_]*$")


def conn() -> psycopg.Connection:
    global _conn
    if _conn is None or _conn.closed:
        _conn = psycopg.connect(PG_DSN, autocommit=True)
        _conn.execute("CREATE EXTENSION IF NOT EXISTS age")
        _conn.execute("LOAD 'age'")
        _conn.execute('SET search_path = ag_catalog, "$user", public')
    return _conn


def _label(name: str) -> str:
    if not _LABEL.match(name):                     # labels cannot be parameters - whitelist their shape
        raise ValueError(f"invalid label {name!r}")
    return name


def cypher(query: str, params: dict | None = None, columns: tuple[str, ...] = ("r",)) -> list[tuple]:
    """Run openCypher through AGE; params are passed as one agtype JSON parameter."""
    cols = ", ".join(f"{c} agtype" for c in columns)
    sql = f"SELECT * FROM cypher('{GRAPH_NAME}', $$ {query} $$" + (", %s" if params is not None else "") + f") AS ({cols})"
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
    if c.execute("SELECT count(*) FROM ag_graph WHERE name = %s", (GRAPH_NAME,)).fetchone()[0]:
        c.execute("SELECT drop_graph(%s, true)", (GRAPH_NAME,))
    c.execute("SELECT create_graph(%s)", (GRAPH_NAME,))
    c.execute("""
        DROP TABLE IF EXISTS public.kg_entities, public.kg_entity_chunks, public.kg_communities, public.kg_chunks;
        CREATE TABLE public.kg_entities (id text PRIMARY KEY, name text, type text, description text,
                                         aliases text[], degree int, community_l0 int, community_l1 int);
        CREATE TABLE public.kg_entity_chunks (entity_id text, chunk_id text, PRIMARY KEY (entity_id, chunk_id));
        CREATE TABLE public.kg_communities (id int PRIMARY KEY, level int, parent int, size int,
                                            entity_ids text[], profile text,
                                            title text, summary text, summarised_at timestamptz);
        CREATE TABLE public.kg_chunks (chunk_id text PRIMARY KEY, source text, headings text[], text text);
    """)


def load(entities: list[dict], relations: list[dict], chunks: list[dict], batch: int = 500):
    """Bulk load: vertices grouped by label, edges grouped by type, UNWIND per batch."""
    c = conn()
    by_type: dict[str, list] = {}
    for e in entities:
        by_type.setdefault(e["type"], []).append(
            {"id": e["id"], "name": e["name"], "description": e["description"][:1500], "degree": e.get("degree", 0)})
    for typ, rows in by_type.items():
        c.execute("SELECT create_vlabel(%s, %s)", (GRAPH_NAME, _label(typ)))
        for i in range(0, len(rows), batch):
            cypher(f"UNWIND $rows AS r CREATE (:{_label(typ)} {{id: r.id, name: r.name, description: r.description, "
                   f"degree: r.degree}})", {"rows": rows[i:i + batch]})
        # property index on id for this label (AGE stores properties as agtype)
        c.execute(f'CREATE INDEX IF NOT EXISTS "{typ}_id" ON {GRAPH_NAME}."{typ}" '
                  f"(ag_catalog.agtype_access_operator(properties, '\"id\"'::agtype))")

    by_rel: dict[str, list] = {}
    for r in relations:
        by_rel.setdefault(r["type"], []).append(
            {"s": r["source_id"], "t": r["target_id"], "weight": r["weight"], "mentions": r["count"],
             "description": r["description"][:800], "chunk_ids": r["chunk_ids"][:20]})
    for typ, rows in by_rel.items():
        c.execute("SELECT create_elabel(%s, %s)", (GRAPH_NAME, _label(typ)))
        for i in range(0, len(rows), batch):
            cypher(f"UNWIND $rows AS r MATCH (a {{id: r.s}}), (b {{id: r.t}}) "
                   f"CREATE (a)-[:{_label(typ)} {{weight: r.weight, mentions: r.mentions, description: r.description, "
                   f"chunk_ids: r.chunk_ids}}]->(b)", {"rows": rows[i:i + batch]})

    with c.cursor() as cur:
        cur.executemany("INSERT INTO public.kg_entities (id, name, type, description, aliases, degree) "
                        "VALUES (%s, %s, %s, %s, %s, %s)",
                        [(e["id"], e["name"], e["type"], e["description"], e.get("aliases", []), e.get("degree", 0))
                         for e in entities])
        cur.executemany("INSERT INTO public.kg_entity_chunks VALUES (%s, %s) ON CONFLICT DO NOTHING",
                        [(e["id"], cid) for e in entities for cid in e["chunk_ids"]])
        cur.executemany("INSERT INTO public.kg_chunks VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                        [(ch["chunk_id"], ch["source"], ch["headings"], ch["text"]) for ch in chunks])


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
    g = GRAPH_NAME
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
    by_label = c.execute("SELECT type, count(*) FROM public.kg_entities GROUP BY type ORDER BY 2 DESC").fetchall()
    return {"nodes": nodes, "edges": edges, "entities_by_type": dict(by_label)}
