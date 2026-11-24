"""g5 - Graph storage. Apache AGE (a Postgres extension, Apache 2.0) is used purely as storage and
for traversal; all processing happens in Python. "json" mode keeps the same graph in
outputs/g5_graph.json and traverses it with NetworkX, for machines without Postgres.

Graph model (AGE labels):
  (:Entity {id, name, type, aliases, summary, chunk_ids, community})
  (:Chunk {id, source, access})           id == chunk_id in Qdrant and 2_chunks.json
  (:Community {id, level, title, size})
  (Entity)-[:RELATED {type, description, weight, strength, chunk_ids}]->(Entity)
  (Chunk)-[:MENTIONS]->(Entity)
  (Entity)-[:IN_COMMUNITY]->(Community)   lowest level the entity belongs to
  (Community)-[:CHILD_OF]->(Community)

Output: outputs/g5_graph.json (always written; it also feeds the UI)
"""
import json

from ..config import GRAPH_DSN, GRAPH_NAME, GRAPH_STORE
from ..utils import read_json, write_json

BATCH = 500


def _graph_doc() -> dict:
    res = read_json("g2_resolution.json")
    comms = read_json("g3_communities.json")["communities"]
    summ = read_json("g4_summaries.json")
    chunks = read_json("2_chunks.json")
    titles = {r["community"]: r["title"] for r in summ["reports"]}
    leaf = {}
    for c in sorted(comms, key=lambda c: c["level"]):
        for e in c["entity_ids"]:
            leaf[e] = c["id"]
    used_chunks = {ch for e in res["entities"] for ch in e["chunk_ids"]}
    return {
        "entities": [{"id": e["id"], "name": e["name"], "type": e["type"], "aliases": e["aliases"],
                      "summary": summ["entities"][e["id"]]["summary"], "chunk_ids": e["chunk_ids"],
                      "descriptions": e["descriptions"], "community": leaf.get(e["id"])}
                     for e in res["entities"]],
        "relations": [{"source": r["source"], "target": r["target"], "type": r["type"],
                       "description": r["descriptions"][0]["text"], "weight": r["weight"],
                       "strength": r["strength"], "chunk_ids": r["chunk_ids"]} for r in res["relations"]],
        "chunks": [{"id": c["chunk_id"], "source": c["source"], "access": c["access"]}
                   for c in chunks if c["chunk_id"] in used_chunks],
        "communities": [{"id": c["id"], "level": c["level"], "size": c["size"], "parent": c["parent"],
                         "title": titles.get(c["id"], "")} for c in comms],
    }


# ---- Apache AGE -------------------------------------------------------------------------------
class AgeStore:
    def __init__(self, dsn: str = GRAPH_DSN, graph: str = GRAPH_NAME):
        import psycopg
        self.graph = graph
        self.conn = psycopg.connect(dsn, autocommit=True)
        self.conn.execute("LOAD 'age'")
        self.conn.execute('SET search_path = ag_catalog, "$user", public')

    def cypher(self, query: str, params: dict | None = None, cols: tuple = ("v",)):
        """Run Cypher through AGE. Values go in as a bound agtype parameter, never pasted into the query."""
        as_ = ", ".join(f"{c} agtype" for c in cols)
        sql = f"SELECT * FROM cypher('{self.graph}', $$ {query} $$" + (", %s" if params is not None else "") + f") AS ({as_})"
        rows = self.conn.execute(sql, (json.dumps(params),) if params is not None else None).fetchall()
        return [tuple(json.loads(_agtype(v)) for v in row) for row in rows]

    def reset(self):
        exists = self.conn.execute("SELECT count(*) FROM ag_catalog.ag_graph WHERE name = %s", (self.graph,)).fetchone()[0]
        if exists:
            self.conn.execute("SELECT drop_graph(%s, true)", (self.graph,))
        self.conn.execute("SELECT create_graph(%s)", (self.graph,))
        for label in ("Entity", "Chunk", "Community"):
            self.conn.execute("SELECT create_vlabel(%s, %s)", (self.graph, label))
        for label in ("RELATED", "MENTIONS", "IN_COMMUNITY", "CHILD_OF"):
            self.conn.execute("SELECT create_elabel(%s, %s)", (self.graph, label))
        # property index on id: AGE stores properties as agtype; a GIN index makes {id: ...} lookups fast
        for label in ("Entity", "Chunk", "Community"):
            self.conn.execute(f'CREATE INDEX ON {self.graph}."{label}" USING gin (properties)')

    def _batched(self, query: str, rows: list[dict]):
        for i in range(0, len(rows), BATCH):
            self.cypher(query, {"rows": rows[i:i + BATCH]})

    def load(self, doc: dict):
        self.reset()
        ents = [{k: e[k] for k in ("id", "name", "type", "aliases", "summary", "chunk_ids", "community")}
                for e in doc["entities"]]
        self._batched("UNWIND $rows AS r CREATE (:Entity {id: r.id, name: r.name, type: r.type, aliases: r.aliases, "
                      "summary: r.summary, chunk_ids: r.chunk_ids, community: r.community})", ents)
        self._batched("UNWIND $rows AS r CREATE (:Chunk {id: r.id, source: r.source, access: r.access})", doc["chunks"])
        self._batched("UNWIND $rows AS r CREATE (:Community {id: r.id, level: r.level, size: r.size, title: r.title})",
                      doc["communities"])
        self._batched("UNWIND $rows AS r MATCH (a:Entity {id: r.source}), (b:Entity {id: r.target}) "
                      "CREATE (a)-[:RELATED {type: r.type, description: r.description, weight: r.weight, "
                      "strength: r.strength, chunk_ids: r.chunk_ids}]->(b)", doc["relations"])
        mentions = [{"c": ch, "e": e["id"]} for e in doc["entities"] for ch in e["chunk_ids"]]
        self._batched("UNWIND $rows AS r MATCH (c:Chunk {id: r.c}), (e:Entity {id: r.e}) CREATE (c)-[:MENTIONS]->(e)",
                      mentions)
        member = [{"e": e["id"], "c": e["community"]} for e in doc["entities"] if e["community"]]
        self._batched("UNWIND $rows AS r MATCH (e:Entity {id: r.e}), (c:Community {id: r.c}) "
                      "CREATE (e)-[:IN_COMMUNITY]->(c)", member)
        child = [{"c": c["id"], "p": c["parent"]} for c in doc["communities"] if c["parent"]]
        self._batched("UNWIND $rows AS r MATCH (c:Community {id: r.c}), (p:Community {id: r.p}) "
                      "CREATE (c)-[:CHILD_OF]->(p)", child)
        return self.counts()

    def counts(self) -> dict:
        out = {}
        for label in ("Entity", "Chunk", "Community"):
            out[label] = self.cypher(f"MATCH (n:{label}) RETURN count(n)")[0][0]
        for label in ("RELATED", "MENTIONS", "IN_COMMUNITY", "CHILD_OF"):
            out[label] = self.cypher(f"MATCH ()-[r:{label}]->() RETURN count(r)")[0][0]
        return out

    def neighbourhood(self, ids: list[str]) -> list[dict]:
        """RELATED edges touching any of ids (one hop), with the entity on the other side."""
        rows = self.cypher("MATCH (n:Entity)-[r:RELATED]-(m:Entity) WHERE n.id IN $ids "
                           "RETURN startNode(r).id, endNode(r).id, r.type, r.description, r.weight, r.strength, "
                           "r.chunk_ids, m.id", {"ids": ids},
                           cols=("s", "t", "type", "d", "w", "st", "ch", "m"))
        seen, out = set(), []
        for s, t, typ, d, w, st, ch, m in rows:
            if (s, t, typ) in seen:
                continue
            seen.add((s, t, typ))
            out.append({"source": s, "target": t, "type": typ, "description": d, "weight": w, "strength": st,
                        "chunk_ids": ch, "other": m})
        return out

    def entities(self, ids: list[str]) -> list[dict]:
        rows = self.cypher("MATCH (n:Entity) WHERE n.id IN $ids RETURN n.id, n.name, n.type, n.summary, n.chunk_ids, "
                           "n.community", {"ids": ids}, cols=("i", "n", "t", "s", "c", "k"))
        return [{"id": i, "name": n, "type": t, "summary": s, "chunk_ids": c, "community": k} for i, n, t, s, c, k in rows]

    def close(self):
        self.conn.close()


def _agtype(v):
    """AGE returns agtype as text like '"x"' or '{"a": 1}::vertex'; strip the type suffix."""
    if v is None:
        return "null"
    v = str(v)
    for suffix in ("::vertex", "::edge", "::path"):
        if v.endswith(suffix):
            return v[: -len(suffix)]
    return v


# ---- JSON + NetworkX fallback -----------------------------------------------------------------
class JsonStore:
    def __init__(self, doc: dict | None = None):
        self.doc = doc or read_json("g5_graph.json")["graph"]
        self.by_id = {e["id"]: e for e in self.doc["entities"]}
        self.adj = {}
        for r in self.doc["relations"]:
            for a, b in ((r["source"], r["target"]), (r["target"], r["source"])):
                self.adj.setdefault(a, []).append({**r, "other": b})

    def neighbourhood(self, ids):
        seen, out = set(), []
        for i in ids:
            for r in self.adj.get(i, []):
                k = (r["source"], r["target"], r["type"])
                if k not in seen:
                    seen.add(k)
                    out.append(r)
        return out

    def entities(self, ids):
        return [{k: self.by_id[i][k] for k in ("id", "name", "type", "summary", "chunk_ids", "community")}
                for i in ids if i in self.by_id]

    def counts(self):
        return {"Entity": len(self.doc["entities"]), "Chunk": len(self.doc["chunks"]),
                "Community": len(self.doc["communities"]), "RELATED": len(self.doc["relations"])}

    def close(self):
        pass


def open_store():
    return AgeStore() if GRAPH_STORE == "age" else JsonStore()


def store_all() -> dict:
    doc = _graph_doc()
    if GRAPH_STORE == "age":
        st = AgeStore()
        try:
            counts = st.load(doc)
            sample = st.cypher("MATCH (c:Chunk)-[:MENTIONS]->(e:Entity)-[r:RELATED]->(m:Entity) "
                               "RETURN c.id, e.name, r.type, m.name LIMIT 5", cols=("c", "e", "r", "m"))
        finally:
            st.close()
        where = f"Apache AGE graph '{GRAPH_NAME}'"
    else:
        counts, sample, where = JsonStore(doc).counts(), [], "outputs/g5_graph.json (NetworkX)"
    write_json("g5_graph.json", {"stored_in": where, "counts": counts, "sample_paths": sample, "graph": doc})
    return {"stored_in": where, **counts}
