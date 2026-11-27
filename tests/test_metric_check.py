"""Nightly metric check (0.9): approved metrics are run on the current data; one that fails (a renamed or dropped
column) is set to needs_review, hidden from agents and shown on the Approvals tab; it is approved again when it
works again. Queued once a day per workspace by the scheduler. Sample layer in a SQLite definitions store; no models."""
import shutil

import pytest
from fastapi.testclient import TestClient

from app import core, engine, jobs
from semantic import config as SC
from semantic import db as SDB
from semantic import defstore, monitor


@pytest.fixture
def layer(tmp_path, monkeypatch):
    core.reset_for_tests(tmp_path / "var")
    data = tmp_path / "data"
    shutil.copytree(SC.ROOT / "data", data, ignore=shutil.ignore_patterns("_*"))
    monkeypatch.setattr(SC, "DATA_DIR", data)
    monkeypatch.setattr(SDB, "DATA_DIR", data)
    monkeypatch.setenv("CL_DEFS_DSN", f"sqlite:///{tmp_path / 'defs.db'}")
    monkeypatch.setenv("CL_DEFS_EXPORT", "0")
    defstore.reset_for_tests()
    engine.clear_ask_cache()
    yield data
    defstore.reset_for_tests()


def status(name):
    from semantic.yamlio import load_metrics
    return next(m.status for m in load_metrics().metrics if m.name == name)


def test_renamed_column_takes_a_metric_out_until_it_works_again(layer):
    orders = (layer / "orders.csv").read_text(encoding="utf-8")
    assert status("net_revenue") == "approved"
    assert monitor.run()["failed"] == []
    (layer / "orders.csv").write_text(orders.replace("refund_amount", "refund_amt", 1), encoding="utf-8")
    out = monitor.run()
    assert "net_revenue" in {f["metric"] for f in out["failed"]}
    assert status("net_revenue") == "needs_review"
    taken = {c["metric"]: c for c in monitor.taken_out()}
    assert taken["net_revenue"]["why"] == "a column it uses was renamed or removed"
    # hidden from agents: refused by name, not listed, not planned
    with pytest.raises(ValueError, match="being reviewed"):
        engine.metric_query({"metrics": ["net_revenue"]})
    assert "net_revenue" not in {m["name"] for m in engine.describe_data()["metrics"]}
    from semantic.rule_planner import planner
    assert all(m.name != "net_revenue" for m in planner().approved)
    # fixed: the next run approves it again
    (layer / "orders.csv").write_text(orders, encoding="utf-8")
    out = monitor.run()
    assert "net_revenue" in out["restored"] and status("net_revenue") == "approved" and monitor.taken_out() == []


def test_queued_once_a_day(layer, monkeypatch):
    queued = []
    monkeypatch.setattr(jobs, "enqueue", lambda kind, label, fn, by, info: queued.append(kind) or {"id": "j"})
    assert monitor.due() == 1 and queued == ["monitor"]
    assert monitor.due() == 0
    monkeypatch.setenv("CL_MONITOR_HOURS", "0")
    assert monitor.due() == 1


def test_shown_on_the_approvals_tab(layer, monkeypatch):
    monkeypatch.setenv("CL_PUBLIC_HOSTS", "testserver")
    monkeypatch.setattr(engine, "postgres_ok", lambda: (False, "test"))
    (layer / "orders.csv").write_text((layer / "orders.csv").read_text(encoding="utf-8").replace("refund_amount", "x", 1),
                                      encoding="utf-8")
    monitor.run()
    from app.main import asgi
    from tests.test_app import H, setup_owner
    with TestClient(asgi) as c:
        setup_owner(c)
        r = c.get("/api/v1/approvals").json()
        assert "net_revenue" in {x["metric"] for x in r["metric_checks"]}
        assert c.put("/api/v1/metrics/net_revenue/status", headers=H, json={"status": "approved"}).status_code == 200
        assert "net_revenue" not in {x["metric"] for x in c.get("/api/v1/approvals").json()["metric_checks"]}
