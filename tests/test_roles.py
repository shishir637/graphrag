"""API and worker processes (0.7): CL_ROLE=api serves questions without job workers or a scheduler, CL_ROLE=worker
runs them without HTTP, CL_ROLE=all (default) does both; the scheduler runs in exactly one place, and a long, quiet
job keeps a heartbeat so a worker starting elsewhere never marks it failed."""
import threading
import time

import psycopg
import pytest

from app import core, db, jobs, scheduler, vault, worker
from tests.conftest import needs_pg

pytestmark = needs_pg


@pytest.fixture
def ws(tmp_path):
    jobs.stop_workers()
    scheduler.stop()
    core.reset_for_tests(tmp_path / "var")
    vault.reset_for_tests()
    yield core.create_tenant("Acme")
    jobs.stop_workers()
    scheduler.stop()


def test_role_values(monkeypatch):
    monkeypatch.delenv("CL_ROLE", raising=False)
    assert worker.role() == "all" and worker.runs_jobs()
    monkeypatch.setenv("CL_ROLE", "api")
    assert worker.role() == "api" and not worker.runs_jobs()
    monkeypatch.setenv("CL_ROLE", "Worker")
    assert worker.role() == "worker" and worker.runs_jobs()
    monkeypatch.setenv("CL_ROLE", "both")
    with pytest.raises(SystemExit):
        worker.role()


def test_api_role_runs_no_workers_and_no_scheduler(ws, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setenv("CL_ROLE", "api")
    with TestClient(app) as c:
        assert c.get("/api/v1/health").status_code == 200
        assert not jobs._threads and scheduler._thread is None
    monkeypatch.setenv("CL_ROLE", "all")
    monkeypatch.setenv("CL_WORKERS", "1")
    with TestClient(app):
        assert len(jobs._threads) == 1 and scheduler._thread is not None


def test_worker_runs_jobs_and_holds_the_only_scheduler(ws, monkeypatch, tmp_path):
    ran = []
    jobs.register("t_quiet", "A long, quiet job", lambda info: (time.sleep(1.2), ran.append(info)))
    monkeypatch.setattr(jobs, "HEARTBEAT_S", 0.2)
    monkeypatch.setattr(jobs, "STALE_S", 0.6)                    # stale after 0.6 s without a heartbeat
    monkeypatch.setattr(scheduler, "TICK_S", 0.2)
    monkeypatch.setenv("CL_WORKER_ALIVE", str(tmp_path / "alive"))
    monkeypatch.setenv("CL_WORKERS", "1")
    stop = threading.Event()
    th = threading.Thread(target=worker.main, args=(stop,), daemon=True)
    th.start()
    try:
        with core.tenant_scope(ws):
            j = jobs.enqueue("t_quiet", "quiet", None, "test", {"n": 1})
        deadline = time.time() + 15
        while time.time() < deadline and not ran:
            time.sleep(0.3)
            jobs.recover_stale()                                 # another worker starting meanwhile
        with core.tenant_scope(ws):
            jobs.wait_idle(10)
            st = core.db().execute("SELECT status FROM jobs WHERE id = ?", (j["id"],)).fetchone()["status"]
        assert ran == [{"n": 1}] and st == "done"                # the heartbeat kept it alive
        assert (tmp_path / "alive").exists()
        deadline = time.time() + 5
        while time.time() < deadline and scheduler._leader is None:
            time.sleep(0.2)
        with psycopg.connect(db.url(), autocommit=True) as other:   # a second scheduler cannot take the lead
            assert other.execute("SELECT pg_try_advisory_lock(727003)").fetchone()[0] is False
    finally:
        stop.set()
        th.join(timeout=10)
