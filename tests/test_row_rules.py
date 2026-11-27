"""Row rules for numbers (0.9): metrics only count the rows a data set, team or role may see; a table with rules
is closed to callers no rule covers; company admins see every row; managed from the Access tab (API). Uses the
shipped sample data and semantic layer; no models."""
import pytest
from fastapi.testclient import TestClient

from app import core, engine
from semantic import access as RA

REQ = {"metrics": ["net_revenue"], "dimensions": ["regions.region_name"]}


@pytest.fixture
def fresh(tmp_path):
    core.reset_for_tests(tmp_path / "var")
    engine.clear_ask_cache()
    yield
    RA.SCOPE.set(None)


def regions(scope):
    RA.SCOPE.set(scope)
    try:
        rows = engine.metric_query(REQ)["rows"]
        return sorted(next(v for k, v in r.items() if "region" in k) or "(none)" for r in rows)
    finally:
        RA.SCOPE.set(None)


def test_rules_limit_rows_by_data_set_team_and_role(fresh):
    everything = regions(None)
    assert len(everything) >= 3
    RA.put_rule("regions", "region_name", "dataset", "south-sales", ["South"])
    RA.put_rule("regions", "region_name", "team", "North team", ["North"])
    RA.put_rule("regions", "region_name", "role", "Finance reader", ["North", "South"])
    assert regions(None) == everything                                      # no scope: company admins
    assert regions({"groups": ["public", "south-sales"]}) == ["South"]
    assert regions({"groups": ["public"], "teams": ["North team"]}) == ["North"]
    assert regions({"groups": ["public"], "roles": ["Finance reader"]}) == ["North", "South"]
    assert regions({"groups": ["public"]}) == []                            # rules exist, none for this caller
    RA.SCOPE.set({"groups": ["public", "south-sales"]})                    # the total covers South's orders only
    south_total = engine.metric_query({"metrics": ["net_revenue"]})["rows"][0]
    RA.SCOPE.set(None)
    by_region = {next(v for k, v in r.items() if "region" in k): r["net_revenue"] for r in engine.metric_query(REQ)["rows"]}
    assert round(south_total["net_revenue"], 2) == round(by_region["South"], 2)
    assert RA.delete_rule("regions", "region_name", "dataset", "south-sales")
    with pytest.raises(ValueError):
        RA.put_rule("regions", "region_name", "planet", "x", ["South"])


def test_scope_for_callers(fresh):
    assert RA.scope_for({"kind": "user", "email": "o@example.com", "role": "owner", "all_groups": True, "groups": ["public"]}) is None
    s = RA.scope_for({"kind": "key", "groups": ["public", "finance"]})
    assert s == {"groups": ["public", "finance"], "teams": [], "roles": []}


def test_row_rules_api(tmp_path, monkeypatch):
    core.reset_for_tests(tmp_path / "var")
    monkeypatch.setenv("CL_PUBLIC_HOSTS", "testserver")
    monkeypatch.setattr(engine, "postgres_ok", lambda: (False, "test"))
    from app.main import asgi
    from tests.test_app import H, setup_owner
    with TestClient(asgi) as c:
        setup_owner(c)
        r = c.get("/api/v1/access/row-rules").json()
        assert any(e["name"] == "regions" and "region_name" in e["columns"] for e in r["entities"])
        body = {"entity": "regions", "column": "region_name", "kind": "dataset", "audience": "public", "values": ["South"]}
        assert c.put("/api/v1/access/row-rules", headers=H, json=body).json()["rules"][0]["values"] == ["South"]
        assert c.put("/api/v1/access/row-rules", headers=H, json=body | {"values": []}).status_code == 400
        assert c.delete("/api/v1/access/row-rules?entity=regions&column=region_name&kind=dataset&audience=public",
                        headers=H).json()["rules"] == []
        assert any(e["action"] == "access.row_rule" for e in core.list_events())
