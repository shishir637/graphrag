"""API and worker as separate processes (0.9): on Postgres the job queue is the jobs table; the API queues, a worker
claims (FOR UPDATE SKIP LOCKED, one running job per workspace), runs the handler of the job's kind in the job's
workspace and writes the log back; waiting jobs of the same kind merge; a job whose worker stopped is marked failed;
one process schedules (advisory lock); CL_ROLE=api runs no workers. Needs CL_TEST_DATABASE_URL."""
import os
import time

import pytest

from app import core, jobqueue, jobs, scheduler

pytestmark = pytest.mark.skipif(not os.environ.get("CL_TEST_DATABASE_URL"), reason="no test Postgres (CL_TEST_DATABASE_URL)")


@pytest.fixture
def q(tmp_path):
    jobqueue.stop_workers()
    core.reset_for_tests(tmp_path / "var")
    ran = []
    jobqueue.register("probe", lambda info: (print("[1/2] probing"), ran.append((core.current_tenant(), dict(info)))))
    yield ran
    jobqueue.HANDLERS.pop("probe", None)


def test_queue_claim_run_and_log(q):
    assert jobs.db_mode()
    j = jobs.enqueue("probe", "Probing", None, "o@example.com", {"files": ["a"]})
    assert j["status"] == "queued" and jobs.get(j["id"])["queue_position"] == 1
    j2 = jobs.enqueue("probe", "Probing", None, "o@example.com", {"files": ["b"]})
    assert j2["id"] == j["id"] and jobs.info(j["id"])["files"] == ["a", "b"]           # merged while waiting
    assert jobqueue.work_once() and not jobqueue.work_once()
    done = jobs.get(j["id"])
    assert done["status"] == "done" and any("probing" in x for x in done["log"]) and done["steps"]
    assert q == [("default", {"files": ["a", "b"], "by": "o@example.com"})]
    assert any(e["action"] == "job.probe" for e in core.list_events())


def test_one_running_job_per_workspace_and_workspaces_apart(q):
    with core.tenant_scope("acme-1"):
        a1 = jobs.enqueue("probe:1", "A1", None, "x", {})
        jobs.enqueue("probe:2", "A2", None, "x", {})
    with core.tenant_scope("globex-2"):
        b1 = jobs.enqueue("probe:1", "B1", None, "y", {})
        assert [x["id"] for x in jobs.list_jobs()] == [b1["id"]]                        # only its own jobs
    first = jobqueue._claim()
    assert first["id"] == a1["id"]
    second = jobqueue._claim()                                                          # acme busy: globex goes next
    assert second["id"] == b1["id"] and jobqueue._claim() is None


def test_stale_running_job_is_failed(q, monkeypatch):
    j = jobs.enqueue("probe", "P", None, "x", {})
    jobqueue._claim()
    core.db().execute("UPDATE jobs SET heartbeat = '2020-01-01T00:00:00+00:00' WHERE id = ?", (j["id"],))
    jobqueue._claim()
    assert jobs.get(j["id"])["status"] == "failed" and "stopped" in jobs.get(j["id"])["error"]


def test_start_refuses_while_waiting(q):
    jobs.start("probe", "P", None, "x")
    with pytest.raises(RuntimeError):
        jobs.start("probe", "P", None, "x")


def test_worker_threads_run_jobs(q):
    jobqueue.start_workers(1)
    try:
        j = jobs.enqueue("probe", "P", None, "x", {"n": 1})
        for _ in range(50):
            if jobs.get(j["id"])["status"] == "done":
                break
            time.sleep(0.1)
        assert jobs.get(j["id"])["status"] == "done"
    finally:
        jobqueue.stop_workers()


def test_one_scheduler_leader(q):
    import psycopg
    from app import db as D
    scheduler.release_leadership()
    other = psycopg.connect(D.url(), autocommit=True)
    try:
        assert other.execute("SELECT pg_try_advisory_lock(%s)", (scheduler.LEADER_LOCK,)).fetchone()[0]
        assert scheduler.is_leader() is False                                           # another process schedules
        other.execute("SELECT pg_advisory_unlock(%s)", (scheduler.LEADER_LOCK,))
        assert scheduler.is_leader() is True and scheduler.is_leader() is True
    finally:
        other.close()
        scheduler.release_leadership()


def test_api_role_runs_no_workers(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import engine
    core.reset_for_tests(tmp_path / "var")
    monkeypatch.setenv("CL_ROLE", "api")
    monkeypatch.setenv("CL_PUBLIC_HOSTS", "testserver")
    monkeypatch.setattr(engine, "postgres_ok", lambda: (False, "test"))
    jobqueue.stop_workers()
    from app.main import asgi
    with TestClient(asgi):
        assert not any(t.is_alive() for t in jobqueue._threads)
