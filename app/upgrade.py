"""Automatic, idempotent upgrade of existing workspaces to the 0.7 layout, run at every start (after migrations).

Each step backfills the system of record and the landing zone from what older versions left in a workspace's
folder, and does nothing when there is nothing left to do, so running it twice changes nothing:
  landing    CSV tables -> Parquet, a manifest row for every landed file          (app/landing.py)
  passages   outputs/2_chunks.json -> Postgres table passages                     (rag/passages.py)
  graph      outputs/_graph_build.json -> Postgres graph tables                    (graph/kg.py)
One process at a time runs it (a Postgres advisory lock), so an API and a worker container starting together never
race.

python -m app.upgrade drop-age removes what Apache AGE left in a database created before 0.7 (the per-workspace graph
copies and the extension). It refuses while any workspace still has a _graph_build.json to move into the graph
tables, and it needs the database owner (it runs with CL_DATABASE_URL's user, like the migrations).
"""
import sys
import traceback

LOCK_ID = 727002


def _steps():
    from graph import kg
    from rag import passages
    from . import landing
    return [("landing", landing.backfill), ("passages", passages.backfill), ("graph", kg.backfill)]


def run_workspace(tenant_id: str) -> dict:
    from .db import tenant_scope
    out = {}
    with tenant_scope(tenant_id):
        for name, fn in _steps():
            try:
                r = fn()
            except Exception as e:                         # noqa: BLE001 - one step failing must not stop the app
                traceback.print_exc()
                r = {"error": f"{type(e).__name__}: {e}"[:300]}
            if r and any(v for v in r.values()):
                out[name] = r
    return out


def drop_age() -> dict:
    """Drop the Apache AGE graph copies and the extension (the product no longer uses them). Idempotent."""
    import psycopg
    from rag.wspath import files_root
    left = sorted(str(p) for p in files_root().glob("*/outputs/_graph_build.json"))
    if left:
        raise SystemExit("Some workspaces still have a graph build file to move into the graph tables; start the app "
                         "once (it moves them) and run this again: " + ", ".join(left[:5]))
    from .db import url
    out = {"schemas": [], "extension": False}
    with psycopg.connect(url(), autocommit=True) as c:
        has_catalog = c.execute("SELECT 1 FROM pg_namespace WHERE nspname = 'ag_catalog'").fetchone()
        if has_catalog:
            for (name,) in c.execute("SELECT name::text FROM ag_catalog.ag_graph").fetchall():
                try:                                   # AGE still installed: let it drop its own graph
                    c.execute("LOAD 'age'")
                    c.execute("SELECT ag_catalog.drop_graph(%s, true)", (name,))
                except psycopg.Error:                  # the official image (no AGE library): drop the schema
                    c.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
                out["schemas"].append(name)
        for (name,) in c.execute("SELECT nspname FROM pg_namespace WHERE nspname LIKE 'kg\\_t\\_%'").fetchall():
            c.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')            # the kg_* side tables of each copy
            out["schemas"].append(name)
        if c.execute("SELECT 1 FROM pg_extension WHERE extname = 'age'").fetchone():
            c.execute("DROP EXTENSION age CASCADE")
            out["extension"] = True
        c.execute("DROP SCHEMA IF EXISTS ag_catalog CASCADE")
    return out


def run() -> dict:
    """Upgrade every workspace. Returns {workspace: {step: result}} for the workspaces that changed."""
    import psycopg
    from .db import db, system, url
    with psycopg.connect(url(), autocommit=True) as lock:
        lock.execute("SELECT pg_advisory_lock(%s)", (LOCK_ID,))
        try:
            with system():
                tenants = [r["id"] for r in db().execute("SELECT id FROM tenants ORDER BY created")]
            done = {}
            for t in tenants:
                r = run_workspace(t)
                if r:
                    done[t] = r
            if done:
                from .core import log_event
                for t, r in done.items():
                    from .db import tenant_scope
                    with tenant_scope(t):
                        log_event("system", "upgrade", "upgrade.0.7", "workspace", r)
                print(f"0.7 upgrade: {done}")
            return done
        finally:
            lock.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))
if __name__ == "__main__":
    if sys.argv[1:] == ["drop-age"]:
        print(f"removed: {drop_age()}")
    elif sys.argv[1:] in ([], ["run"]):
        print(f"upgraded: {run() or 'nothing to do'}")
    else:
        raise SystemExit("usage: python -m app.upgrade [run | drop-age]")
