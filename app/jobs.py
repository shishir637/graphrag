"""Background jobs (prepare documents, data, graph; uploads; connector syncs; nightly checks), queued in Postgres
(table jobs, migrations/004_jobs.sql).

- Any app process can run them: worker threads claim the oldest queued job with FOR UPDATE SKIP LOCKED.
- One job at a time per workspace (builds of one workspace share its files); different workspaces run in parallel
  up to CL_WORKERS threads per process (default 2).
- Jobs run inside their workspace (row-level security, per-workspace folders).
- Questions do not wait for a build: indexes are updated in place, so agents keep the last good version.
- A job whose worker stopped (no heartbeat for 10 minutes) is marked failed at the next start.

What a kind runs is registered in HANDLERS (kind -> fn(info)); app.engine registers the build steps.
"""
import io
import json
import os
import secrets
import socket
import sys
import threading
import time
import traceback
from contextlib import contextmanager

from . import core
from .db import system, tenant_scope

HANDLERS: dict = {}                 # kind -> callable(info: dict)
LABELS: dict = {}
STALE_S = 600
WORKER = f"{socket.gethostname()}:{os.getpid()}"
LOCK = threading.Lock()             # kept for callers that serialise local work; questions no longer wait on it
_stop = threading.Event()
_threads: list[threading.Thread] = []
_wake = threading.Event()


class _Router(io.TextIOBase):
    """sys.stdout/stderr that sends a worker thread's prints to its job log (several jobs run at once)."""

    def __init__(self, base):
        self.base, self.local = base, threading.local()

    def write(self, s):
        target = getattr(self.local, "log", None)
        return target.write(s) if target is not None else self.base.write(s)

    def flush(self):
        if getattr(self.local, "log", None) is None:
            self.base.flush()


_routers: list[_Router] = []


def _install_router():
    if not _routers:
        _routers.extend([_Router(sys.stdout), _Router(sys.stderr)])
        sys.stdout, sys.stderr = _routers


@contextmanager
def _capture(log):
    _install_router()
    for r in _routers:
        r.local.log = log
    try:
        yield
    finally:
        for r in _routers:
            r.local.log = None


def register(kind: str, label: str, fn):
    HANDLERS[kind], LABELS[kind] = fn, label


class _Log(io.TextIOBase):
    """Collects a job's printed lines and writes them to the job row every couple of seconds."""

    def __init__(self, job_id: str, tenant: str):
        self.job_id, self.tenant, self.buf, self.lines, self.last = job_id, tenant, "", [], 0.0

    def write(self, s):
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            line = line.rstrip()
            if line:
                self.lines.append(line[:400])
                del self.lines[:-300]
        if time.time() - self.last > 2:
            self.flush()
        return len(s)

    def flush(self):
        self.last = time.time()
        with tenant_scope(self.tenant):
            core.db().execute("UPDATE jobs SET log = ?, heartbeat = ? WHERE id = ?",
                              ("\n".join(self.lines), core.now(), self.job_id))


# ------------------------------------------------------------------ reading

def _public(r) -> dict:
    log = (r["log"] or "").splitlines()
    return {"id": r["id"], "kind": r["kind"], "label": r["label"], "status": r["status"], "started": r["started"],
            "finished": r["finished"], "by": r["by"], "error": r["error"], "log": log[-60:], "created": r["created"],
            "queue_position": None}


def public(job: dict) -> dict:
    return job


def list_jobs(limit: int = 20) -> list[dict]:
    rows = core.db().execute("SELECT * FROM jobs WHERE tenant_id = ? ORDER BY seq DESC LIMIT ?",
                             (core.current_tenant(), limit)).fetchall()
    out = [_public(r) for r in rows]
    queued = sorted((j for j in out if j["status"] == "queued"), key=lambda j: j["created"])
    for i, j in enumerate(queued, 1):
        j["queue_position"] = i
    return out


def get(job_id: str) -> dict | None:
    r = core.db().execute("SELECT * FROM jobs WHERE id = ? AND tenant_id = ?", (job_id, core.current_tenant())).fetchone()
    if r is None:
        return None
    j = _public(r)
    if j["status"] == "queued":
        j["queue_position"] = core.db().execute(
            "SELECT count(*) FROM jobs WHERE tenant_id = ? AND status = 'queued' AND seq <= ?",
            (core.current_tenant(), r["seq"])).fetchone()[0]
    return j


def info(job_id: str) -> dict:
    r = core.db().execute("SELECT info FROM jobs WHERE id = ? AND tenant_id = ?", (job_id, core.current_tenant())).fetchone()
    return dict(r["info"]) if r else {}


def running_mine() -> dict | None:
    r = core.db().execute("SELECT * FROM jobs WHERE tenant_id = ? AND status = 'running' ORDER BY seq DESC LIMIT 1",
                          (core.current_tenant(),)).fetchone()
    return _public(r) if r else None


def running() -> dict | None:
    """The current workspace's running job (each workspace runs one at a time)."""
    return running_mine()


# ------------------------------------------------------------------ queueing

def _insert(kind: str, label: str, by: str, info_: dict) -> dict:
    jid = secrets.token_hex(5)
    core.db().execute("INSERT INTO jobs (id, tenant_id, kind, label, status, info, by, created) "
                      "VALUES (?, ?, ?, ?, 'queued', ?::jsonb, ?, ?)",
                      (jid, core.current_tenant(), kind, label, json.dumps(info_), by, core.now()))
    _wake.set()
    return get(jid)


def start(kind: str, label: str, fn=None, by: str = "") -> dict:
    """Queue a build an admin asked for. Refused while the same build is already queued or running."""
    if kind not in HANDLERS and fn is not None:
        register(kind, label, lambda info, _fn=fn: _fn())
    r = core.db().execute("SELECT label FROM jobs WHERE tenant_id = ? AND kind = ? AND status IN ('queued', 'running')",
                          (core.current_tenant(), kind)).fetchone()
    if r:
        raise RuntimeError(f"'{r['label']}' is already queued or running. Wait for it to finish.")
    return _insert(kind, label, by, {})


def enqueue(kind: str, label: str, make_fn=None, by: str = "", info: dict | None = None) -> dict:
    """Run after whatever this workspace is running now. A waiting job of the same kind is reused: its info is
    merged (lists extended, other values replaced), so many uploads in a row become one rebuild."""
    info = info or {}
    if kind not in HANDLERS and make_fn is not None:
        register(kind, label, make_fn)
    with core.tx() as c:
        r = c.execute("SELECT id, info FROM jobs WHERE tenant_id = ? AND kind = ? AND status = 'queued' "
                      "ORDER BY seq LIMIT 1 FOR UPDATE", (core.current_tenant(), kind)).fetchone()
        jid = None
        if r:
            merged = dict(r["info"])
            for k, v in info.items():
                if isinstance(v, list):
                    merged.setdefault(k, [])
                    merged[k] += [x for x in v if x not in merged[k]]
                else:
                    merged[k] = v
            c.execute("UPDATE jobs SET info = ?::jsonb WHERE id = ?", (json.dumps(merged), r["id"]))
            jid = r["id"]
    if jid:
        return get(jid)
    return _insert(kind, label, by, {k: (list(v) if isinstance(v, list) else v) for k, v in info.items()})


# ------------------------------------------------------------------ running

def _claim():
    """The oldest queued job of a workspace that is not running anything (a per-workspace advisory lock and a
    fresh re-check make sure two workers never run one workspace's jobs at the same time)."""
    with system(), core.tx() as c:
        cands = c.execute(
            "SELECT * FROM jobs j WHERE status = 'queued' AND NOT EXISTS (SELECT 1 FROM jobs x WHERE x.tenant_id = j.tenant_id "
            "AND x.status = 'running') ORDER BY seq FOR UPDATE SKIP LOCKED LIMIT 10").fetchall()
        seen = set()
        for r in cands:
            if r["tenant_id"] in seen:
                continue
            seen.add(r["tenant_id"])
            if not c.execute("SELECT pg_try_advisory_xact_lock(727002, hashtext(?))", (r["tenant_id"],)).fetchone()[0]:
                continue
            if c.execute("SELECT 1 FROM jobs WHERE tenant_id = ? AND status = 'running'", (r["tenant_id"],)).fetchone():
                continue
            c.execute("UPDATE jobs SET status = 'running', started = ?, heartbeat = ?, worker = ? WHERE id = ?",
                      (core.now(), core.now(), WORKER, r["id"]))
            return r
        return None


def run_one() -> bool:
    """Claim and run one job (False when there was nothing to do)."""
    r = _claim()
    if r is None:
        return False
    t, log = core.timer(), _Log(r["id"], r["tenant_id"])
    status, error = "done", None
    with tenant_scope(r["tenant_id"]):
        try:
            fn = HANDLERS.get(r["kind"])
            if fn is None:
                raise RuntimeError(f"No handler for job kind {r['kind']}")
            with _capture(log):
                fn(dict(r["info"]))
        except BaseException as e:                         # noqa: BLE001 - shown to the admin
            status, error = "failed", f"{type(e).__name__}: {e}"[:600]
            log.lines.extend(traceback.format_exc().splitlines()[-8:])
            print(traceback.format_exc(), file=sys.__stderr__)
        finally:
            log.flush()
            core.db().execute("UPDATE jobs SET status = ?, error = ?, finished = ? WHERE id = ?",
                              (status, error, core.now(), r["id"]))
            core.log_event("user" if "@" in (r["by"] or "") else "system", r["by"] or "system", f"job.{r['kind']}",
                           r["label"], {"job": r["id"], "error": error}, ok=status == "done", seconds=t())
    return True


def recover_stale():
    """Jobs left running by a stopped process are marked failed (their workspace can then run again)."""
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=STALE_S)).isoformat(timespec="seconds")
    with system():
        core.db().execute("UPDATE jobs SET status = 'failed', error = 'The server stopped while this was running.', "
                          "finished = ? WHERE status = 'running' AND (heartbeat IS NULL OR heartbeat < ?)",
                          (core.now(), cutoff))


def _loop():
    while not _stop.is_set():
        try:
            if run_one():
                continue
        except Exception:                                  # noqa: BLE001 - keep the worker alive
            print(traceback.format_exc(), file=sys.__stderr__)
            _stop.wait(5)
        _wake.wait(2)
        _wake.clear()


def start_workers(n: int | None = None):
    if _threads:
        return
    n = n if n is not None else int(os.environ.get("CL_WORKERS", "2"))
    _stop.clear()
    try:
        recover_stale()
    except Exception:                                      # noqa: BLE001
        print(traceback.format_exc(), file=sys.__stderr__)
    for i in range(n):
        th = threading.Thread(target=_loop, daemon=True, name=f"cl-worker-{i}")
        th.start()
        _threads.append(th)


def stop_workers():
    _stop.set()
    _wake.set()
    for th in _threads:
        th.join(timeout=5)
    _threads.clear()


def wait_idle(timeout: float = 60) -> bool:
    """Tests: wait until the current workspace has nothing queued or running."""
    end = time.time() + timeout
    while time.time() < end:
        n = core.db().execute("SELECT count(*) FROM jobs WHERE tenant_id = ? AND status IN ('queued', 'running')",
                              (core.current_tenant(),)).fetchone()[0]
        if n == 0:
            return True
        time.sleep(0.2)
    return False
