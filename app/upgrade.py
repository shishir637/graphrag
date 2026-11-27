"""Automatic, idempotent upgrade of existing workspaces to the 0.7 layout, run at every start (after migrations).

Each step backfills the system of record and the landing zone from what older versions left in a workspace's
folder, and does nothing when there is nothing left to do, so running it twice changes nothing:
  landing    CSV tables -> Parquet, a manifest row for every landed file          (app/landing.py)
Steps added by later phases are listed in STEPS. One process at a time runs it (a Postgres advisory lock), so an
API and a worker container starting together never race.
"""
import traceback

LOCK_ID = 727002


def _steps():
    from . import landing
    return [("landing", landing.backfill)]


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
