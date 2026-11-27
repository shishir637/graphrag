"""Quality tab: which suites a build touches, runs recorded with their trend, gold metric sets chosen by the metrics
in the company layer (the Kestrel questions when the Kestrel demo is loaded), queued after builds, and the API.
No models: the suites' engines are replaced by fixed results here (they have their own tests)."""
import pytest
from fastapi.testclient import TestClient

from app import core, engine, jobs
from app import quality as Q
from tests.test_app import H, setup_owner


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    core.reset_for_tests(tmp_path / "var")
    Q._ready = None
    yield tmp_path


def test_suites_for_each_build():
    assert Q.suites_for("documents") == ["documents"]
    assert Q.suites_for("data") == ["metrics"]
    assert set(Q.suites_for("graph")) == {"graph", "documents"}
    assert Q.suites_for("rebuild", {"files": [{"kind": "table"}]}) == ["metrics"]
    assert Q.suites_for("rebuild", {"files": [{"kind": "document"}], "graph": True}) == ["documents", "graph"]
    assert set(Q.suites_for("sync:s_1")) == {"documents", "metrics", "graph"}
    assert Q.suites_for("quality") == [] and Q.suites_for("ontology") == []


def test_runs_are_recorded_with_a_trend(fresh, monkeypatch):
    vals = iter([0.6, 0.8])
    monkeypatch.setitem(Q.RUNNERS, "documents", lambda: {"scores": {"recall@5": next(vals), "mrr": 0.5, "questions": 10}})
    monkeypatch.setitem(Q.RUNNERS, "graph", lambda: {"skipped": "The knowledge graph is not built."})
    monkeypatch.setitem(Q.RUNNERS, "metrics", lambda: 1 / 0)
    Q.run(trigger="after documents")
    Q.run(["documents"], trigger="manual")
    ov = {s["id"]: s for s in Q.overview()}
    d = ov["documents"]
    assert d["scores"]["recall@5"] == 0.8 and d["change"] == 0.2
    assert [p["value"] for p in d["trend"]] == [0.6, 0.8] and d["trend"][0]["trigger"] == "after documents"
    assert ov["metrics"]["ok"] is False and "ZeroDivisionError" in ov["metrics"]["error"]
    assert ov["graph"]["last_run"] is None                        # skipped suites are not recorded
    assert len(Q.history()) == 3


def test_gold_sets_follow_the_company_layer(fresh, monkeypatch):
    from semantic import rule_eval, yamlio
    used = []
    monkeypatch.setattr(rule_eval, "score_set", lambda p: used.append(p.name) or
                        {"questions": 4, "execution_accuracy": 0.75, "refusal_accuracy": 1.0, "by_kind": {}, "failures": []})

    class M:
        def __init__(self, names):
            self.metrics = [type("X", (), {"name": n})() for n in names]
    monkeypatch.setattr(yamlio, "load_metrics", lambda: M(["deployment_count", "change_failure_rate", "sev1_incidents",
                                                         "lead_time_for_changes", "incident_count", "failed_deployments",
                                                         "mrr", "headcount", "support_ticket_count", "cloud_cost"]))
    out = Q.run_metrics()
    assert used == ["eval_questions.yaml"] and Q.KESTREL_GOLD.name == used[0]    # only the Kestrel set fits
    assert out["scores"]["kestrel_accuracy"] == 0.75 and "development" in out["detail"]["skipped_sets"]
    monkeypatch.setattr(yamlio, "load_metrics", lambda: M(["something_else"]))
    assert "skipped" in Q.run_metrics()


def test_documents_suite_needs_gold_passages(fresh, monkeypatch, tmp_path):
    out = tmp_path / "outputs"
    out.mkdir()
    monkeypatch.setattr(engine, "OUT_DIR", out)
    (out / "2_chunks.json").write_text('[{"chunk_id": "x1", "source": "a.md", "text": "t"}]', encoding="utf-8")
    assert "skipped" in Q.run_documents()


def test_queued_after_a_build(fresh, monkeypatch):
    got = []
    monkeypatch.setattr(jobs, "enqueue", lambda kind, label, fn, by, info: got.append((kind, info)) or {"id": "j"})
    Q.after_build("data", {}, "o@example.com")
    assert got == [("quality", {"suites": ["metrics"], "trigger": "after data"})]
    core.set_setting("quality_after_build", False)
    Q.after_build("data", {}, "o@example.com")
    assert len(got) == 1
    core.set_setting("quality_after_build", True)
    monkeypatch.setenv("CL_QUALITY_AFTER_BUILD", "0")
    Q.after_build("documents", {}, "o@example.com")
    assert len(got) == 1


def test_quality_api(tmp_path, monkeypatch):
    core.reset_for_tests(tmp_path / "var")
    Q._ready = None
    monkeypatch.setenv("CL_PUBLIC_HOSTS", "testserver")
    monkeypatch.setattr(engine, "postgres_ok", lambda: (False, "test"))
    queued = []
    monkeypatch.setattr(jobs, "enqueue", lambda kind, label, fn, by, info: queued.append(info) or {"id": "j1", "status": "queued"})
    from app.main import asgi
    with TestClient(asgi) as c:
        setup_owner(c)
        r = c.get("/api/v1/quality").json()
        assert [s["id"] for s in r["suites"]] == ["documents", "metrics", "graph"] and r["after_build"] is True
        assert c.post("/api/v1/quality/run", headers=H, json={"suites": ["metrics"]}).status_code == 200
        assert queued == [{"suites": ["metrics"], "trigger": "manual"}]
        assert c.post("/api/v1/quality/run", headers=H, json={"suites": ["vibes"]}).status_code == 400
        assert c.put("/api/v1/quality/settings", headers=H, json={"after_build": False}).json() == {"after_build": False}
        assert c.get("/api/v1/quality").json()["after_build"] is False
