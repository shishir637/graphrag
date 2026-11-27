"""Keys for apps (0.9): a delegate key acts for each of an app's end users only (signed one-time headers or a short
HS256 token, with that user's data sets and roles, never more than the key), keys can expire and be limited to IP
ranges. Ported from dev tests/test_security.py. No models."""
import time

import pytest
from fastapi.testclient import TestClient

from app import core, delegate, engine, vault

H = {"X-CL": "1"}
MH = {"accept": "application/json, text/event-stream"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    core.reset_for_tests(tmp_path / "var")
    vault.reset_for_tests()
    for name, path in (("DOCS_DIR", tmp_path / "documents"), ("OUT_DIR", tmp_path / "outputs")):
        path.mkdir()
        monkeypatch.setattr(engine, name, path)
    monkeypatch.setattr(engine, "ACCESS_FILE", tmp_path / "documents" / "_access.json")
    monkeypatch.setattr(engine, "postgres_ok", lambda: (False, "test"))
    monkeypatch.setenv("CL_PUBLIC_HOSTS", "testserver")
    from app import main
    main._attempts.clear()
    with TestClient(main.asgi) as c:
        r = c.post("/api/v1/setup", headers=H, json={"name": "O", "email": "o@acme.io", "password": "pass-word-123",
                                                      "organisation": "Acme"})
        assert r.status_code == 200
        yield c


def mcp(c, token, extra=None, tool="list_documents", args=None):
    return c.post("/mcp", headers=MH | {"authorization": f"Bearer {token}"} | (extra or {}),
                  json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args or {}}})


def groups_of(r):
    assert r.status_code == 200, r.text
    return r.json()["result"]["structuredContent"]["your_groups"]


def test_delegate_key_acts_for_each_end_user_only(client):
    c = client
    c.post("/api/v1/access/datasets", headers=H, json={"name": "repo-api"})
    c.post("/api/v1/access/datasets", headers=H, json={"name": "repo-web"})
    c.post("/api/v1/access/roles", headers=H, json={"name": "Web dev", "datasets": ["repo-web"], "connectors": ["documents"]})
    r = c.post("/api/v1/agents", headers=H, json={"name": "CodeVerse", "scope": "delegate",
                                                  "groups": ["public", "repo-api", "repo-web"],
                                                  "capabilities": ["documents", "graph"]}).json()
    token, secret = r["token"], r["signing_secret"]
    assert "signing_secret" not in c.get("/api/v1/agents").text                              # shown once
    assert c.put("/api/v1/external-users/u-ann", headers=H, json={"datasets": ["repo-api"]}).status_code == 200
    assert c.put("/api/v1/external-users/u-ben", headers=H, json={"datasets": [], "roles": ["Web dev"]}).status_code == 200
    assert c.put("/api/v1/external-users/u-x", headers=H, json={"roles": ["Nope"]}).status_code == 400
    assert mcp(c, token).status_code == 401                                                  # no identity
    assert groups_of(mcp(c, token, delegate.sign_headers("u-ann", secret))) == ["repo-api"]
    jwt = delegate.make_token("u-ben", secret)
    for _ in range(2):                                                                       # reusable until exp
        assert groups_of(mcp(c, token, {"x-cl-user-token": jwt})) == ["repo-web"]           # through the role
    hdr = delegate.sign_headers("u-ann", secret)
    assert mcp(c, token, hdr).status_code == 200
    replay = mcp(c, token, hdr)
    assert replay.status_code == 401 and "replay" in replay.json()["error_description"]
    assert mcp(c, token, delegate.sign_headers("u-ann", secret, ts=int(time.time()) - 600)).status_code == 401
    assert mcp(c, token, delegate.sign_headers("u-ann", "cls_sign_forged")).status_code == 401
    assert mcp(c, token, {"x-cl-user-token": delegate.make_token("u-ben", secret, ttl=3600)}).status_code == 401
    assert mcp(c, token, {"x-cl-user-token": delegate.make_token("u-ben", "wrong")}).status_code == 401
    assert mcp(c, token, delegate.sign_headers("u-nobody", secret)).status_code == 401       # unknown user
    # never more than the key: a data set the key lacks is dropped
    r2 = c.post("/api/v1/agents", headers=H, json={"name": "Narrow", "scope": "delegate", "groups": ["repo-api"],
                                                   "capabilities": ["documents"]}).json()
    c.put("/api/v1/external-users/u-cy", headers=H, json={"datasets": ["repo-api", "repo-web"]})
    assert groups_of(mcp(c, r2["token"], delegate.sign_headers("u-cy", r2["signing_secret"]))) == ["repo-api"]
    ev = [e for e in core.list_events(actor_type="agent") if e["action"] == "tool.list_documents" and e["ok"]]
    assert {"u-ann", "u-ben"} <= {e["detail"].get("end_user") for e in ev}
    assert all(" for " in e["actor"] for e in ev)
    assert c.delete("/api/v1/external-users/u-ann", headers=H).status_code == 200
    assert mcp(c, token, delegate.sign_headers("u-ann", secret)).status_code == 401


def test_key_expiry_and_ip_allow_list(client, monkeypatch):
    c = client
    soon = c.post("/api/v1/agents", headers=H, json={"name": "Soon", "groups": ["public"], "expires_days": 1}).json()
    assert soon["agent"]["expires"] and mcp(c, soon["token"]).status_code == 200
    core.db().execute("UPDATE agents SET expires = '2020-01-01T00:00:00+00:00' WHERE id = ?", (soon["agent"]["id"],))
    assert mcp(c, soon["token"]).status_code == 401                                         # expired
    assert c.post("/api/v1/agents", headers=H, json={"name": "Bad", "groups": ["public"],
                                                     "allowed_ips": ["not-an-ip"]}).status_code == 400
    office = c.post("/api/v1/agents", headers=H, json={"name": "Office", "groups": ["public"],
                                                       "allowed_ips": ["10.0.0.0/8"]}).json()["token"]
    monkeypatch.setenv("CL_TRUST_PROXY", "1")
    assert mcp(c, office, {"x-forwarded-for": "10.1.2.3"}).status_code == 200
    r = mcp(c, office, {"x-forwarded-for": "192.168.1.9"})
    assert r.status_code == 401 and "address" in r.json()["error_description"]
    monkeypatch.delenv("CL_TRUST_PROXY")
    assert mcp(c, office, {"x-forwarded-for": "10.1.2.3"}).status_code == 401              # header not trusted
