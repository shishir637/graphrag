"""Security hardening: graph evidence per passage, delegate keys acting for an app's end users, key expiry and
IP allow-lists, audit retention and redaction, and that deleting a document leaves nothing behind. No models."""
import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from app import core, delegate, engine, jobs, vault
from tests.conftest import needs_pg

pytestmark = needs_pg
H = {"X-CL": "1"}
MH = {"accept": "application/json, text/event-stream"}


# ------------------------------------------------------------------ 1. graph evidence per passage

def test_graph_shows_only_evidence_from_visible_passages(tmp_path, monkeypatch):
    from graph import search as GS
    from graph.build import aggregate
    rels = [{"s": "a", "t": "b", "type": "LEADS", "strength": 5, "chunk_id": "pub1",
             "description": "Asha leads the Orion team."},
            {"s": "a", "t": "b", "type": "LEADS", "strength": 5, "chunk_id": "hr1",
             "description": "Asha leads Orion while on a performance plan (confidential)."}]
    agg = aggregate(rels, {"a": "e_a", "b": "e_b"})
    assert [e["chunk_id"] for e in agg[0]["evidence"]] == ["pub1", "hr1"]
    build = {"entities": [
        {"id": "e_a", "name": "Asha Rao", "type": "Person", "aliases": ["Agent Falcon"], "chunk_ids": ["pub1", "hr1"],
         "names_evidence": [{"text": "Asha Rao", "chunk_ids": ["pub1"]}, {"text": "Agent Falcon", "chunk_ids": ["hr1"]}],
         "description_evidence": [{"text": "Team lead", "chunk_ids": ["pub1"]},
                                  {"text": "On a performance plan", "chunk_ids": ["hr1"]}]},
        {"id": "e_b", "name": "Orion", "type": "Organisation", "aliases": [], "chunk_ids": ["pub1", "hr1"]}],
        "relations": [agg[0]], "communities": []}
    f = tmp_path / "_graph_build.json"
    f.write_text(json.dumps(build))
    monkeypatch.setattr(GS, "BUILD", f)
    from graph import kg
    monkeypatch.setattr(kg, "OUT_DIR", tmp_path)                 # command line: the graph file (0.7: kg.py)
    GS._GS.clear()
    access = {"pub1": ["public"], "hr1": ["hr"]}
    vis = lambda c, g: g is None or bool(set(access.get(c, [])) & set(g))          # noqa: E731
    pub = GS.local("Who leads Orion? Asha Rao", ["public"], vis)
    text = json.dumps(pub)
    assert "confidential" not in text and "Agent Falcon" not in text and "Asha leads the Orion team." in text
    e = GS.entity("Asha Rao", ["public"], vis)
    assert e["aliases"] == [] and "performance plan" not in json.dumps(e) and e["description"] == "Team lead"
    hr = GS.entity("Asha Rao", ["public", "hr"], vis)
    assert hr["aliases"] == ["Agent Falcon"] and "performance plan" in hr["description"]


# ------------------------------------------------------------------ app fixture

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


# ------------------------------------------------------------------ 2. delegate keys

def test_delegate_key_acts_for_each_end_user_only(client):
    c = client
    r = c.post("/api/v1/agents", headers=H, json={"name": "CodeVerse", "scope": "delegate", "groups": [],
                                                  "capabilities": ["documents", "graph"]}).json()
    token, secret = r["token"], r["agent"]["signing_secret"]
    assert c.get("/api/v1/agents").json()["agents"][0].get("signing_secret") is None      # shown once
    c.put("/api/v1/external-users/u-ann", headers=H, json={"groups": ["repo-acme-api"]})
    c.put("/api/v1/external-users/u-ben", headers=H, json={"groups": ["repo-acme-web"]})
    assert mcp(c, token).status_code == 401                                                  # no identity
    ok = mcp(c, token, delegate.sign_headers("u-ann", secret))
    assert ok.status_code == 200 and ok.json()["result"]["structuredContent"]["your_groups"] == ["repo-acme-api"]
    jwt = delegate.make_token("u-ben", secret)
    for _ in range(2):                                                                       # reusable until exp
        r2 = mcp(c, token, {"x-cl-user-token": jwt})
        assert r2.json()["result"]["structuredContent"]["your_groups"] == ["repo-acme-web"]
    hdr = delegate.sign_headers("u-ann", secret)
    assert mcp(c, token, hdr).status_code == 200
    replay = mcp(c, token, hdr)
    assert replay.status_code == 401 and "replay" in replay.json()["error_description"]
    assert mcp(c, token, delegate.sign_headers("u-ann", secret, ts=int(time.time()) - 600)).status_code == 401
    assert mcp(c, token, delegate.sign_headers("u-ann", "cls_sign_forged")).status_code == 401
    assert mcp(c, token, {"x-cl-user-token": delegate.make_token("u-ben", secret, ttl=3600)}).status_code == 401
    assert mcp(c, token, {"x-cl-user-token": delegate.make_token("u-ben", "wrong")}).status_code == 401
    assert mcp(c, token, delegate.sign_headers("u-nobody", secret)).status_code == 401       # unknown user
    ev = [e for e in c.get("/api/v1/events?type=agent").json()["events"] if e["action"] == "tool.list_documents"]
    assert {e["detail"].get("end_user") for e in ev} == {"u-ann", "u-ben"} and all("CodeVerse for" in e["actor"] for e in ev)


# ------------------------------------------------------------------ 3. expiry and IP allow-lists

def test_key_expiry_and_ip_allow_list(client, monkeypatch):
    c = client
    old = c.post("/api/v1/agents", headers=H, json={"name": "Old", "groups": ["public"],
                                                    "expires": "2020-01-01T00:00:00Z"}).json()["token"]
    assert mcp(c, old).status_code == 401
    assert c.post("/api/v1/agents", headers=H, json={"name": "Bad", "groups": ["public"],
                                                     "allowed_ips": ["not-an-ip"]}).status_code == 400
    office = c.post("/api/v1/agents", headers=H, json={"name": "Office", "groups": ["public"],
                                                       "allowed_ips": ["10.0.0.0/8"]}).json()["token"]
    monkeypatch.setenv("CL_TRUST_PROXY", "1")
    assert mcp(c, office, {"x-forwarded-for": "10.1.2.3"}).status_code == 200
    assert mcp(c, office, {"x-forwarded-for": "192.168.1.9"}).status_code == 401
    monkeypatch.delenv("CL_TRUST_PROXY")
    assert mcp(c, office, {"x-forwarded-for": "10.1.2.3"}).status_code == 401              # header not trusted


# ------------------------------------------------------------------ 5. audit retention and redaction

def test_retention_and_redacted_questions(client):
    c = client
    tid = c.get("/api/v1/me").json()["user"]["tenant_id"]
    assert c.put("/api/v1/settings/privacy", headers=H, json={"event_retention_days": 30, "redact_questions": True}).status_code == 200
    token = c.post("/api/v1/agents", headers=H, json={"name": "Bot", "groups": ["public"]}).json()["token"]
    mcp(c, token, tool="search_documents", args={"query": "salary of Asha Rao"})
    with core.tenant_scope(tid):
        ev = [e for e in core.list_events(actor_type="agent") if e["action"] == "tool.search_documents"]
        assert ev and "Asha" not in json.dumps(ev) and ev[0]["target"].startswith("sha256:")
        core.db().execute("UPDATE events SET time = '2020-01-01T00:00:00+00:00' WHERE id = ?", (ev[0]["id"],))
        assert core.purge_events(30) == 1
        assert all(e["id"] != ev[0]["id"] for e in core.list_events())


# ------------------------------------------------------------------ 6. deleting a document leaves nothing behind

def test_deleted_document_leaves_no_points_chunks_or_access(tmp_path, monkeypatch):
    import httpx
    from rag import store
    from rag.config import OUT_DIR
    from rag.retrieve import _chunk_index, build_filter
    from graph.access import chunk_access
    url = os.environ.get("CL_TEST_QDRANT_URL", "http://127.0.0.1:6399")
    try:
        httpx.get(url + "/collections", timeout=3)
    except Exception:                                      # noqa: BLE001
        pytest.skip("no test Qdrant")
    core.reset_for_tests(tmp_path / "var")
    monkeypatch.setenv("QDRANT_URL", url)
    monkeypatch.setattr(store, "_client", None)
    from tests.test_workspace_storage import _write
    t = core.create_tenant("Acme")
    with core.tenant_scope(t):
        _write(OUT_DIR.resolve_now(), {"keep.md": ["kept passage"], "gone.md": ["secret passage one", "secret two"]})
        store.build_index()
        assert "gone.md" in {c["source"] for cs in _chunk_index()["by_source"].values() for c in cs}
        time.sleep(1.1)                                    # file times must differ for the caches to notice
        _write(OUT_DIR.resolve_now(), {"keep.md": ["kept passage"]})
        assert store.build_index()["deleted"] == 2
        pts, _ = store.client().scroll("docs", scroll_filter=build_filter(None, None), limit=100)
        assert {p.payload["source"] for p in pts} == {"keep.md"}
        assert set(_chunk_index()["by_source"]) == {"keep.md"}
        assert all(not k.startswith("gone") for k in chunk_access())
        assert engine._sources_version() != ()                          # the ask cache key follows the files
