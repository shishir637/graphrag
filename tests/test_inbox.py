"""The owner's inbox: uncertain facts (with quote and source) and name matches in one list with drafts; approve,
reject or correct; decisions survive rebuilds and reach fact tables and cards. Over the API and the admin tool."""
import pytest
from fastapi.testclient import TestClient

from app import core, engine, vault
from tests.conftest import needs_pg
from tests.test_facts import MSA, write_chunks

pytestmark = needs_pg
H = {"X-CL": "1"}


@pytest.fixture
def c(tmp_path, monkeypatch):
    core.reset_for_tests(tmp_path / "var")
    vault.reset_for_tests()
    monkeypatch.setenv("CL_PUBLIC_HOSTS", "testserver")
    monkeypatch.setenv("CL_SEED_LAYER", "0")
    monkeypatch.setattr(engine, "postgres_ok", lambda: (False, "test"))
    from app import main
    main._attempts.clear()
    with TestClient(main.asgi) as client:
        r = client.post("/api/v1/setup", headers=H, json={"name": "O", "email": "o@acme.io", "password": "pass-word-123",
                                                          "organisation": "Acme"})
        tid = r.json()["user"]["tenant_id"]
        with core.tenant_scope(tid):
            from facts import store
            write_chunks({"MSA-001.txt": (MSA.replace("15 days'", "400 days'"), ["public"])})
            store.run(verbose=False)
        yield client, tid


def test_inbox_lists_uncertain_facts_and_decisions_stick(c):
    client, tid = c
    items = client.get("/api/v1/inbox").json()["items"]
    fact = next(i for i in items if i["type"] == "fact")
    assert "notice" in fact["title"] and "400 days" in fact["quote"] and fact["document"] == "MSA-001.txt"
    assert "above 365" in fact["reason"]
    r = client.post(f"/api/v1/inbox/fact/{fact['id']}", headers=H, json={"action": "edit", "value": "40"})
    assert r.status_code == 200
    from facts import store
    with core.tenant_scope(tid):
        f = next(x for x in store.facts_for(["public"]) if x["field"] == "notice_days")
        assert f["norm"] == 40 and f["value"] == "40"
        assert store.doc_card("MSA-001.txt", ["public"])["key_facts"]["notice_days"] == 40
        store.run(verbose=False)                                    # a rebuild keeps the owner's correction
        assert next(x for x in store.facts_for(["public"]) if x["field"] == "notice_days")["norm"] == 40
    assert not any(i["type"] == "fact" for i in client.get("/api/v1/inbox").json()["items"])
    assert client.post(f"/api/v1/inbox/fact/{fact['id']}", headers=H, json={"action": "edit", "value": "soon"}).status_code == 400
    assert client.post("/api/v1/inbox/fact/f_nope", headers=H, json={"action": "approve"}).status_code == 404


def test_admin_tool_and_reject(c):
    client, tid = c
    tok = client.post("/api/v1/agents", headers=H, json={"name": "Setup", "groups": ["public"], "scope": "admin"}).json()["token"]
    mh = {"accept": "application/json, text/event-stream", "authorization": f"Bearer {tok}"}
    res = client.post("/mcp", headers=mh, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                "params": {"name": "review_inbox", "arguments": {}}}).json()["result"]
    ref = next(w["ref"] for w in res["structuredContent"]["waiting"] if w["ref"].startswith("fact/"))
    res = client.post("/mcp", headers=mh, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                                "params": {"name": "review_inbox", "arguments": {"reject": [ref]}}}).json()["result"]
    assert res["structuredContent"]["decided"][0]["action"] == "reject"
    from facts import store
    with core.tenant_scope(tid):
        assert not [f for f in store.facts_for(None, status="rejected") if f["field"] != "notice_days"]
        assert [f["field"] for f in store.facts_for(None, status="rejected")] == ["notice_days"]
