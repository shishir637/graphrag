"""Upgrade steps that are run by hand.

python -m app.upgrade drop-age   removes what Apache AGE left in a database created before 0.9 (the graph copies, their
                                 kg_* side tables and the extension). 0.9 keeps the graph in plain tables (graph/kg.py)
                                 and Docker uses the official postgres:17 image. Run it once after the app has started
                                 on 0.9 (the first start moves each workspace's graph into the tables). Idempotent.
Ported from dev app/upgrade.py.
"""
import sys


def drop_age() -> dict:
    import psycopg
    from .db import url
    if not url():
        raise SystemExit("Set CL_DATABASE_URL (the app database) first.")
    out = {"schemas": [], "tables": [], "extension": False}
    with psycopg.connect(url(), autocommit=True) as c:
        if c.execute("SELECT 1 FROM pg_namespace WHERE nspname = 'ag_catalog'").fetchone():
            for (name,) in c.execute("SELECT name::text FROM ag_catalog.ag_graph").fetchall():
                try:                                   # AGE still installed: let it drop its own graph
                    c.execute("LOAD 'age'")
                    c.execute("SELECT ag_catalog.drop_graph(%s, true)", (name,))
                except psycopg.Error:                  # the official image (no AGE library): drop the schema
                    c.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
                out["schemas"].append(name)
        for t in ("kg_entities", "kg_communities"):    # the side tables of the AGE copy (not the 0.9 graph_* tables)
            if c.execute("SELECT 1 FROM pg_tables WHERE schemaname = 'public' AND tablename = %s", (t,)).fetchone():
                c.execute(f"DROP TABLE public.{t} CASCADE")
                out["tables"].append(t)
        if c.execute("SELECT 1 FROM pg_extension WHERE extname = 'age'").fetchone():
            c.execute("DROP EXTENSION age CASCADE")
            out["extension"] = True
        c.execute("DROP SCHEMA IF EXISTS ag_catalog CASCADE")
    return out


if __name__ == "__main__":
    if sys.argv[1:] == ["drop-age"]:
        print(f"removed: {drop_age()}")
    else:
        raise SystemExit("usage: python -m app.upgrade drop-age")
