"""Chat with each person's own Claude API key: key storage, the tool loop, files from the chat, access, errors.
Claude is faked (no key or network needed); the Context Layer tools are stubbed (no search models needed)."""
import json

import pytest
from fastapi.testclient import TestClient

from app import chat, core, engine
from app import mcp_server as M
from tests.test_app import H, setup_owner

KEY = "sk-ant-api03-" + "x" * 40
PID = "a1b2c3d4e5f6"


class FakeClaude:
    def __init__(self):
        self.calls, self.script = [], []

    def __call__(self, key, body):
        self.calls.append((key, json.loads(json.dumps(body))))
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def tool_use(name, inp, i="tu1"):
    return {"stop_reason": "tool_use", "usage": {"input_tokens": 100, "output_tokens": 20},
            "content": [{"type": "text", "text": "Let me look."}, {"type": "tool_use", "id": i, "name": name, "input": inp}]}


def answer(text):
    return {"stop_reason": "end_turn", "usage": {"input_tokens": 300, "output_tokens": 50},
            "content": [{"type": "text", "text": text}]}


@pytest.fixture
def env(tmp_path, monkeypatch):
    core.reset_for_tests(tmp_path / "var")
    chat._ready_for = None
    docs, out = tmp_path / "documents", tmp_path / "outputs"
    docs.mkdir(); out.mkdir()
    monkeypatch.setattr(engine, "DOCS_DIR", docs)
    monkeypatch.setattr(engine, "ACCESS_FILE", docs / "_access.json")
    monkeypatch.setattr(engine, "OUT_DIR", out)
    monkeypatch.setattr(engine, "postgres_ok", lambda: (False, "test"))
    monkeypatch.setattr(engine, "graph_ready", lambda: False)
    monkeypatch.setenv("CL_PUBLIC_HOSTS", "testserver")
    for k in (chat.KEY_ENV_COMPANY, chat.MASTER_ENV):
        monkeypatch.delenv(k, raising=False)
    built = []
    monkeypatch.setattr(engine, "rebuild", lambda info: built.append(info))
    monkeypatch.setattr(chat, "verify_key", lambda key: None)
    asked = []

    def fake_ask(agent, question, mode="auto"):
        asked.append((agent["groups"], question))
        return {"route": "documents", "passages": [{"id": PID, "source": "leave-policy.pdf", "section": "Leave",
                                                     "pages": [2], "text": "Leave over 10 days needs HR approval."}]}
    monkeypatch.setitem(M.TOOLS, "ask", fake_ask)
    fake = FakeClaude()
    monkeypatch.setattr(chat, "call_claude", fake)
    from app import main
    main._attempts.clear()
    with TestClient(main.asgi) as c:
        setup_owner(c)
        yield c, fake, built, asked
    main._attempts.clear()                # sign-ins here must not use up the login rate limit of later tests


def events(r):
    assert r.status_code == 200, r.text
    return [json.loads(x) for x in r.text.splitlines() if x.strip()]


def new_chat(c):
    return c.post("/api/v1/chats", headers=H).json()["chat"]["id"]


def as_viewer(c, email="v@example.com"):
    c.post("/api/v1/users", headers=H, json={"email": email, "name": "Vee", "role": "viewer", "password": "viewer-pass-1"})
    c.post("/api/v1/logout", headers=H)
    c.post("/api/v1/login", headers=H, json={"email": email, "password": "viewer-pass-1"})


def test_key_is_checked_encrypted_and_never_returned(env):
    c, *_ = env
    me = c.get("/api/v1/chat/me").json()
    assert me["key_set"] is False and me["can_chat"] is False
    assert c.put("/api/v1/chat/key", headers=H, json={"api_key": "not-a-key", "model": ""}).status_code == 400
    r = c.put("/api/v1/chat/key", headers=H, json={"api_key": KEY, "model": "claude-haiku-5-5"})
    assert r.status_code == 200 and r.json()["key_set"] and r.json()["model"] == "claude-haiku-5-5"
    assert KEY not in r.text and r.json()["key"].startswith("sk-ant-")
    row = chat.db().execute("SELECT enc FROM chat_keys").fetchone()
    assert KEY not in row["enc"] and chat.decrypt(row["enc"]) == KEY
    assert KEY not in (core.DATA_DIR / "app.db").read_bytes().decode("latin-1")
    # model only, key kept
    assert c.put("/api/v1/chat/key", headers=H, json={"model": "claude-opus-5-5"}).json()["model"] == "claude-opus-5-5"
    assert c.put("/api/v1/chat/key", headers=H, json={"api_key": "", "model": ""}).json()["key_set"] is False


def test_chat_turn_uses_tools_and_cites_sources(env):
    c, fake, _, asked = env
    c.put("/api/v1/chat/key", headers=H, json={"api_key": KEY, "model": ""})
    cid = new_chat(c)
    fake.script = [tool_use("ask", {"question": "who approves long leave?"}),
                   answer(f"HR approves leave over 10 days [{PID}].")]
    ev = events(c.post(f"/api/v1/chats/{cid}/messages", headers=H, json={"text": "Who approves long leave?"}))
    assert [e["type"] for e in ev][-1] == "answer"
    assert any(e["type"] == "step" and e["step"]["tool"] == "ask" for e in ev)
    msg = ev[-1]["message"]
    assert msg["sources"][0]["source"] == "leave-policy.pdf" and msg["key"] == "own"
    assert msg["usage"] == {"input_tokens": 400, "output_tokens": 70}
    # what Claude got: the person's key, tools, the tool result on the second call
    key, body = fake.calls[0]
    assert key == KEY and body["model"] == chat.DEFAULT_MODEL
    assert {t["name"] for t in body["tools"]} >= {"ask", "search_documents", "query_metrics", "build_status"}
    assert not {"add_document", "approve_metric", "create_upload_link"} & {t["name"] for t in body["tools"]}
    second = fake.calls[1][1]["messages"]
    assert second[-1]["content"][0]["type"] == "tool_result" and PID in second[-1]["content"][0]["content"]
    assert asked and "public" in asked[0][0]
    # the page shows two messages; tool calls are folded into the answer's steps
    h = c.get(f"/api/v1/chats/{cid}").json()
    assert [m["role"] for m in h["messages"]] == ["user", "assistant"]
    assert h["chat"]["title"].startswith("Who approves")
    assert h["messages"][1]["steps"][0]["summary"].startswith("1 passages")
    # the next turn carries the whole history in order, with alternating roles
    fake.script = [answer("You're welcome.")]
    events(c.post(f"/api/v1/chats/{cid}/messages", headers=H, json={"text": "thanks"}))
    roles = [m["role"] for m in fake.calls[-1][1]["messages"]]
    assert roles == ["user", "assistant", "user", "assistant", "user"]
    assert any(e["action"] == "tool.ask" and e["actor"].endswith("via chat") for e in core.list_events())


def test_files_from_the_chat(env, tmp_path):
    c, fake, built, _ = env
    c.put("/api/v1/chat/key", headers=H, json={"api_key": KEY, "model": ""})
    cid = new_chat(c)
    r = c.post(f"/api/v1/chats/{cid}/files", headers=H, files=[("files", ("pricing.md", b"# Pricing\nGold plan costs 90.", "text/markdown"))])
    assert r.status_code == 200, r.text
    f = r.json()["files"][0]
    assert f["file"] == "pricing.md" and f["kind"] == "document" and r.json()["job_id"]
    assert (engine.DOCS_DIR / "pricing.md").exists()
    fake.script = [answer("Gold costs 90 (pricing.md).")]
    ev = events(c.post(f"/api/v1/chats/{cid}/messages", headers=H, json={"text": "What does Gold cost?", "uploads": [f]}))
    assert ev[-1]["type"] == "answer"
    sent = fake.calls[0][1]["messages"][-1]["content"]
    assert sent[0]["type"] == "document" and "Gold plan costs 90" in sent[0]["source"]["data"]
    assert "pricing.md" in sent[1]["text"] and sent[-1]["text"] == "What does Gold cost?"
    # history keeps the note and the file chip, not the file's content
    h = c.get(f"/api/v1/chats/{cid}").json()
    assert h["messages"][0]["text"] == "What does Gold cost?" and h["messages"][0]["uploads"][0]["file"] == "pricing.md"
    stored = chat.db().execute("SELECT content FROM chat_messages ORDER BY id LIMIT 1").fetchone()["content"]
    assert "Gold plan costs 90" not in stored
    # a ref from someone else's folder is ignored
    blocks, kept = chat._file_blocks(999, [f])
    assert blocks == [] and kept == []


def test_access_company_key_and_upload_rule(env):
    c, fake, _, asked = env
    cid = new_chat(c)
    as_viewer(c)
    assert c.get(f"/api/v1/chats/{cid}").status_code == 404                 # someone else's chat
    mine = new_chat(c)
    ev = events(c.post(f"/api/v1/chats/{mine}/messages", headers=H, json={"text": "hi"}))
    assert ev[-1]["type"] == "error" and "API key" in ev[-1]["text"]
    assert c.put("/api/v1/settings/chat", headers=H, json={"company_key_for": "everyone"}).status_code == 403
    # the owner allows the company key and limits uploads to admins
    c.post("/api/v1/logout", headers=H)
    c.post("/api/v1/login", headers=H, json={"email": "o@example.com", "password": "pass-word-123"})
    r = c.put("/api/v1/settings/chat", headers=H, json={"company_key_for": "everyone", "uploads": "admins",
                                                      "company_model": "claude-haiku-5-5", "company_key": KEY})
    assert r.status_code == 200 and r.json()["company_key_set"] and KEY not in r.text
    c.post("/api/v1/logout", headers=H)
    c.post("/api/v1/login", headers=H, json={"email": "v@example.com", "password": "viewer-pass-1"})
    me = c.get("/api/v1/chat/me").json()
    assert me["can_chat"] and me["company_key_available"] and not me["can_upload"]
    fake.script = [tool_use("ask", {"question": "x"}), answer("ok")]
    ev = events(c.post(f"/api/v1/chats/{mine}/messages", headers=H, json={"text": "hi"}))
    assert ev[-1]["message"]["key"] == "company" and fake.calls[-1][1]["model"] == "claude-haiku-5-5"
    assert asked[-1][0] == ["public"]                                         # the viewer's own groups only
    r = c.post(f"/api/v1/chats/{mine}/files", headers=H, files=[("files", ("a.txt", b"hello", "text/plain"))])
    assert r.status_code == 400 and "not allowed" in r.json()["detail"]


def test_failed_turn_is_kept_but_not_replayed(env):
    c, fake, *_ = env
    c.put("/api/v1/chat/key", headers=H, json={"api_key": KEY, "model": ""})
    cid = new_chat(c)
    fake.script = [tool_use("ask", {"question": "x"}), chat.ChatError("Claude refused the API key. Update it under My Claude key.")]
    ev = events(c.post(f"/api/v1/chats/{cid}/messages", headers=H, json={"text": "first"}))
    assert ev[-1] == {"type": "error", "text": "Claude refused the API key. Update it under My Claude key."}
    h = c.get(f"/api/v1/chats/{cid}").json()["messages"]
    assert len(h) == 1 and h[0]["error"].startswith("Claude refused")
    fake.script = [answer("hello")]
    events(c.post(f"/api/v1/chats/{cid}/messages", headers=H, json={"text": "second"}))
    msgs = fake.calls[-1][1]["messages"]
    assert len(msgs) == 1 and msgs[0]["content"][-1]["text"] == "second"


def test_bad_tool_arguments_go_back_to_claude(env):
    c, fake, *_ = env
    c.put("/api/v1/chat/key", headers=H, json={"api_key": KEY, "model": ""})
    cid = new_chat(c)
    fake.script = [tool_use("ask", {"nonsense": 1}), tool_use("add_document", {"text": "x"}, "tu2"), answer("sorry")]
    ev = events(c.post(f"/api/v1/chats/{cid}/messages", headers=H, json={"text": "q"}))
    steps = [e["step"] for e in ev if e["type"] == "step"]
    assert [s["ok"] for s in steps] == [False, False]
    r1 = fake.calls[1][1]["messages"][-1]["content"][0]
    assert r1["is_error"] and "Bad arguments" in r1["content"]
    assert "Unknown tool" in fake.calls[2][1]["messages"][-1]["content"][0]["content"]


def test_encryption_round_trip_and_tamper(env):
    t = chat.encrypt("sk-ant-secret-value")
    assert chat.decrypt(t) == "sk-ant-secret-value" and chat.encrypt("sk-ant-secret-value") != t
    raw = bytearray(__import__("base64").urlsafe_b64decode(t)); raw[20] ^= 1
    with pytest.raises(chat.ChatError):
        chat.decrypt(__import__("base64").urlsafe_b64encode(bytes(raw)).decode())


def test_guests_chat_without_signing_in_on_this_computer_only(env):
    c, fake, built, asked = env
    r = c.put("/api/v1/settings/chat", headers=H, json={"guests": True})
    assert r.status_code == 400 and "company Claude API key" in r.json()["detail"]
    assert c.put("/api/v1/settings/chat", headers=H, json={"guests": True, "company_key": KEY,
                                                           "company_model": "claude-haiku-5-5"}).json()["guests"]
    c.post("/api/v1/logout", headers=H)
    c.cookies.clear()
    assert c.get("/api/v1/setup").json()["guest_chat"] is True
    assert c.get("/api/v1/me").status_code == 401                              # the admin app still needs sign-in
    me = c.get("/api/v1/chat/me")
    assert me.status_code == 200 and me.json()["guest"] and me.json()["can_chat"] and me.json()["groups"] == ["public"]
    assert "cl_guest" in me.headers.get("set-cookie", "")
    cid = new_chat(c)
    fake.script = [tool_use("ask", {"question": "x"}), answer("ok")]
    ev = events(c.post(f"/api/v1/chats/{cid}/messages", headers=H, json={"text": "hello"}))
    assert ev[-1]["message"]["key"] == "company" and fake.calls[-1][0] == KEY and fake.calls[-1][1]["model"] == "claude-haiku-5-5"
    assert asked[-1][0] == ["public"]
    assert c.put("/api/v1/chat/key", headers=H, json={"api_key": KEY}).status_code == 403
    r = c.post(f"/api/v1/chats/{cid}/files", headers=H, files=[("files", ("n.txt", b"note", "text/plain"))])
    assert r.status_code == 200 and r.json()["files"][0]["groups"] == ["public"]
    assert c.get(f"/api/v1/chat/jobs/{r.json()['job_id']}").json()["job"]["status"] in ("queued", "running", "done")
    # another browser is another guest: it can't see this chat
    other = c.cookies.get("cl_guest")
    c.cookies.clear()
    assert c.get(f"/api/v1/chats/{cid}").status_code == 404
    assert c.get("/api/v1/chats").json()["chats"] == []
    # guest accounts are hidden, can't sign in, can't be enabled
    c.cookies.set("cl_guest", other, domain="testserver.local")
    assert c.get("/api/v1/chats").json()["chats"][0]["id"] == cid
    c.post("/api/v1/login", headers=H, json={"email": "o@example.com", "password": "pass-word-123"})
    users = c.get("/api/v1/users").json()["users"]
    assert [u["email"] for u in users] == ["o@example.com"]
    gid = core.db().execute("SELECT id FROM users WHERE email LIKE '%@guest.local'").fetchone()[0]
    assert c.patch(f"/api/v1/users/{gid}", headers=H, json={"disabled": False}).status_code == 404
    # over the public link: no guest chat
    c.post("/api/v1/logout", headers=H); c.cookies.clear()
    import os
    os.environ["CL_PUBLIC_HOSTS"] = "testserver,abc.trycloudflare.com"
    try:
        assert c.get("/api/v1/chat/me", headers={"host": "abc.trycloudflare.com"}).status_code == 404
    finally:
        os.environ["CL_PUBLIC_HOSTS"] = "testserver"
    # turned off: guests are asked to sign in
    c.post("/api/v1/login", headers=H, json={"email": "o@example.com", "password": "pass-word-123"})
    c.put("/api/v1/settings/chat", headers=H, json={"guests": False})
    c.post("/api/v1/logout", headers=H); c.cookies.clear()
    assert c.get("/api/v1/chat/me").status_code == 401 and c.get("/api/v1/setup").json()["guest_chat"] is False
