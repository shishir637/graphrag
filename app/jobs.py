"""Background jobs (prepare documents, data, graph; quality runs). One at a time, because the engines share
the embedded search index and output files. Agent questions wait for a running job (see LOCK)."""
import io
import secrets
import sys
import threading
import traceback
from contextlib import redirect_stderr, redirect_stdout

from . import core

LOCK = threading.Lock()            # held by a running job; read paths take it briefly too
_jobs: dict[str, dict] = {}
_order: list[str] = []


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
        return len(s)


def running() -> dict | None:
    return next((_jobs[j] for j in reversed(_order) if _jobs[j]["status"] == "running"), None)


def public(job: dict) -> dict:
    return {k: job[k] for k in ("id", "kind", "label", "status", "started", "finished", "by", "error")} | \
           {"log": job["log"][-60:]}


def list_jobs(limit: int = 20) -> list[dict]:
    return [public(_jobs[j]) for j in reversed(_order[-limit:])]


def get(job_id: str) -> dict | None:
    j = _jobs.get(job_id)
    return public(j) if j else None


def start(kind: str, label: str, fn, by: str) -> dict:
    if running():
        raise RuntimeError(f"'{running()['label']}' is still running. Wait for it to finish.")
    job = {"id": secrets.token_hex(5), "kind": kind, "label": label, "status": "running", "started": core.now(),
           "finished": None, "by": by, "error": None, "log": []}
    _jobs[job["id"]] = job
    _order.append(job["id"])
    del _order[:-50]

    def run():
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
                core.log_event("user", by, f"job.{kind}", label, {"job": job["id"], "error": job["error"]},
                               ok=job["status"] == "done", seconds=t())

    threading.Thread(target=run, daemon=True, name=f"job-{kind}").start()
    return public(job)
