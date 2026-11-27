"""Background jobs (prepare documents, data, graph; quality runs). One at a time, because the engines share
the embedded search index and output files. Agent questions wait for a running job (see LOCK)."""
import io
import time
import re
import secrets
import sys
import threading
import traceback
from contextlib import redirect_stderr, redirect_stdout

from . import core

LOCK = threading.Lock()            # held by a running job; read paths take it briefly too
_jobs: dict[str, dict] = {}
_order: list[str] = []


STEP = re.compile(r"^\s*(\[\d+/\d+\]|\[graph\]|Rebuilding|\d+/\d+ passages|unchanged |parsed |indexed |  removed )")


class _Log(io.TextIOBase):
    def __init__(self, job):
        self.job, self.buf = job, ""

    def write(self, s):
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            line = line.rstrip()
            if line:
                self.job["log"].append(line[:400])
                del self.job["log"][:-300]
                if STEP.match(line):
                    self.job.setdefault("steps", []).append(f"{time.strftime('%H:%M:%S')} {line.strip()[:160]}")
                    del self.job["steps"][:-40]
        return len(s)


def _mine(job: dict) -> bool:
    from .db import TENANT
    return job.get("tenant") == TENANT.get()


def running(any_workspace: bool = False) -> dict | None:
    """The running job of this workspace (any_workspace: of any; jobs run one at a time per process)."""
    return next((_jobs[j] for j in reversed(_order) if _jobs[j]["status"] == "running"
                 and (any_workspace or _mine(_jobs[j]))), None)


def public(job: dict) -> dict:
    return {k: job[k] for k in ("id", "kind", "label", "status", "started", "finished", "by", "error")} | \
           {"log": job["log"][-60:], "steps": list(job.get("steps") or [])[-40:], "queue_position": (_queue.index(job["id"]) + 1) if job["id"] in _queue else None}


def list_jobs(limit: int = 20) -> list[dict]:
    return [public(_jobs[j]) for j in reversed(_order) if _mine(_jobs[j])][:limit]


def get(job_id: str) -> dict | None:
    j = _jobs.get(job_id)
    return public(j) if j and _mine(j) else None


_qlock = threading.Lock()
_queue: list[str] = []


def _new(kind: str, label: str, by: str, status: str) -> dict:
    from .db import TENANT
    job = {"id": secrets.token_hex(5), "kind": kind, "label": label, "status": status, "started": None,
           "finished": None, "by": by, "error": None, "log": [], "info": {}, "tenant": TENANT.get()}
    _jobs[job["id"]] = job
    _order.append(job["id"])
    for old in _order[:-50]:
        if _jobs[old]["status"] not in ("running", "queued"):
            _jobs.pop(old, None)
    _order[:] = [j for j in _order if j in _jobs]
    return job


def _launch(job: dict, fn):
    job["status"], job["started"] = "running", core.now()

    def run():
        from .db import tenant_scope
        with tenant_scope(job.get("tenant")):            # a job runs in the workspace that queued it
            _run()

    def _run():
        t = core.timer()
        log = _Log(job)
        with LOCK:
            try:
                with redirect_stdout(log), redirect_stderr(log):
                    fn()
                job["status"] = "done"
            except BaseException as e:                     # noqa: BLE001 - shown to the admin
                job["status"], job["error"] = "failed", f"{type(e).__name__}: {e}"[:600]
                job["log"].extend(traceback.format_exc().splitlines()[-8:])
                print(traceback.format_exc(), file=sys.__stderr__)
            finally:
                job["finished"] = core.now()
                core.log_event("user", job["by"], f"job.{job['kind']}", job["label"],
                               {"job": job["id"], "error": job["error"]}, ok=job["status"] == "done", seconds=t())
        try:                                               # answers may change: drop cached evidence
            from . import engine
            engine.clear_ask_cache()
        except Exception:                                  # noqa: BLE001
            pass
        if job["status"] == "done":
            try:                                           # scores after every build (Quality tab)
                from . import quality
                quality.after_build(job["kind"], job.get("info") or {}, job["by"])
            except Exception:                              # noqa: BLE001 - never blocks the queue
                print(traceback.format_exc(), file=sys.__stderr__)
        _kick()

    threading.Thread(target=run, daemon=True, name=f"job-{job['kind']}").start()


def _kick():
    with _qlock:
        if running(True) or not _queue:
            return
        job = _jobs[_queue.pop(0)]
        _launch(job, job.pop("fn"))


def start(kind: str, label: str, fn, by: str) -> dict:
    with _qlock:
        busy = running(True)
        if busy:
            raise RuntimeError(f"'{busy['label']}' is still running. Wait for it to finish." if _mine(busy)
                               else "Another build is running on this server. Try again in a few minutes.")
        job = _new(kind, label, by, "running")
        _launch(job, fn)
    return public(job)


def enqueue(kind: str, label: str, make_fn, by: str, info: dict) -> dict:
    """Run after whatever is running now. A waiting job of the same kind is reused: its info is merged
    (lists extended, sets joined), so many uploads in a row become one rebuild. make_fn(info) -> callable."""
    with _qlock:
        for jid in _queue:
            j = _jobs[jid]
            if j["kind"] == kind and j["info"].get("layer") == info.get("layer") and _mine(j):
                for k, v in info.items():
                    if isinstance(v, list):
                        j["info"].setdefault(k, [])
                        j["info"][k] += [x for x in v if x not in j["info"][k]]
                    else:
                        j["info"][k] = v
                return public(j)
        job = _new(kind, label, by, "queued")
        job["info"] = {k: (list(v) if isinstance(v, list) else v) for k, v in info.items()}
        job["fn"] = lambda: make_fn(job["info"])
        _queue.append(job["id"])
    _kick()
    return public(job)


def info(job_id: str) -> dict:
    j = _jobs.get(job_id)
    return dict(j["info"]) if j and _mine(j) else {}
