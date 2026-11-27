"""Connecting from Claude / ChatGPT: sign in to Context Layer (OAuth 2.1 + PKCE) and add files from the chat.
Runs the whole flow the way an MCP client does, through a tunnel hostname. No models needed (the rebuild is stubbed)."""
import base64
import hashlib
import re
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlparse

import pytest

from app import core, engine, jobs, oauth
from fastapi.testclient import TestClient

from tests.test_app import H, setup_owner

PUB = {"host": "abc-def.trycloudflare.com"}
BASE = "https://abc-def.trycloudflare.com"
CB = "https://claude.ai/api/mcp/auth_callback"
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
ACCEPT = {"accept": "application/json, text/event-stream"}


@pytest.fixture
def pub(tmp_path, monkeypatch):
    core.reset_for_tests(tmp_path / "var")
    docs, out = tmp_path / "documents", tmp_path / "outputs"
    docs.mkdir(); out.mkdir()
    monkeypatch.setattr(engine, "DOCS_DIR", docs)
    import rag.config
    monkeypatch.setattr(rag.config, "DOCS_DIR", docs)                 # the landing zone (app/storage.py)
    monkeypatch.setattr(engine, "ACCESS_FILE", docs / "_access.json")
    monkeypatch.setattr(engine, "OUT_DIR", out)
    monkeypatch.setattr(engine, "postgres_ok", lambda: (False, "test"))
    monkeypatch.setattr(engine, "graph_ready", lambda: False)
    tables = tmp_path / "tables"
    tables.mkdir()
    monkeypatch.setattr(engine, "save_table", lambda name, data, **kw: [(tables / name).write_bytes(data) and name.rsplit(".", 1)[0]])
    monkeypatch.setenv("CL_PUBLIC_HOSTS", "*.trycloudflare.com")
    oauth._hits.clear()
    built = []
    monkeypatch.setattr(engine, "rebuild", lambda info: built.append(info))
    from app.main import asgi
    with TestClient(asgi) as c:
        setup_owner(c)
        c.cookies.clear()                       # the AI app's browser window is not signed in yet
        c.base_url = "https://testserver"      # the tunnel is https, so the sign-in cookie is Secure
        yield c, built


def pkce():
    v = secrets.token_urlsafe(48)
    return v, base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()


def connect(c, email="o@example.com", password="pass-word-123", client_id=None):
    """Register (DCR) unless a client_id is given, sign in, allow, exchange the code. Returns the token response."""
    if client_id is None:
        r = c.post("/oauth/register", headers=PUB, json={"client_name": "Claude", "redirect_uris": [CB]})
        assert r.status_code == 201, r.text
        client_id = r.json()["client_id"]
    v, ch = pkce()
    q = {"response_type": "code", "client_id": client_id, "redirect_uri": CB, "code_challenge": ch,
         "code_challenge_method": "S256", "state": "st8", "scope": "contextlayer", "resource": f"{BASE}/mcp"}
    r = c.get("/oauth/authorize?" + urlencode(q), headers=PUB)
    assert r.status_code == 200 and "Sign in" in r.text
    r = c.post("/oauth/login", headers=PUB, data=q | {"email": email, "password": password}, follow_redirects=False)
    assert r.status_code == 303, r.text
    r = c.get(r.headers["location"], headers=PUB)
    assert "Allow Claude?" in r.text or "Allow" in r.text
    csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', r.text).group(1)
    r = c.post("/oauth/authorize", headers=PUB, data=q | {"csrf": csrf, "decision": "allow"}, follow_redirects=False)
    assert r.status_code == 303
    loc = urlparse(r.headers["location"])
    back = parse_qs(loc.query)
    assert loc.netloc == "claude.ai" and back["state"] == ["st8"] and back["iss"] == [BASE]
    t = c.post("/oauth/token", headers=PUB, data={"grant_type": "authorization_code", "code": back["code"][0],
                                                   "redirect_uri": CB, "client_id": client_id, "code_verifier": v,
                                                   "resource": f"{BASE}/mcp"})
    assert t.status_code == 200, t.text
    return t.json() | {"client_id": client_id, "code": back["code"][0], "verifier": v}


def mcp(c, token, name, args, host=PUB):
    body = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": args}}
    return c.post("/mcp", json=body, headers=host | ACCEPT | {"authorization": f"Bearer {token}"}).json()["result"]


def test_discovery(pub):
    c, _ = pub
    r = c.post("/mcp", json=INIT, headers=PUB | ACCEPT)
    assert r.status_code == 401
    assert f'resource_metadata="{BASE}/.well-known/oauth-protected-resource/mcp"' in r.headers["www-authenticate"]
    prm = c.get("/.well-known/oauth-protected-resource/mcp", headers=PUB).json()
    assert prm["resource"] == f"{BASE}/mcp" and prm["authorization_servers"] == [BASE]
    md = c.get("/.well-known/oauth-authorization-server", headers=PUB).json()
    assert md["issuer"] == BASE and md["code_challenge_methods_supported"] == ["S256"]
    assert md["client_id_metadata_document_supported"] and "none" in md["token_endpoint_auth_methods_supported"]
    assert c.options("/oauth/token", headers=PUB).headers["access-control-allow-origin"] == "*"
    # the admin web app stays private on the public name
    assert c.get("/api/v1/setup", headers=PUB).status_code == 404


def test_sign_in_and_use(pub):
    c, _ = pub
    t = connect(c)
    assert t["access_token"].startswith("cla_") and t["token_type"] == "Bearer" and t["expires_in"] == 3600
    acc = t["access_token"]
    assert c.post("/mcp", json=INIT, headers=PUB | ACCEPT | {"authorization": f"Bearer {acc}"}).status_code == 200
    r = mcp(c, acc, "list_documents", {})
    assert not r.get("isError")
    # bound to the address it was issued for
    assert c.post("/mcp", json=INIT, headers={"host": "other.trycloudflare.com"} | ACCEPT |
                  {"authorization": f"Bearer {acc}"}).status_code == 401
    # a code works once; PKCE must match
    again = c.post("/oauth/token", headers=PUB, data={"grant_type": "authorization_code", "code": t["code"],
                                                       "redirect_uri": CB, "client_id": t["client_id"],
                                                       "code_verifier": t["verifier"]})
    assert again.status_code == 400 and again.json()["error"] == "invalid_grant"


def test_wrong_pkce_and_redirect(pub):
    c, _ = pub
    cid = c.post("/oauth/register", headers=PUB, json={"client_name": "X", "redirect_uris": [CB]}).json()["client_id"]
    assert c.post("/oauth/register", headers=PUB, json={"redirect_uris": ["http://evil.example.com/cb"]}).status_code == 400
    q = {"response_type": "code", "client_id": cid, "redirect_uri": "https://evil.example.com/cb",
         "code_challenge": "x" * 43, "code_challenge_method": "S256"}
    r = c.get("/oauth/authorize?" + urlencode(q), headers=PUB, follow_redirects=False)
    assert r.status_code == 400 and "return address" in r.text                # never redirects to an unknown address
    q["redirect_uri"], q["code_challenge_method"] = CB, "plain"
    r = c.get("/oauth/authorize?" + urlencode(q), headers=PUB, follow_redirects=False)
    assert r.status_code == 303 and "error=invalid_request" in r.headers["location"]
    r = c.post("/oauth/login", headers=PUB, data={**q, "code_challenge_method": "S256", "email": "o@example.com",
                                                  "password": "wrong-password"})
    assert r.status_code == 401 and "not correct" in r.text


def test_refresh_rotation_and_reuse(pub):
    c, _ = pub
    t = connect(c)
    r = c.post("/oauth/token", headers=PUB, data={"grant_type": "refresh_token", "refresh_token": t["refresh_token"],
                                                   "client_id": t["client_id"]})
    assert r.status_code == 200
    t2 = r.json()
    assert t2["refresh_token"] != t["refresh_token"]
    assert c.post("/mcp", json=INIT, headers=PUB | ACCEPT | {"authorization": f"Bearer {t['access_token']}"}).status_code == 401
    assert c.post("/mcp", json=INIT, headers=PUB | ACCEPT | {"authorization": f"Bearer {t2['access_token']}"}).status_code == 200
    # the old refresh token comes back: treat as stolen and sign the app out
    r = c.post("/oauth/token", headers=PUB, data={"grant_type": "refresh_token", "refresh_token": t["refresh_token"],
                                                   "client_id": t["client_id"]})
    assert r.status_code == 400
    assert c.post("/mcp", json=INIT, headers=PUB | ACCEPT | {"authorization": f"Bearer {t2['access_token']}"}).status_code == 401


def test_deny(pub):
    c, _ = pub
    cid = c.post("/oauth/register", headers=PUB, json={"client_name": "ChatGPT", "redirect_uris": [CB]}).json()["client_id"]
    v, ch = pkce()
    q = {"response_type": "code", "client_id": cid, "redirect_uri": CB, "code_challenge": ch,
         "code_challenge_method": "S256", "state": "s"}
    c.post("/oauth/login", headers=PUB, data=q | {"email": "o@example.com", "password": "pass-word-123"})
    page = c.get("/oauth/authorize?" + urlencode(q), headers=PUB).text
    assert "Allow ChatGPT?" in page
    csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', page).group(1)
    r = c.post("/oauth/authorize", headers=PUB, data=q | {"csrf": "0" * 64, "decision": "allow"}, follow_redirects=False)
    assert r.status_code == 400                                               # forged form
    r = c.post("/oauth/authorize", headers=PUB, data=q | {"csrf": csrf, "decision": "deny"}, follow_redirects=False)
    assert "error=access_denied" in r.headers["location"]


def test_client_id_metadata_document(pub, monkeypatch):
    c, _ = pub
    url = "https://claude.ai/oauth/claude-code-client-metadata"
    monkeypatch.setattr(oauth, "_fetch_cimd", lambda cid: {"name": "Claude Code", "redirect_uris": [CB]} if cid == url else None)
    t = connect(c, client_id=url)
    assert t["access_token"].startswith("cla_")
    assert c.get("/oauth/authorize?" + urlencode({"client_id": "https://unknown.example/x.json", "redirect_uri": CB,
                                                  "response_type": "code"}), headers=PUB).status_code == 400


def test_upload_from_chat(pub):
    c, built = pub
    acc = connect(c)["access_token"]
    r = mcp(c, acc, "add_document", {"filename": "pricing-policy.pdf",
                                      "text": "# Pricing policy\nDiscounts above 20% need CFO approval."})
    sc = r["structuredContent"]
    assert not r.get("isError") and sc["saved"]["file"] == "pricing-policy.md" and sc["saved"]["groups"] == ["public"]
    assert (engine.DOCS_DIR / "pricing-policy.md").read_text().startswith("# Pricing policy")
    for _ in range(50):
        st = mcp(c, acc, "build_status", {"job_id": sc["job_id"]})["structuredContent"]
        if st["status"] == "done":
            break
        time.sleep(0.05)
    assert st["status"] == "done" and st["files"][0]["file"] == "pricing-policy.md" and "Ready" in st["next"]
    assert built and built[-1]["files"][0]["kind"] == "document"
    assert mcp(c, acc, "build_status", {})["structuredContent"]["job_id"] == sc["job_id"]
    # binary files as base64; tables go to data
    csv = base64.b64encode(b"region,amount\nNorth,10\nSouth,20\n").decode()
    r = mcp(c, acc, "add_document", {"filename": "sales.csv", "content_base64": csv})
    assert r["structuredContent"]["saved"]["kind"] == "table"
    assert mcp(c, acc, "add_document", {"filename": "a.md"})["isError"]
    # ChatGPT file params: only public https download addresses
    r = mcp(c, acc, "add_document", {"file": {"download_url": "http://127.0.0.1:8080/x", "file_name": "x.pdf"}})
    assert r["isError"] and "public https" in r["content"][0]["text"]


def test_upload_link(pub):
    c, built = pub
    acc = connect(c)["access_token"]
    link = mcp(c, acc, "create_upload_link", {})["structuredContent"]["upload_url"]
    assert link.startswith(f"{BASE}/upload/clu_")
    path = urlparse(link).path
    assert "Add files" in c.get(path, headers=PUB).text
    r = c.post(path, headers=PUB, files=[("files", ("notes.txt", b"Asha leads the Pune office.", "text/plain"))])
    assert r.status_code == 200 and "Got it" in r.text
    assert (engine.DOCS_DIR / "notes.txt").exists()
    assert c.get("/upload/clu_nope", headers=PUB).status_code == 404


def test_viewer_cannot_upload_and_sees_own_groups(pub):
    c, _ = pub
    c.post("/api/v1/login", headers=H, json={"email": "o@example.com", "password": "pass-word-123"})
    uid = c.post("/api/v1/users", headers=H, json={"email": "v@example.com", "name": "V", "role": "viewer",
                                                   "password": "pass-word-456"}).json()["user"]["id"]
    assert c.patch(f"/api/v1/users/{uid}", headers=H, json={"groups": ["sales"]}).json()["user"]["groups"] == ["sales"]
    c.cookies.clear()
    acc = connect(c, "v@example.com", "pass-word-456")["access_token"]
    r = mcp(c, acc, "add_document", {"filename": "x.md", "text": "hello"})
    assert r["isError"] and "may not use upload" in r["content"][0]["text"]
    assert mcp(c, acc, "list_documents", {})["structuredContent"]["your_groups"] == ["sales"]
    assert mcp(c, acc, "approve_metric", {"name": "net_revenue"})["isError"]
    # disabling the user signs their apps out
    c.cookies.clear()
    c.post("/api/v1/login", headers=H, json={"email": "o@example.com", "password": "pass-word-123"})
    apps = c.get("/api/v1/connected-apps", headers=H).json()["apps"]
    assert {a["user"] for a in apps} == {"v@example.com"}
    c.patch(f"/api/v1/users/{uid}", headers=H, json={"disabled": True})
    assert c.post("/mcp", json=INIT, headers=PUB | ACCEPT | {"authorization": f"Bearer {acc}"}).status_code == 401


def test_disconnect_app(pub):
    c, _ = pub
    acc = connect(c)["access_token"]
    apps = c.get("/api/v1/connected-apps", headers=H).json()["apps"]
    assert len(apps) == 1 and apps[0]["app"] == "Claude"
    assert c.post(f"/api/v1/connected-apps/{apps[0]['id']}/revoke", headers=H).json()["ok"]
    assert c.post("/mcp", json=INIT, headers=PUB | ACCEPT | {"authorization": f"Bearer {acc}"}).status_code == 401


def test_key_with_upload_limited_to_its_groups(pub):
    c, _ = pub
    c.post("/api/v1/login", headers=H, json={"email": "o@example.com", "password": "pass-word-123"})
    tok = c.post("/api/v1/agents", headers=H, json={"name": "HR bot", "groups": ["hr"],
                                                     "capabilities": ["documents", "upload"]}).json()["token"]
    r = mcp(c, tok, "add_document", {"filename": "a.md", "text": "x", "groups": ["public"]}, host={})
    assert r["isError"] and "your own groups" in r["content"][0]["text"]
    r = mcp(c, tok, "add_document", {"filename": "a.md", "text": "x"}, host={})
    assert r["structuredContent"]["saved"]["groups"] == ["hr"]
    with core.tenant_scope(c.get("/api/v1/me").json()["user"]["tenant_id"]):     # jobs belong to a workspace
        jobs_ = [j for j in jobs.list_jobs() if j["kind"] == "rebuild"]
    assert jobs_
