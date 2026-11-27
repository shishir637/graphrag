"""Chat inside Context Layer, answered by Claude with each person's own Claude API key.

People sign in to Context Layer as usual and add their own Claude API key once (My Claude key). Their chats then
run on that key, billed to their own Claude Console account. An owner can also set a company key that people
without a key may use. Claude.ai / Pro / Max sign-in is not used: Anthropic does not allow third-party apps to
offer it.

How a turn works: Claude gets the person's message and the Context Layer tools (the same tools agents get over
MCP, with the person's own access groups) and calls them in a loop until it can answer with cited passages.
Files dropped into the chat are added to Context Layer (search index, graph, metric drafts) in the background,
and small PDFs / text files are also shown to Claude directly in that turn so it can use them at once.

Keys are stored encrypted (stdlib: HMAC-SHA256 counter-mode keystream + HMAC tag) with a master key that lives in
the secrets file, not in the database. Keys are never sent back to the browser.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path

from . import core, engine, jobs

KEY_ENV_COMPANY = "CL_CHAT_COMPANY_KEY"
MASTER_ENV = "CL_CHAT_MASTER_KEY"
BASE_URL = os.environ.get("CL_CHAT_BASE_URL", "https://api.anthropic.com").rstrip("/")
DEFAULT_MODEL = "claude-sonnet-5-5"
MODELS = [("claude-sonnet-5-5", "Claude Sonnet 5.5 (balanced)"), ("claude-haiku-5-5", "Claude Haiku 5.5 (fastest, cheapest)"),
          ("claude-opus-5-5", "Claude Opus 5.5 (strongest)")]
KEYS_URL = "https://platform.claude.com/settings/keys"
MAX_STEPS = 10
MAX_TOOL_CHARS = 24000
MAX_FILE_MB = 50
INLINE_PDF_MB = 10
INLINE_TEXT_KB = 300
BUILD_WAIT_S = 40
READ_TOOLS = ["ask", "search_documents", "fetch_passage", "list_documents", "describe_data", "query_metrics",
              "list_metrics", "find_connections", "explore_entity", "build_status"]
TOOL_NEEDS = {"search_documents": "documents", "list_documents": "documents", "describe_data": "data",
              "query_metrics": "data", "list_metrics": "data", "find_connections": "graph", "explore_entity": "graph"}
TOOL_LABEL = {"ask": "Looking for evidence", "search_documents": "Searching documents", "fetch_passage": "Reading a passage",
              "list_documents": "Listing documents", "describe_data": "Reading the metric catalogue",
              "query_metrics": "Computing numbers", "list_metrics": "Listing metrics", "find_connections": "Following links in the graph",
              "explore_entity": "Looking up an entity", "build_status": "Checking the upload build"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_keys (
  user_id INTEGER PRIMARY KEY REFERENCES users(id), enc TEXT NOT NULL, hint TEXT NOT NULL, model TEXT NOT NULL,
  created TEXT NOT NULL, last_used TEXT);
CREATE TABLE IF NOT EXISTS chats (
  id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), title TEXT NOT NULL, created TEXT NOT NULL,
  updated TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS chats_user ON chats(user_id, updated);
CREATE TABLE IF NOT EXISTS chat_messages (
  id INTEGER PRIMARY KEY, chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE, role TEXT NOT NULL,
  content TEXT NOT NULL, meta TEXT NOT NULL DEFAULT '{}', created TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS chat_messages_chat ON chat_messages(chat_id, id);
"""
_ready_for: str | None = None


def db():
    global _ready_for
    c = core.db()
    if _ready_for != str(core.DB_PATH):
        c.executescript(SCHEMA)
        _ready_for = str(core.DB_PATH)
    return c


class ChatError(RuntimeError):
    """Shown to the person as is."""


# ------------------------------------------------------------------ settings

def settings() -> dict:
    s = core.get_setting("chat", {}) or {}
    return {"company_key_for": s.get("company_key_for", "nobody"),       # nobody | everyone
            "uploads": s.get("uploads", "everyone"),                      # everyone | admins
            "company_model": s.get("company_model", DEFAULT_MODEL),
            "guests": bool(s.get("guests", False))}                    # chat without signing in (this computer only)


def save_settings(company_key_for: str, uploads: str, company_model: str, company_key: str | None,
                  guests: bool = False):
    if company_key_for not in ("nobody", "everyone") or uploads not in ("everyone", "admins"):
        raise ValueError("company_key_for is nobody or everyone; uploads is everyone or admins.")
    model = _clean_model(company_model)
    if company_key is not None:
        k = company_key.strip()
        if k:
            _check_key_shape(k)
        core.save_secret(KEY_ENV_COMPANY, k or None)
    if guests and not os.environ.get(KEY_ENV_COMPANY):
        raise ValueError("Add a company Claude API key first: people who chat without signing in use it.")
    core.set_setting("chat", {"company_key_for": company_key_for, "uploads": uploads, "company_model": model,
                              "guests": bool(guests)})


def public_settings() -> dict:
    k = os.environ.get(KEY_ENV_COMPANY) or ""
    return settings() | {"company_key_set": bool(k), "company_key": core.mask(k), "models": MODELS, "keys_url": KEYS_URL}


def _clean_model(m: str) -> str:
    m = (m or "").strip()[:80] or DEFAULT_MODEL
    if not re.match(r"^[A-Za-z0-9._:\-]+$", m):
        raise ValueError("That model name has characters that are not allowed.")
    return m


def _check_key_shape(k: str):
    if not k.startswith("sk-ant-") or len(k) < 30 or len(k) > 300 or re.search(r"\s", k):
        raise ValueError("That does not look like a Claude API key. Keys start with sk-ant- (Claude Console → API keys).")


# ------------------------------------------------------------------ encrypted per-person keys

def _master() -> bytes:
    m = os.environ.get(MASTER_ENV)
    if not m:
        m = secrets.token_hex(32)
        core.save_secret(MASTER_ENV, m)
    return bytes.fromhex(m)


def _subkeys() -> tuple[bytes, bytes]:
    m = _master()
    return hmac.new(m, b"cl-chat-enc", hashlib.sha256).digest(), hmac.new(m, b"cl-chat-mac", hashlib.sha256).digest()


def _stream(k: bytes, nonce: bytes, n: int) -> bytes:
    out, i = bytearray(), 0
    while len(out) < n:
        out += hmac.new(k, nonce + i.to_bytes(8, "big"), hashlib.sha256).digest()
        i += 1
    return bytes(out[:n])


def encrypt(plain: str) -> str:
    ke, km = _subkeys()
    nonce = secrets.token_bytes(16)
    data = plain.encode()
    ct = bytes(a ^ b for a, b in zip(data, _stream(ke, nonce, len(data))))
    tag = hmac.new(km, nonce + ct, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(nonce + ct + tag).decode()


def decrypt(token: str) -> str:
    ke, km = _subkeys()
    raw = base64.urlsafe_b64decode(token.encode())
    nonce, ct, tag = raw[:16], raw[16:-32], raw[-32:]
    if not hmac.compare_digest(tag, hmac.new(km, nonce + ct, hashlib.sha256).digest()):
        raise ChatError("Your saved Claude key could not be read (the server's secrets changed). Add it again.")
    return bytes(a ^ b for a, b in zip(ct, _stream(ke, nonce, len(ct)))).decode()


def my_key(user_id: int) -> dict:
    r = db().execute("SELECT hint, model, created, last_used FROM chat_keys WHERE user_id = ?", (user_id,)).fetchone()
    s = public_settings()
    company = s["company_key_set"] and s["company_key_for"] == "everyone"
    return {"key_set": r is not None, "key": r["hint"] if r else None, "model": r["model"] if r else DEFAULT_MODEL,
            "created": r["created"] if r else None, "last_used": r["last_used"] if r else None,
            "company_key_available": company, "company_model": s["company_model"],
            "can_chat": r is not None or company, "models": MODELS, "keys_url": KEYS_URL}


def set_my_key(user_id: int, key: str | None, model: str, verify=None):
    """key None: keep the saved key and only change the model. key "": remove it."""
    model = _clean_model(model)
    c = db()
    if key is None:
        if c.execute("UPDATE chat_keys SET model = ? WHERE user_id = ?", (model, user_id)).rowcount == 0:
            raise ValueError("Add your Claude API key first.")
        return
    key = key.strip()
    if not key:
        c.execute("DELETE FROM chat_keys WHERE user_id = ?", (user_id,))
        return
    _check_key_shape(key)
    (verify or verify_key)(key)
    c.execute("INSERT INTO chat_keys (user_id, enc, hint, model, created) VALUES (?, ?, ?, ?, ?) "
              "ON CONFLICT(user_id) DO UPDATE SET enc = excluded.enc, hint = excluded.hint, model = excluded.model, "
              "created = excluded.created, last_used = NULL",
              (user_id, encrypt(key), core.mask(key), model, core.now()))


def verify_key(key: str):
    """A free call (list models) to check the key before saving it."""
    import urllib.error
    import urllib.request
    req = urllib.request.Request(BASE_URL + "/v1/models?limit=1",
                                 headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise ValueError("Claude refused this key. Check that you copied the whole key and that it is active.")
        raise ValueError(f"Could not check the key: Claude answered {e.code}. Try again in a minute.")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ValueError(f"Could not reach Claude to check the key ({getattr(e, 'reason', e)}). "
                         "This server needs internet access to api.anthropic.com.")


def credentials(user_id: int, guest: bool = False) -> tuple[str, str, str]:
    """(api key, model, whose key) for a chat turn."""
    if guest:
        if not guests_on():
            raise ChatError("Chat without signing in is turned off. Sign in to Context Layer.")
        return os.environ[KEY_ENV_COMPANY], settings()["company_model"], "company"
    r = db().execute("SELECT enc, model FROM chat_keys WHERE user_id = ?", (user_id,)).fetchone()
    if r:
        db().execute("UPDATE chat_keys SET last_used = ? WHERE user_id = ?", (core.now(), user_id))
        return decrypt(r["enc"]), r["model"], "own"
    s = settings()
    k = os.environ.get(KEY_ENV_COMPANY)
    if k and s["company_key_for"] == "everyone":
        return k, s["company_model"], "company"
    raise ChatError("Add your Claude API key first (the key icon above the chat).")


# ------------------------------------------------------------------ guests (chat without signing in)

GUEST_DOMAIN = "@guest.local"
GUEST_COOKIE = "cl_guest"


def guests_on() -> bool:
    return settings()["guests"] and bool(os.environ.get(KEY_ENV_COMPANY))


def is_guest(u: dict) -> bool:
    return u["email"].endswith(GUEST_DOMAIN)


def guest_user(token: str | None) -> tuple[dict, str | None]:
    """The guest behind a browser cookie, made on first visit: a disabled viewer account (it can never sign in)
    with the public group only, so each browser keeps its own chats. Returns (user, new cookie or None)."""
    new = None
    if not token or not re.fullmatch(r"[A-Za-z0-9_-]{20,64}", token):
        token = new = secrets.token_urlsafe(24)
    email = "guest-" + hashlib.sha256(token.encode()).hexdigest()[:20] + GUEST_DOMAIN
    r = core.db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if r is None:
        with core.tx() as c:
            c.execute("INSERT OR IGNORE INTO users (email, name, pw_hash, role, created, disabled, groups) "
                      "VALUES (?, ?, ?, 'viewer', ?, 1, '[\"public\"]')",
                      (email, "Guest", "disabled$" + secrets.token_hex(16), core.now()))
        r = core.db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    return core.public_user(r), new


# ------------------------------------------------------------------ who is chatting

def principal(u: dict) -> dict:
    """The signed-in person, with the same rights they get when they connect from Claude or ChatGPT."""
    admin = u["role"] in ("owner", "admin") and not is_guest(u)
    groups = sorted(set(engine.all_groups()) | set(u["groups"])) if admin else (["public"] if is_guest(u) else u["groups"])
    up = admin or settings()["uploads"] == "everyone"
    if not is_guest(u):
        try:
            from .access import for_agent
            g, caps = for_agent(u["email"], u["role"])
            groups = sorted(set(groups) | set(g)) if admin else g
            up = admin or (up and "upload" in caps)
        except Exception:                                  # noqa: BLE001
            pass
    return {"id": f"user:{u['id']}", "kind": "user", "user_id": u["id"], "email": u["email"], "role": u["role"],
            "name": f"guest {u['email'][6:12]} via chat" if is_guest(u) else f"{u['name']} via chat", "client": "Context Layer chat", "groups": groups, "all_groups": admin,
            "capabilities": list(core.READ_CAPABILITIES) + (["upload"] if up else [])}


# ------------------------------------------------------------------ chats

def list_chats(user_id: int) -> list[dict]:
    return [dict(r) for r in db().execute("SELECT id, title, created, updated FROM chats WHERE user_id = ? "
                                          "ORDER BY updated DESC LIMIT 200", (user_id,))]


def _own(chat_id: str, user_id: int):
    r = db().execute("SELECT * FROM chats WHERE id = ? AND user_id = ?", (chat_id, user_id)).fetchone()
    if r is None:
        raise KeyError(chat_id)
    return r


def new_chat(user_id: int, title: str = "New chat") -> dict:
    cid = secrets.token_hex(8)
    db().execute("INSERT INTO chats VALUES (?, ?, ?, ?, ?)", (cid, user_id, title[:80], core.now(), core.now()))
    return {"id": cid, "title": title[:80], "created": core.now(), "updated": core.now()}


def rename_chat(chat_id: str, user_id: int, title: str):
    _own(chat_id, user_id)
    db().execute("UPDATE chats SET title = ? WHERE id = ?", (" ".join(title.split())[:80] or "Chat", chat_id))


def delete_chat(chat_id: str, user_id: int):
    _own(chat_id, user_id)
    with core.tx() as c:
        c.execute("DELETE FROM chat_messages WHERE chat_id = ?", (chat_id,))
        c.execute("DELETE FROM chats WHERE id = ?", (chat_id,))


def _rows(chat_id: str) -> list:
    return db().execute("SELECT * FROM chat_messages WHERE chat_id = ? ORDER BY id", (chat_id,)).fetchall()


def _add(chat_id: str, role: str, content: list, meta: dict | None = None) -> int:
    cur = db().execute("INSERT INTO chat_messages (chat_id, role, content, meta, created) VALUES (?, ?, ?, ?, ?)",
                       (chat_id, role, json.dumps(content, ensure_ascii=False), json.dumps(meta or {}, ensure_ascii=False),
                        core.now()))
    db().execute("UPDATE chats SET updated = ? WHERE id = ?", (core.now(), chat_id))
    return cur.lastrowid


def history(chat_id: str, user_id: int) -> dict:
    """What the page shows: the person's messages (with their files) and Claude's answers (with sources and steps)."""
    ch = _own(chat_id, user_id)
    out = []
    for r in _rows(chat_id):
        meta = json.loads(r["meta"])
        if meta.get("hidden"):
            continue
        content = json.loads(r["content"])
        text = "\n".join(b.get("text", "") for b in content if b.get("type") == "text" and not b.get("_note")).strip()
        if r["role"] == "user" and not text and not meta.get("uploads"):
            continue                                       # tool results
        if r["role"] == "assistant" and not meta.get("final"):
            continue                                       # tool calls (shown as steps on the final answer)
        out.append({"id": r["id"], "role": r["role"], "text": text, "created": r["created"],
                    **{k: meta[k] for k in ("uploads", "sources", "steps", "model", "key", "usage", "error") if k in meta}})
    return {"chat": dict(ch), "messages": out}


# ------------------------------------------------------------------ files dropped into the chat

def _files_dir(user_id: int) -> Path:
    d = core.DATA_DIR / "chat_files" / str(user_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def add_files(u: dict, chat_id: str, files: list[tuple[str, bytes]], groups: list[str] | None = None) -> dict:
    """Save the files to Context Layer (documents or data tables) and queue one rebuild. Returns refs the
    next message can point to."""
    from . import mcp_server as M
    _own(chat_id, u["id"])
    p = principal(u)
    if "upload" not in p["capabilities"]:
        raise ChatError("Your Context Layer admin has not allowed adding files from the chat.")
    try:
        g = M._upload_groups(p, groups)
    except Exception as e:                                   # ToolError
        raise ChatError(str(e)) from e
    saved, errors, refs = [], [], []
    for name, data in files:
        if len(data) > MAX_FILE_MB * 2 ** 20:
            errors.append(f"{name}: larger than {MAX_FILE_MB} MB")
            continue
        try:
            s = engine.ingest_upload(name, data, g)
        except ValueError as e:
            errors.append(str(e))
            continue
        ref = secrets.token_hex(8)
        safe = engine.safe_filename(name)
        (_files_dir(u["id"]) / f"{ref}__{safe}").write_bytes(data)
        saved.append(s)
        refs.append({"ref": ref, "file": s["file"], "kind": s["kind"], "bytes": len(data), "groups": s.get("groups", g)})
    if not saved:
        raise ChatError("; ".join(errors) or "No files.")
    job = engine.queue_rebuild(saved, p["name"])
    for r in refs:
        r["job_id"] = job["id"]
    core.log_event("user", u["email"], "chat.files", ", ".join(s["file"] for s in saved), {"job": job["id"], "chat": chat_id})
    return {"files": refs, "job_id": job["id"], "errors": errors}


def _file_blocks(user_id: int, uploads: list[dict]) -> tuple[list[dict], list[dict]]:
    """Content blocks for this turn (files shown to Claude directly) and the uploads as stored."""
    blocks, kept = [], []
    d = _files_dir(user_id)
    for up in uploads[:10]:
        ref = str(up.get("ref", ""))
        if not re.fullmatch(r"[0-9a-f]{16}", ref):
            continue
        hits = list(d.glob(f"{ref}__*"))
        if not hits:
            continue
        f = hits[0]
        name = f.name.split("__", 1)[1]
        info = {"ref": ref, "file": up.get("file") or name, "kind": up.get("kind", "document"),
                "job_id": up.get("job_id"), "bytes": f.stat().st_size}
        ext = Path(name).suffix.lower()
        shown = False
        if ext == ".pdf" and f.stat().st_size <= INLINE_PDF_MB * 2 ** 20:
            blocks.append({"type": "document", "title": name,
                           "source": {"type": "base64", "media_type": "application/pdf",
                                      "data": base64.b64encode(f.read_bytes()).decode()}})
            shown = True
        elif ext in engine.TEXT_EXTS and f.stat().st_size <= INLINE_TEXT_KB * 1024:
            blocks.append({"type": "document", "title": name,
                           "source": {"type": "text", "media_type": "text/plain",
                                      "data": f.read_bytes().decode("utf-8", "replace")}})
            shown = True
        info["shown"] = shown
        kept.append(info)
    return blocks, kept


def _upload_note(kept: list[dict], shown_now: bool) -> str:
    lines = []
    for k in kept:
        how = ("shown to you above in this turn" if k["shown"] and shown_now else
               "shown in an earlier turn" if k["shown"] else "not shown directly (use the tools)")
        lines.append(f"- {k['file']} ({k['kind']}, build job {k['job_id']}; {how})")
    return ("[Files the person attached in this message. They are being added to Context Layer in the background; "
            "once build_status says the job is done you can search them (search_documents with files=[name]) and they "
            "are kept for later.]\n" + "\n".join(lines))


# ------------------------------------------------------------------ the Claude loop

SYSTEM = """You are the chat assistant inside Context Layer, {org}'s governed company knowledge.
You are talking to {name} ({role}). Today is {today}.

Answer from evidence, not memory:
- For anything about the organisation (its documents, people, policies, products, numbers), use the tools first.
  `ask` is the best start; then `search_documents`, `fetch_passage`, the metric tools or the graph tools as needed.
- Cite every fact from a passage with its id in square brackets right after the sentence, e.g. [a1b2c3d4e5f6].
  Use only ids the tools returned. Numbers from query_metrics: say which metric and period they are.
- If the evidence does not contain the answer, say so plainly and say what you searched. Do not guess.
- The person only sees what their access allows; if a tool refuses something, tell them.
- General questions that are not about the organisation can be answered normally, without tools.
- Files the person attached are listed in their message. If a file was shown to you directly, you can use it at
  once (cite it by file name). Otherwise call build_status (it waits up to {wait} s) and search it once it is ready.
- Keep answers short and plain. Use markdown lists or tables only when they help."""


def _tools(p: dict) -> list[dict]:
    from . import mcp_server as M
    by = {t.name: t for t in M.mcp._tool_manager.list_tools()}
    out = []
    for name in READ_TOOLS:
        need = TOOL_NEEDS.get(name)
        if need and need not in p["capabilities"]:
            continue
        t = by[name]
        schema = {k: v for k, v in t.parameters.items() if k != "title"}
        desc = t.description
        if name == "build_status":
            desc += f" Waits up to {BUILD_WAIT_S} seconds while the build is still running."
        out.append({"name": name, "description": desc, "input_schema": schema})
    if out:
        out[-1]["cache_control"] = {"type": "ephemeral"}
    return out


def _run_tool(p: dict, name: str, args: dict) -> tuple[dict, bool]:
    """(result, ok). Uses the same tool code and logging as MCP agents."""
    from . import mcp_server as M
    fn = M.TOOLS.get(name)
    if fn is None or name not in READ_TOOLS:
        return {"error": f"Unknown tool {name}."}, False
    if not isinstance(args, dict):
        args = {}
    if name == "build_status":
        def call():
            end = time.time() + BUILD_WAIT_S
            while True:
                r = fn(p, **args)
                if r.get("status") not in ("running", "queued") or time.time() > end:
                    return r
                time.sleep(3)
    else:
        def call():
            return fn(p, **args)
    try:
        return M.run(p, name, args, call, wait=name != "build_status"), True
    except Exception as e:                                       # ToolError, TypeError on bad args
        msg = str(e) or e.__class__.__name__
        if isinstance(e, TypeError):
            msg = f"Bad arguments for {name}: {msg}"
        return {"error": msg}, False


def _sources(name: str, result: dict) -> list[dict]:
    ps = []
    if name in ("ask", "search_documents"):
        ps = result.get("passages") or []
    elif name == "fetch_passage" and result.get("found"):
        ps = [result]
    return [{"id": x["id"], "source": x.get("source", ""), "section": x.get("section", ""), "pages": x.get("pages") or [],
             "text": (x.get("text") or "")[:600]} for x in ps if x.get("id")]


def _summary(name: str, result: dict, ok: bool) -> str:
    if not ok:
        return result.get("error", "failed")[:200]
    if "passages" in result:
        return f"{len(result['passages'])} passages" + (f" ({result['route']})" if result.get("route") else "")
    if "row_count" in result:
        return f"{result['row_count']} rows"
    if name == "build_status":
        return str(result.get("status"))
    if "relations" in result:
        return f"{len(result.get('relations') or [])} links"
    return "done"


def _clip(result: dict) -> str:
    s = json.dumps(result, ensure_ascii=False, default=str)
    return s if len(s) <= MAX_TOOL_CHARS else s[:MAX_TOOL_CHARS] + ' ... [cut: ask for less, or fetch_passage]'


KEEP_RECENT_ROWS = 16


def _api_messages(chat_id: str) -> list[dict]:
    msgs = []
    rows = _rows(chat_id)
    for i, r in enumerate(rows):
        if json.loads(r["meta"]).get("error_turn"):
            continue
        content = [{k: v for k, v in b.items() if k != "_note"} for b in json.loads(r["content"])]
        if i < len(rows) - KEEP_RECENT_ROWS:                  # old tool results: keep the call, drop the bulk
            content = [b | {"content": "[earlier result removed to save space; call the tool again if needed]"}
                       if b.get("type") == "tool_result" else b for b in content]
        if msgs and msgs[-1]["role"] == r["role"]:
            msgs[-1]["content"] += content                     # keep roles alternating
        else:
            msgs.append({"role": r["role"], "content": content})
    return msgs


def call_claude(key: str, body: dict) -> dict:
    from graph.extract_ai import AIError, _post
    try:
        return _post(BASE_URL + "/v1/messages", body, {"x-api-key": key, "anthropic-version": "2023-06-01"})
    except AIError as e:
        m = str(e)
        if m.startswith(("401", "403")):
            raise ChatError("Claude refused the API key. Update it under My Claude key.") from e
        if "credit balance" in m.lower():
            raise ChatError("The Claude account behind this key is out of credit (Claude Console → Billing).") from e
        if m.startswith("404") and "model" in m.lower():
            raise ChatError(f"Claude does not know the model '{body['model']}'. Pick another one under My Claude key.") from e
        if m.startswith("429") or m.startswith("529"):
            raise ChatError("Claude is busy or this key hit its rate limit. Try again in a minute.") from e
        raise ChatError(f"Claude could not answer: {m[:300]}") from e


_busy: set[int] = set()


def send(u: dict, chat_id: str, text: str, uploads: list[dict] | None = None, claude=None):
    """Run one turn. Yields progress events (dicts); the last one is the answer or an error."""
    if u["id"] in _busy:
        raise ChatError("Wait for the current answer to finish.")
    _busy.add(u["id"])
    try:
        yield from _send(u, chat_id, text, uploads, claude or call_claude)
    finally:
        _busy.discard(u["id"])


def _send(u: dict, chat_id: str, text: str, uploads: list[dict] | None, claude):
    ch = _own(chat_id, u["id"])
    text = (text or "").strip()
    if not text and not uploads:
        raise ChatError("Write a message or attach a file.")
    if len(text) > 50000:
        raise ChatError("That message is too long (50,000 characters at most).")
    key, model, whose = credentials(u["id"], is_guest(u))
    p = principal(u)

    blocks, kept = _file_blocks(u["id"], uploads or [])
    user_content = blocks + ([{"type": "text", "text": _upload_note(kept, True), "_note": True}] if kept else []) \
        + ([{"type": "text", "text": text}] if text else [])
    if not text:
        user_content.append({"type": "text", "text": "(The person attached files without a message.)", "_note": True})
    stored = ([{"type": "text", "text": _upload_note(kept, False), "_note": True}] if kept else []) \
        + ([{"type": "text", "text": text}] if text else [{"type": "text", "text": "(attached files)", "_note": True}])
    first = not _rows(chat_id)
    user_row = _add(chat_id, "user", stored, {"uploads": kept} if kept else {})
    if first and ch["title"] == "New chat":
        rename_chat(chat_id, u["id"], (text or (kept[0]["file"] if kept else "Chat"))[:60])

    messages = _api_messages(chat_id)
    messages[-1]["content"] = [b for b in messages[-1]["content"][:-len(stored)]] + \
        [{k: v for k, v in b.items() if k != "_note"} for b in user_content]

    system = [{"type": "text", "text": SYSTEM.format(org=core.get_setting("organisation", "") or "the organisation",
                                                     name=u["name"], role=u["role"], today=core.now()[:10], wait=BUILD_WAIT_S),
               "cache_control": {"type": "ephemeral"}}]
    tools = _tools(p)
    steps, sources, usage = [], {}, {"input_tokens": 0, "output_tokens": 0}
    t = core.timer()
    yield {"type": "status", "text": "Thinking"}
    try:
        for step in range(MAX_STEPS + 1):
            body = {"model": model, "max_tokens": 4096, "system": system, "messages": messages}
            if tools and step < MAX_STEPS:
                body["tools"] = tools
            r = claude(key, body)
            for k in usage:
                usage[k] += (r.get("usage") or {}).get(k, 0)
            content = [b for b in r.get("content") or [] if b.get("type") in ("text", "tool_use", "thinking", "redacted_thinking")]
            calls = [b for b in content if b.get("type") == "tool_use"]
            if not calls or r.get("stop_reason") != "tool_use":
                answer = "".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
                if r.get("stop_reason") == "max_tokens":
                    answer += "\n\n(The answer was cut off at the length limit.)"
                cited = set(re.findall(r"\[([0-9a-f]{12})\]", answer))
                src = [s for s in sources.values() if s["id"] in cited]
                meta = {"final": True, "sources": src, "steps": steps, "model": model, "key": whose, "usage": usage}
                mid = _add(chat_id, "assistant", content or [{"type": "text", "text": answer}], meta)
                core.log_event("user", u["email"], "chat.answer", text[:200],
                               {"chat": chat_id, "model": model, "key": whose, "steps": len(steps), **usage}, True, t())
                yield {"type": "answer", "message": {"id": mid, "role": "assistant", "text": answer, "created": core.now(),
                                                     **{k: meta[k] for k in ("sources", "steps", "model", "key", "usage")}}}
                return
            _add(chat_id, "assistant", content, {"hidden": True})
            messages.append({"role": "assistant", "content": content})
            results = []
            for b in calls:
                yield {"type": "status", "text": TOOL_LABEL.get(b["name"], b["name"])}
                res, ok = _run_tool(p, b["name"], b.get("input") or {})
                for s in _sources(b["name"], res):
                    sources[s["id"]] = s
                steps.append({"tool": b["name"], "label": TOOL_LABEL.get(b["name"], b["name"]),
                              "input": b.get("input") or {}, "ok": ok, "summary": _summary(b["name"], res, ok)})
                yield {"type": "step", "step": steps[-1]}
                results.append({"type": "tool_result", "tool_use_id": b["id"], "content": _clip(res),
                                **({} if ok else {"is_error": True})})
            _add(chat_id, "user", results, {"hidden": True})
            messages.append({"role": "user", "content": results})
        raise ChatError("Claude used too many steps without answering. Ask a narrower question.")
    except ChatError as e:
        _undo_turn(chat_id, user_row, str(e))
        core.log_event("user", u["email"], "chat.answer", text[:200], {"chat": chat_id, "error": str(e)}, False, t())
        yield {"type": "error", "text": str(e)}


def _undo_turn(chat_id: str, user_row: int, error: str = ""):
    """A failed turn: keep the person's message (shown with the error) but drop half-finished tool calls, so the
    next turn starts from a clean history."""
    db().execute("DELETE FROM chat_messages WHERE chat_id = ? AND id > ?", (chat_id, user_row))
    db().execute("UPDATE chat_messages SET meta = json_set(meta, '$.error_turn', 1, '$.error', ?) WHERE id = ?",
                 (error, user_row))
