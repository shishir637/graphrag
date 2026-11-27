"""The worker process: job workers and the scheduler, without an HTTP server.

CL_ROLE decides what a process runs:
  all     (default) web app, REST, MCP, job workers and the scheduler in one process: single-container installs
  api     web app, REST and MCP only: no job workers, no scheduler (questions never compete with builds)
  worker  job workers and the scheduler only (python -m app.worker, or python -m app with CL_ROLE=worker)

In docker-compose.yml the same image runs twice: `app` (CL_ROLE=api) and `worker` (CL_ROLE=worker), sharing the
volumes. The scheduler runs in exactly one place even with several workers: each scheduler takes a Postgres
advisory lock and only the holder schedules (app/scheduler.py). Jobs are claimed with FOR UPDATE SKIP LOCKED and one
workspace's jobs never run in parallel (app/jobs.py), so more workers can be added. A running job writes a
heartbeat every minute, so a worker that starts while another one is busy never mistakes a long, quiet job for a
stopped one (jobs.recover_stale only fails jobs without a heartbeat for 10 minutes).

The worker touches CL_WORKER_ALIVE (default /tmp/cl-worker-alive) every 30 seconds for the container health check.
"""
import os
import signal
import threading
import time
from pathlib import Path

ROLES = ("all", "api", "worker")


def role() -> str:
    r = (os.environ.get("CL_ROLE") or "all").strip().lower()
    if r not in ROLES:
        raise SystemExit(f"CL_ROLE={r}: use all, api or worker.")
    return r


def runs_jobs() -> bool:
    """Job workers and the scheduler run in this process (CL_WORKERS=0 also turns the workers off)."""
    return role() in ("all", "worker")


def main(stop: threading.Event | None = None):
    """Run until SIGTERM / SIGINT (or until `stop` is set, for tests)."""
    from . import core, jobs, scheduler, upgrade
    from . import engine  # noqa: F401  (registers the job handlers)
    core.load_secrets()
    from connectors import manage
    manage.install()                                       # the sync job and the scheduler hook
    from .db import pool
    pool()                                                 # applies migrations
    try:
        upgrade.run()                                      # existing workspaces -> the 0.7 layout (idempotent, locked)
    except Exception as e:                                 # noqa: BLE001 - never block the worker
        print(f"0.7 upgrade skipped: {type(e).__name__}: {e}")
    stop = stop or threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, lambda *_: stop.set())
        except ValueError:                                 # not the main thread (tests)
            pass
    n = int(os.environ.get("CL_WORKERS", "2"))
    jobs.start_workers(n)
    scheduler.start()
    alive = Path(os.environ.get("CL_WORKER_ALIVE", "/tmp/cl-worker-alive"))
    print(f"Context Layer {core.VERSION} worker: {n} job workers + scheduler (role {role()})", flush=True)
    try:
        while not stop.is_set():
            try:
                alive.touch()
            except OSError:
                pass
            stop.wait(30)
    finally:
        scheduler.stop()
        jobs.stop_workers()
        time.sleep(0.1)


if __name__ == "__main__":
    main()
