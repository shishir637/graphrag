"""Background work in Postgres: the job queue (merging, one job per workspace at a time, logs, failures), the
scheduler, the nightly metric check and synonym suggestions from unanswered questions. No models needed."""
import pytest

from app import core, jobs, scheduler
from semantic import repo
from semantic.config import LAYER_DIR
from tests.conftest import needs_pg, seed

pytestmark = needs_pg


@pytest.fixture
def ws(tmp_path, monkeypatch):
    jobs.stop_workers()                                   # run jobs by hand here (run_one), not in threads
    core.reset_for_tests(tmp_path / "var")
    return core.create_tenant("Acme"), core.create_tenant("Globex")


def test_queue_merges_runs_in_workspace_and_logs(ws):
    a, b = ws
    seen = []
    jobs.register("t_build", "Test build", lambda info: (print("step one"), seen.append((core.current_tenant(), info))))
    with core.tenant_scope(a):
        j1 = jobs.enqueue("t_build", "Test build", None, "ann@acme.io", {"files": ["x"]})
        j2 = jobs.enqueue("t_build", "Test build", None, "ann@acme.io", {"files": ["y"]})
        assert j1["id"] == j2["id"] and jobs.info(j1["id"])["files"] == ["x", "y"]      # one rebuild for both
    with core.tenant_scope(b):
        assert jobs.get(j1["id"]) is None and jobs.list_jobs() == []                    # other workspace: invisible
    assert jobs.run_one() and not jobs.run_one()
    assert seen == [(a, {"files": ["x", "y"]})]
    with core.tenant_scope(a):
        j = jobs.get(j1["id"])
        assert j["status"] == "done" and "step one" in j["log"]
        assert any(e["action"] == "job.t_build" for e in core.list_events())


def test_failure_is_recorded(ws):
    a, _ = ws
    jobs.register("t_fail", "Fails", lambda info: 1 / 0)
    with core.tenant_scope(a):
        jid = jobs.start("t_fail", "Fails", None, "ann@acme.io")["id"]
        with pytest.raises(RuntimeError):
            jobs.start("t_fail", "Fails", None, "ann@acme.io")                       # already queued
    jobs.run_one()
    with core.tenant_scope(a):
        j = jobs.get(jid)
        assert j["status"] == "failed" and "ZeroDivisionError" in j["error"]


def test_one_job_per_workspace_at_a_time(ws):
    a, b = ws
    jobs.register("t_noop", "Noop", lambda info: None)
    with core.tenant_scope(a):
        ja = jobs.enqueue("t_noop", "Noop", None, "x", {"n": 1})
        jobs.enqueue("t_other", "Other", None, "x", {})
    with core.tenant_scope(b):
        jb = jobs.enqueue("t_noop", "Noop", None, "x", {})
    first = jobs._claim()                                   # Acme's first job is now running...
    assert first["id"] == ja["id"]
    second = jobs._claim()                                  # ...so the next claim goes to Globex, not Acme's 2nd job
    assert second["id"] == jb["id"] and jobs._claim() is None


def test_scheduler_queues_checks_once(ws):
    a, b = ws
    with core.tenant_scope(a):
        seed(a)
    assert scheduler.tick() == 4          # Acme: check + learning + retention; Globex (no layer): retention only
    assert scheduler.tick() == 0                            # not due again yet
    with core.tenant_scope(a):
        assert {j["kind"] for j in jobs.list_jobs()} == {"monitor", "learn", "retention"}
    with core.tenant_scope(b):
        assert {j["kind"] for j in jobs.list_jobs()} == {"retention"}


def test_monitor_takes_out_a_broken_metric_and_restores_it(ws):
    from semantic import monitor
    a, _ = ws
    with core.tenant_scope(a):
        seed(a)
        r = repo.get("metric", "net_revenue")
        good = r["definition"]
        repo.put("metric", "net_revenue", good | {"expression": "sum(orders.no_such_column)"}, status="approved",
                 source="manual", by="ann")
        out = monitor.run()
        failed = [f["metric"] for f in out["failed"]]          # net_revenue and the metrics built on it
        assert "net_revenue" in failed and "average_order_value" in failed and "gross_revenue" not in failed
        r = repo.get("metric", "net_revenue")
        assert r["status"] == "needs_review" and r["note"].startswith(monitor.CHECK_NOTE)
        repo.put("metric", "net_revenue", good, status="needs_review", source="manual", by="ann", note=r["note"])
        assert sorted(monitor.run()["restored"]) == sorted(failed)
        assert repo.get("metric", "net_revenue")["status"] == "approved"


def test_learning_suggests_a_synonym_draft(ws):
    from semantic import learn
    a, _ = ws
    with core.tenant_scope(a):
        seed(a)
        for q in ("total refundz last month", "refundz by region", "how many refundz in 2025"):
            learn.record(q, "no_metric")
        learn.record("refundz by region", "no_metric")
        out = learn.run()
        assert {"term": "refundz", "target": "refunds", "asked": 4} in out["proposed"]
        r = repo.get("synonym", "refundz=>refunds")
        assert r["status"] == "draft" and r["source"] == "learned"
        assert "refundz" not in {s.term for s in repo.load_enrichment().synonyms}    # agents only see approved
