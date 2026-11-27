"""Jobs in Postgres (0.9): the API process queues, worker processes run (app/worker.py, CL_ROLE=worker|all).

A job is a row of `jobs` (per workspace, row-level security). Workers claim the oldest queued job with
FOR UPDATE SKIP LOCKED, never two at a time for the same workspace (the engines share that workspace's files), and
run it in the job's workspace. The handler is picked by the job's kind (handler(info); see HANDLERS / _handler), so a
job can be run by any process. The log and the build steps are written back every few seconds for the web app.
A job whose worker stopped (no heartbeat for CL_JOB_STALE_MIN, default 15) is marked failed and can be run again.

app/jobs.py keeps its functions (enqueue, start, running, list_jobs, get, info, public) and forwards to this module
when the app is on Postgres (CL_JOBS=memory keeps the in-process queue).
"""
import io
import json
import os
import socket
import sys
import threading
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout

from . import core

SCHEMA = """CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, label TEXT NOT NULL, status TEXT NOT NULL, by_user TEXT NOT NULL DEFAULT '',
  info TEXT NOT NULL DEFAULT '{}', log TEXT NOT NULL DEFAULT '[]', steps TEXT NOT NULL DEFAULT '[]', error TEXT,
  created TEXT NOT NULL, started TEXT, finished TEXT, heartbeat TEXT, worker TEXT{tenant})"""
HANDLERS: dict = {}
WORKER = f"{socket.gethostname()}:{os.getpid()}"
_stop = threading.Event()
_threads: list[threading.Thread] = []


def register(kind: str, fn):
    """fn(info) runs a job of this kind (the part before ':' of the job's kind)."""
    HANDLERS[kind] = fn


def _db():
    return core.db_with(SCHEMA)


def _handler(kind: str):
    base = kind.split(":")[0]
    if base in HANDLERS:
        return HANDLERS[base]
    from . import engine
    if base in engine.JOBS:
        return lambda info: engine.JOBS[base][1]()
    if base == "rebuild":
        return engine.rebuild
    if base == "sync":
        from sources.manage import run_sync
        return run_sync
    if base == "quality":
        from . import quality
        return lambda info: quality.run(info.get("suites"), info.get("trigger") or "manual")
    if base == "monitor":
        from semantic import monitor
        return lambda info: monitor.run(info.get("by") or "nightly check")
    if base == "demo":
        from . import demo
        return lambda info: demo.load(info["by"])
    if base == "ontology":
        from graph import ontology
        return lambda info: ontology.propose()
    raise KeyError(f"no handler for jobs of kind {kind}")


def _row(r) -> dict:
    return {"id": r["id"], "kind": r["kind"], "label": r["label"], "status": r["status"], "started": r["started"],
            "finished": r["finished"], "by": r["by_user"], "error": r["error"], "log": json.loads(r["log"] or "[]"),
            "steps": json.loads(r["steps"] or "[]"), "info": json.loads(r["info"] or "{}"),
            "tenant": core._tenant(r), "created": r["created"]}


def public(job: dict) -> dict:
    pos = None
    if job["status"] == "queued":
        pos = 1 + _db().execute("SELECT count(*) FROM jobs WHERE status = 'queued' AND created < ?",
                                (job["created"],)).fetchone()[0]
    return {k: job[k] for k in ("id", "kind", "label", "status", "started", "finished", "by", "error")} | \
           {"log": job["log"][-60:], "steps": list(job.get("steps") or [])[-40:], "queue_position": pos}


def _new_id() -> str:
    return f"{time.time_ns():020d}{os.urandom(3).hex()}"


def enqueue(kind: str, label: str, by: str, info: dict) -> dict:
    """Queue a job in the current workspace; a waiting job of the same kind (and layer) takes the new info instead
    (lists extended, values replaced), so many uploads in a row become one rebuild."""
    info = {k: (list(v) if isinstance(v, list) else v) for k, v in info.items()}
    info.setdefault("by", by)
    _db()
    with core.tx() as c:
        for r in c.execute("SELECT * FROM jobs WHERE status = 'queued' AND kind = ? ORDER BY created FOR UPDATE",
                           (kind,)).fetchall() if _pg() else []:
            cur = json.loads(r["info"] or "{}")
            if cur.get("layer") != info.get("layer"):
                continue
            for k, v in info.items():
                if isinstance(v, list):
                    cur.setdefault(k, [])
                    cur[k] += [x for x in v if x not in cur[k]]
                else:
                    cur[k] = v
            c.execute("UPDATE jobs SET info = ? WHERE id = ?", (json.dumps(cur), r["id"]))
            return public(_row(c.execute("SELECT * FROM jobs WHERE id = ?", (r["id"],)).fetchone()))
        jid = _new_id()
        c.execute("INSERT INTO jobs (id, kind, label, status, by_user, info, created) VALUES (?, ?, ?, 'queued', ?, ?, ?)",
                  (jid, kind, label, by, json.dumps(info), core.now()))
        row = c.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
    return public(_row(row))


def _pg() -> bool:
    from . import db as D
    return D.postgres()


def start(kind: str, label: str, by: str) -> dict:
    busy = running()
    if busy or _db().execute("SELECT 1 FROM jobs WHERE status = 'queued' AND kind = ?", (kind,)).fetchone():
        raise RuntimeError(f"'{(busy or {}).get('label') or label}' is still running or waiting. Wait for it to finish.")
    return enqueue(kind, label, by, {})


def running(any_workspace: bool = False) -> dict | None:
    if any_workspace:
        with core.system():
            r = _db().execute("SELECT * FROM jobs WHERE status = 'running' ORDER BY started DESC LIMIT 1").fetchone()
    else:
        r = _db().execute("SELECT * FROM jobs WHERE status = 'running' ORDER BY started DESC LIMIT 1").fetchone()
    return _row(r) if r else None


def list_jobs(limit: int = 20) -> list[dict]:
    return [public(_row(r)) for r in _db().execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]


def get(job_id: str) -> dict | None:
    r = _db().execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return public(_row(r)) if r else None


def info(job_id: str) -> dict:
    r = _db().execute("SELECT info FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return json.loads(r["info"]) if r else {}


# ------------------------------------------------------------------ workers

class _DbLog(io.TextIOBase):
    """Captures a job's output into its row (every few seconds), like the in-process log."""

    def __init__(self, job: dict):
        from .jobs import STEP
        self.job, self.buf, self.step, self.last = job, "", STEP, 0.0

    def write(self, s):
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            line = line.rstrip()
            if line:
                self.job["log"].append(line[:400])
                del self.job["log"][:-300]
                if self.step.match(line):
                    self.job["steps"].append(f"{time.strftime('%H:%M:%S')} {line.strip()[:160]}")
                    del self.job["steps"][:-40]
        if time.time() - self.last > 2:
            self.flush_db()
        return len(s)

    def flush_db(self):
        self.last = time.time()
        try:
            _db().execute("UPDATE jobs SET log = ?, steps = ?, heartbeat = ? WHERE id = ?",
                          (json.dumps(self.job["log"]), json.dumps(self.job["steps"]), core.now(), self.job["id"]))
        except Exception:                                      # noqa: BLE001 - the log is a convenience
            pass


def _claim() -> dict | None:
    """The oldest queued job of a workspace that has nothing running, marked running by this worker."""
    from datetime import datetime, timedelta, timezone
    stale = (datetime.now(timezone.utc) - timedelta(minutes=float(os.environ.get("CL_JOB_STALE_MIN", "15")))) \
        .isoformat(timespec="seconds")
    _db()
    with core.system(), core.tx() as c:
        c.execute("UPDATE jobs SET status = 'failed', error = 'The worker running it stopped.', finished = ? "
                  "WHERE status = 'running' AND COALESCE(heartbeat, started) < ?", (core.now(), stale))
        r = c.execute("SELECT * FROM jobs j WHERE status = 'queued' AND NOT EXISTS (SELECT 1 FROM jobs x WHERE "
                      "x.status = 'running' AND x.tenant_id = j.tenant_id) ORDER BY created LIMIT 1 "
                      "FOR UPDATE SKIP LOCKED").fetchone()
        if r is None:
            return None
        c.execute("UPDATE jobs SET status = 'running', started = ?, heartbeat = ?, worker = ? WHERE id = ?",
                  (core.now(), core.now(), WORKER, r["id"]))
        return _row(c.execute("SELECT * FROM jobs WHERE id = ?", (r["id"],)).fetchone())


def work_once() -> bool:
    """Run one queued job if there is one. Returns True when a job ran."""
    job = _claim()
    if job is None:
        return False
    from .db import tenant_scope
    from .jobs import LOCK
    with tenant_scope(job["tenant"] if job["tenant"] != "default" else None):
        t = core.timer()
        log = _DbLog(job)
        status, error = "done", None
        with LOCK:
            try:
                with redirect_stdout(log), redirect_stderr(log):
                    _handler(job["kind"])(job["info"])
            except BaseException as e:                         # noqa: BLE001 - shown to the admin
                status, error = "failed", f"{type(e).__name__}: {e}"[:600]
                job["log"].extend(traceback.format_exc().splitlines()[-8:])
                print(traceback.format_exc(), file=sys.__stderr__)
        log.flush_db()
        _db().execute("UPDATE jobs SET status = ?, error = ?, finished = ?, log = ?, steps = ? WHERE id = ?",
                      (status, error, core.now(), json.dumps(job["log"][-300:]), json.dumps(job["steps"][-40:]), job["id"]))
        core.log_event("user", job["by"], f"job.{job['kind']}", job["label"], {"job": job["id"], "error": error},
                       ok=status == "done", seconds=t())
        try:
            from . import engine
            engine.clear_ask_cache()
        except Exception:                                      # noqa: BLE001
            pass
        if status == "done":
            try:
                from . import quality
                quality.after_build(job["kind"], job["info"], job["by"])
            except Exception:                                  # noqa: BLE001
                print(traceback.format_exc(), file=sys.__stderr__)
    return True


def _loop():
    poll = float(os.environ.get("CL_WORKER_POLL_S", "1"))
    while not _stop.is_set():
        try:
            if not work_once():
                _stop.wait(poll)
        except Exception:                                      # noqa: BLE001 - keep working
            print(traceback.format_exc(), file=sys.__stderr__)
            _stop.wait(5)


def start_workers(n: int | None = None):
    """Worker threads in this process (CL_WORKERS, default 1)."""
    if any(t.is_alive() for t in _threads):
        return
    _stop.clear()
    _threads.clear()
    for i in range(n or int(os.environ.get("CL_WORKERS", "1"))):
        t = threading.Thread(target=_loop, daemon=True, name=f"worker-{i}")
        t.start()
        _threads.append(t)


def stop_workers():
    _stop.set()
    for t in _threads:
        t.join(timeout=10)
    _threads.clear()
