"""What runs on its own: source syncs when they are due (and, later, the nightly metric check). A thread in the
app process looks every CL_SCHEDULER_TICK_S seconds (default 30) and queues the due work as jobs.

CL_SCHEDULER=0 turns it off (tests, or a process that should only answer requests).
"""
import os
import sys
import threading
import traceback

TICK_S = int(os.environ.get("CL_SCHEDULER_TICK_S", "30"))
DUE: list = []                      # fn() -> number of jobs queued; each feature adds one
_stop = threading.Event()
_thread: threading.Thread | None = None


def _sources_due() -> int:
    from sources import manage
    return manage.run_due()


DUE.append(_sources_due)


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
