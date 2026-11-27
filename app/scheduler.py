"""What runs on its own: source syncs when they are due and the nightly metric check. A thread looks every
CL_SCHEDULER_TICK_S seconds (default 30) and queues the due work as jobs.

With several processes (API and worker containers, or several workers) one of them schedules: a Postgres advisory
lock held on its own connection picks the leader; if it stops, another takes over at its next tick.
CL_SCHEDULER=0 turns it off (tests, or a process that should only answer requests; CL_ROLE=api does the same).
"""
import os
import sys
import threading
import traceback

TICK_S = int(os.environ.get("CL_SCHEDULER_TICK_S", "30"))
LEADER_LOCK = 727003
DUE: list = []                      # fn() -> number of jobs queued; each feature adds one
_stop = threading.Event()
_thread: threading.Thread | None = None
_leader_conn = None


def _sources_due() -> int:
    from sources import manage
    return manage.run_due()


DUE.append(_sources_due)


def _metric_check_due() -> int:
    from semantic import monitor
    return monitor.due()


DUE.append(_metric_check_due)


def is_leader() -> bool:
    """True in the one process that schedules (always true without Postgres: one process)."""
    global _leader_conn
    from app import db as D
    if not D.postgres():
        return True
    try:
        import psycopg
        if _leader_conn is None or _leader_conn.closed:
            _leader_conn = psycopg.connect(D.url(), autocommit=True)
        held = _leader_conn.execute("SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND objid = %s AND "
                                    "pid = pg_backend_pid() AND granted", (LEADER_LOCK,)).fetchone()
        if held:                                  # already the leader (do not stack the session lock)
            return True
        return bool(_leader_conn.execute("SELECT pg_try_advisory_lock(%s)", (LEADER_LOCK,)).fetchone()[0])
    except Exception:                             # noqa: BLE001 - no database: do not schedule
        _leader_conn = None
        return False


def release_leadership():
    global _leader_conn
    if _leader_conn is not None:
        try:
            _leader_conn.close()                  # closing the session releases the lock
        except Exception:                         # noqa: BLE001
            pass
        _leader_conn = None


def tick() -> int:
    """Queue everything that is due. Returns how many jobs were queued."""
    n = 0
    for check in list(DUE):
        try:
            n += check() or 0
        except Exception:                                  # noqa: BLE001 - one bad check must not stop the rest
            print(traceback.format_exc(), file=sys.__stderr__)
    return n


def _loop():
    while not _stop.wait(TICK_S):
        if is_leader():
            tick()


def start():
    global _thread
    if os.environ.get("CL_SCHEDULER", "1") == "0" or (_thread and _thread.is_alive()):
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="scheduler")
    _thread.start()


def stop():
    _stop.set()
    release_leadership()
