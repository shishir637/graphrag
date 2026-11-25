"""What runs on its own: connector syncs when they are due, the nightly semantic-layer check, and the weekly
review of questions the layer could not serve. One app process schedules (a Postgres advisory lock picks it);
the work itself is queued as jobs, so any process's workers run it.

CL_SCHEDULER=0 turns it off (e.g. for a process that should only serve requests).
"""
import os
import sys
import threading
import traceback
from datetime import datetime, timedelta, timezone

from . import core, jobs
from .db import system, tenant_scope

TICK_S = int(os.environ.get("CL_SCHEDULER_TICK_S", "30"))
MONITOR_EVERY = timedelta(hours=int(os.environ.get("CL_MONITOR_HOURS", "24")))
LEARN_EVERY = timedelta(days=7)
DUE: list = []                      # extra checks: fn() -> list of (tenant_id, kind, label, info); connectors add one
_stop = threading.Event()
_thread: threading.Thread | None = None
_leader = None


def _ago(ts: str | None, every: timedelta) -> bool:
    if not ts:
        return True
    try:
        return datetime.now(timezone.utc) - datetime.fromisoformat(ts) >= every
    except ValueError:
        return True


def periodic() -> list[tuple]:
    """(tenant, kind, label, info) for the nightly check and weekly learning that are due."""
    out = []
    with system():
        tenants = [r["id"] for r in core.db().execute("SELECT id FROM tenants WHERE NOT disabled")]
    for t in tenants:
        with tenant_scope(t):
            has_layer = core.db().execute("SELECT 1 FROM sl_definitions WHERE tenant_id = ? AND kind = 'metric' LIMIT 1",
                                          (t,)).fetchone()
            if has_layer and _ago(core.get_setting("monitor_last"), MONITOR_EVERY):
                out.append((t, "monitor", "Checking metric definitions", {}))
            if has_layer and _ago(core.get_setting("learn_last"), LEARN_EVERY):
                out.append((t, "learn", "Suggesting synonyms from unanswered questions", {}))
    return out


def tick() -> int:
    """Queue everything that is due (once per kind and workspace). Returns how many jobs were queued."""
    n = 0
    due = periodic()
    for check in DUE:
        try:
            due += check()
        except Exception:                                  # noqa: BLE001 - one bad check must not stop the rest
            print(traceback.format_exc(), file=sys.__stderr__)
    for t, kind, label, info in due:
        with tenant_scope(t):
            if kind in ("monitor", "learn"):
                core.set_setting(f"{kind}_last", core.now())
            busy = core.db().execute("SELECT 1 FROM jobs WHERE tenant_id = ? AND kind = ? AND status IN ('queued', 'running') "
                                     "AND info = ?::jsonb", (t, kind, __import__("json").dumps(info))).fetchone()
            if not busy:
                jobs.enqueue(kind, label, None, "scheduler", info)
                n += 1
    return n


def _is_leader() -> bool:
    """Hold a session advisory lock on a dedicated connection: only one process schedules."""
    global _leader
    import psycopg
    from .db import url
    try:
        if _leader is None or _leader.closed:
            _leader = psycopg.connect(url(), autocommit=True)
        return bool(_leader.execute("SELECT pg_try_advisory_lock(727003)").fetchone()[0])
    except Exception:                                      # noqa: BLE001
        _leader = None
        return False


def _loop():
    while not _stop.wait(TICK_S):
        try:
            if _is_leader():
                tick()
        except Exception:                                  # noqa: BLE001 - keep scheduling
            print(traceback.format_exc(), file=sys.__stderr__)


def start():
    global _thread
    if os.environ.get("CL_SCHEDULER", "1") == "0" or _thread is not None:
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="cl-scheduler")
    _thread.start()


def stop():
    global _thread, _leader
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=5)
    _thread = None
    if _leader is not None:
        try:
            _leader.close()
        finally:
            _leader = None
