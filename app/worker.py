"""The worker process (0.9): runs queued jobs (builds, source syncs, quality runs, the nightly metric check) and the
schedule, next to API processes that only answer (CL_ROLE=api). Needs the Postgres app database (the queue is the
jobs table, app/jobqueue.py) and the same file volumes as the API.

    python -m app.worker            (docker compose: the worker service; CL_ROLE=worker)
CL_WORKERS: job threads in this process (default 1; each workspace still runs one job at a time).
"""
import os
import signal
import sys
import time


def main():
    from . import core, jobqueue, scheduler
    from . import db as D
    if not D.postgres():
        raise SystemExit("The worker needs the Postgres app database (CL_DATABASE_URL); with SQLite the app runs jobs itself.")
    os.environ.setdefault("CL_ROLE", "worker")
    from . import vault
    vault.check_startup()                                     # CL_ENV=production: a master key must be configured
    core.load_secrets()
    core.db()                                                 # tables, the first workspace, a one-time copy of app.db
    stop = {"now": False}

    def _quit(*_):
        stop["now"] = True
    signal.signal(signal.SIGTERM, _quit)
    signal.signal(signal.SIGINT, _quit)
    scheduler.start()
    jobqueue.start_workers()
    print(f"Context Layer {core.VERSION} worker {jobqueue.WORKER}: running jobs and the schedule", flush=True)
    while not stop["now"]:
        time.sleep(1)
    scheduler.stop()
    jobqueue.stop_workers()
    print("worker stopped", file=sys.stderr)


if __name__ == "__main__":
    main()
