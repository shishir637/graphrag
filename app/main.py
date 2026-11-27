"""Context Layer server: the web app (/), its API (/api/v1) and the MCP endpoint for agents (/mcp).

Run:  python -m app            (or: uvicorn app.main:app)
"""
import contextlib
import csv
import io
import json
import re
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import chat, core, engine, jobs, oauth
from . import mcp_server as M

core.load_secrets()
WEB = core.ROOT / "web" / "dist"
COOKIE = "cl_session"
MAX_UPLOAD_MB = 200
_mcp_asgi = None


@contextlib.asynccontextmanager
async def lifespan(app):
    # a fresh MCP app (and session manager) per server start: a manager can only be run once
    global _mcp_asgi
    _mcp_asgi = M.http_app()
    try:
        M.describe_org()
    except Exception as e:                           # noqa: BLE001 - generic wording stays
        print(f"Could not describe the organisation for Claude: {e}", flush=True)
    _warm_up()
    _landing_backfill()
    import os
    from . import jobqueue, scheduler
    role = os.environ.get("CL_ROLE", "all")         # api: answers only; the worker container runs jobs and the schedule
    if role in ("all", "worker"):
        scheduler.start()
        if jobs.db_mode():
            jobqueue.start_workers()
    try:
        async with M.mcp.session_manager.run():
            yield
    finally:
        scheduler.stop()
        jobqueue.stop_workers()


def _landing_backfill():
    """CL_LANDING_BACKFILL=1 (Docker): tables kept as CSV/Excel by earlier versions become Parquet, per workspace."""
    import os
    import threading
    if os.environ.get("CL_LANDING_BACKFILL") != "1":
        return

    def run():
        from . import db as D
        from . import landing
        for t in ([x["id"] for x in core.list_tenants()] if D.postgres() else [None]):
            try:
                with core.tenant_scope(t):
                    out = landing.backfill()
                if out["converted"]:
                    print(f"Tables moved to Parquet in {t or 'the first workspace'}: {out['converted']}", flush=True)
            except Exception as e:                   # noqa: BLE001 - the CSV keeps working through its view
                print(f"Parquet backfill skipped for {t}: {e}", flush=True)

    threading.Thread(target=run, daemon=True, name="landing-backfill").start()


def _warm_up():
    """Load the search models in the background at start, so the first question is not slow enough to time out
    behind a tunnel. Off with CL_WARMUP=0 (tests) or when no documents are prepared yet."""
    import os
    import threading
    if os.environ.get("CL_WARMUP", "1") == "0" or not (engine.OUT_DIR / "4_index.json").exists():
        return

    def load():
        try:
            from rag import store
            if store.server() and (engine.OUT_DIR / "3_embeddings.json").exists():
                from qdrant_client import models as qm
                flt = qm.Filter(must=[qm.FieldCondition(key="tenant", match=qm.MatchValue(value=store.tenant()))])
                qc = store.client()
                if not qc.collection_exists(store.COLLECTION) or qc.count(store.COLLECTION, count_filter=flt).count == 0:
                    print("Moving the search index to the Qdrant server (from the prepared passages)...", flush=True)
                    store.build_index()            # an install upgraded from the embedded index: no re-embedding
        except Exception as e:                       # noqa: BLE001 - "Prepare files" does the same
            print(f"Search index move skipped: {e}", flush=True)
        try:
            from rag.models import embed_dense, embed_sparse, rerank_scores
            from rag.store import active_collection
            active_collection()
            embed_dense(["warm up"])
            embed_sparse(["warm up"], query=True)
            rerank_scores("warm up", ["warm up"])
            print("Search models loaded.", flush=True)
        except Exception as e:                       # noqa: BLE001 - only a speed-up; the first question loads them
            print(f"Warm-up skipped: {e}", flush=True)

    threading.Thread(target=load, daemon=True, name="warm-up").start()


app = FastAPI(title="Context Layer", version=core.VERSION, docs_url=None, redoc_url=None, lifespan=lifespan)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    # API calls that change something need this header: browsers can't add it cross-site (CSRF guard)
    if request.url.path.startswith("/api/") and request.method not in ("GET", "HEAD", "OPTIONS") \
            and request.headers.get("x-cl") != "1":
        return JSONResponse({"detail": "Missing X-CL header."}, status_code=403)
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    return resp


app.include_router(oauth.router)


# ------------------------------------------------------------------ auth helpers

def current_user(request: Request) -> dict:
    u = core.session_user(request.cookies.get(COOKIE))
    if u is None:
        raise HTTPException(401, "Sign in first.")
    return u


def role(*allowed):
    def dep(u: dict = Depends(current_user)) -> dict:
        if u["role"] not in allowed:
            raise HTTPException(403, "Your role does not allow this.")
        return u
    return dep


ANY = role("owner", "admin", "publisher", "viewer")
ADMIN = role("owner", "admin")
OWNER = role("owner")


def audit(u: dict, action: str, target: str = "", detail: dict | None = None):
    core.log_event("user", u["email"], action, target, detail)


_attempts: dict[str, deque] = defaultdict(deque)


def _rate_limit(key: str, limit: int = 8, window: int = 300):
    q, t = _attempts[key], time.time()
    while q and q[0] < t - window:
        q.popleft()
    if len(q) >= limit:
        raise HTTPException(429, "Too many attempts. Wait a few minutes and try again.")
    q.append(t)


def _set_session(resp: Response, token: str, request: Request):
    resp.set_cookie(COOKIE, token, max_age=core.SESSION_DAYS * 86400, httponly=True, samesite="lax",
                    secure=request.url.scheme == "https", path="/")


# ------------------------------------------------------------------ auth routes

class SetupIn(BaseModel):
    name: str
    email: str
    password: str
    organisation: str = ""


class LoginIn(BaseModel):
    email: str
    password: str


@app.get("/api/v1/setup")
def setup_state():
    return {"needs_setup": core.user_count() == 0, "version": core.VERSION,
            "organisation": core.get_setting("organisation", ""), "guest_chat": chat.guests_on()}


@app.post("/api/v1/setup")
def setup(body: SetupIn, request: Request, response: Response):
    if core.user_count() > 0:
        raise HTTPException(409, "Context Layer is already set up. Sign in instead.")
    try:
        u = core.create_user(body.email, body.name, body.password, "owner")
    except ValueError as e:
        raise HTTPException(400, str(e))
    core.set_setting("organisation", body.organisation.strip()[:80])
    _, token = core.login(body.email, body.password)
    _set_session(response, token, request)
    audit(u, "setup", body.organisation)
    return {"user": u}


@app.get("/api/v1/signup")
def signup_state():
    import os
    return {"open": os.environ.get("CL_SIGNUP", "off") == "open"}


@app.post("/api/v1/signup")
def signup(body: SetupIn, request: Request, response: Response):
    """A new company: its own workspace (separate people, keys, files, definitions and activity; row-level security
    in Postgres) with the person signing up as its owner (company admin). On only with CL_SIGNUP=open."""
    import os
    from . import db as D
    if os.environ.get("CL_SIGNUP", "off") != "open":
        raise HTTPException(403, "Sign-up is closed on this server. Ask its owner for an invite.")
    if not D.postgres():
        raise HTTPException(409, "Separate workspaces need the Postgres database (CL_DATABASE_URL).")
    _rate_limit(f"signup:{request.client.host if request.client else '?'}", limit=5, window=3600)
    with core.system():
        taken = core.db().execute("SELECT 1 FROM users WHERE email = ?", (body.email.strip().lower(),)).fetchone()
    if taken:
        raise HTTPException(400, "A user with this email already exists. Sign in instead.")
    try:
        core.validate_password(body.password)
        tid = core.create_tenant(body.organisation or body.name)
        with core.tenant_scope(tid):
            u = core.create_user(body.email, body.name, body.password, "owner")
            _, token = core.login(body.email, body.password)
            audit(u, "signup", body.organisation)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _set_session(response, token, request)
    try:
        M.describe_org()                     # with two workspaces the MCP description names no company
    except Exception:                        # noqa: BLE001
        pass
    return {"user": u, "workspace": tid}


@app.post("/api/v1/login")
def login(body: LoginIn, request: Request, response: Response):
    _rate_limit(f"login:{request.client.host if request.client else '?'}")
    res = core.login(body.email, body.password)
    if res is None:
        core.log_event("user", body.email.strip().lower()[:200], "login", "", ok=False)
        raise HTTPException(401, "Email or password is not correct.")
    u, token = res
    _set_session(response, token, request)
    with core.tenant_scope(u.get("tenant_id")):
        audit(u, "login")
    return {"user": u}


@app.post("/api/v1/logout")
def logout(request: Request, response: Response):
    core.logout(request.cookies.get(COOKIE))
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@app.post("/api/v1/logout-everywhere")
def logout_everywhere(response: Response, apps: bool = False, u: dict = Depends(ANY)):
    """Sign out of every browser (and with apps=true, disconnect Claude/ChatGPT too)."""
    r = core.logout_everywhere(u["id"], apps)
    response.delete_cookie(COOKIE, path="/")
    audit(u, "logout.everywhere", "", r)
    return r


@app.get("/api/v1/me")
def me(u: dict = Depends(current_user)):
    return {"user": u, "organisation": core.get_setting("organisation", ""), "version": core.VERSION}


class PasswordIn(BaseModel):
    current: str
    new: str


@app.post("/api/v1/me/password")
def change_password(body: PasswordIn, u: dict = Depends(current_user)):
    try:
        core.change_password(u["id"], body.current, body.new)
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit(u, "password.change")
    return {"ok": True}


# ------------------------------------------------------------------ overview

@app.get("/api/v1/overview")
def overview(u: dict = Depends(ANY)):
    st = engine.status()
    agents = core.list_agents()
    active = [a for a in agents if not a["revoked"]]
    checklist = [
        {"id": "documents", "label": "Add and prepare documents", "done": st["documents"]["ready"] > 0, "to": "/sources/documents"},
        {"id": "data", "label": "Add data tables for metrics", "done": st["data"]["ready"], "to": "/sources/data", "optional": True},
        {"id": "graph", "label": "Build the knowledge graph", "done": st["graph"]["ready"], "to": "/knowledge/graph", "optional": True},
        {"id": "agent", "label": "Connect your first agent", "done": bool(active), "to": "/agents"},
    ]
    problems = [e for e in core.list_events(limit=200) if not e["ok"]][:6]
    return {"status": st, "checklist": checklist, "usage": core.usage_by_day(14),
            "agents": {"active": len(active), "total": len(agents)},
            "top_agents": sorted(active, key=lambda a: -a["calls"])[:5],
            "problems": problems, "job": jobs.running() and jobs.public(jobs.running())}


# ------------------------------------------------------------------ documents

@app.get("/api/v1/documents")
def documents(u: dict = Depends(ANY)):
    return {"documents": engine.documents(), "groups": engine.all_groups()}


@app.post("/api/v1/documents")
async def upload_documents(files: list[UploadFile] = File(...), groups: str = Form("public"), u: dict = Depends(ADMIN)):
    saved, errors = [], []
    gl = [g for g in groups.split(",")]
    for f in files:
        data = await f.read()
        if len(data) > MAX_UPLOAD_MB * 2 ** 20:
            errors.append(f"{f.filename}: larger than {MAX_UPLOAD_MB} MB")
            continue
        try:
            saved.append(engine.save_document(f.filename or "file", data, gl))
        except ValueError as e:
            errors.append(str(e))
    if saved:
        audit(u, "documents.upload", ", ".join(s["name"] for s in saved), {"groups": saved[0]["groups"]})
    if not saved and errors:
        raise HTTPException(400, "; ".join(errors))
    return {"saved": saved, "errors": errors}


class GroupsIn(BaseModel):
    groups: list[str]


@app.patch("/api/v1/documents/{name}")
def set_doc_groups(name: str, body: GroupsIn, u: dict = Depends(ADMIN)):
    try:
        g = engine.set_document_groups(name, body.groups)
    except KeyError:
        raise HTTPException(404, "No such document.")
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit(u, "documents.groups", name, {"groups": g})
    return {"name": name, "groups": g, "note": "Prepare documents again for the change to reach agents."}


@app.delete("/api/v1/documents/{name}")
def delete_doc(name: str, u: dict = Depends(ADMIN)):
    try:
        engine.delete_document(name)
    except KeyError:
        raise HTTPException(404, "No such document.")
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit(u, "documents.delete", name)
    return {"ok": True}


# ------------------------------------------------------------------ data and metrics

@app.get("/api/v1/tables")
def tables(u: dict = Depends(ANY)):
    return {"tables": engine.tables()}


@app.post("/api/v1/tables")
async def upload_tables(files: list[UploadFile] = File(...), u: dict = Depends(ADMIN)):
    saved, errors = [], []
    for f in files:
        try:
            saved += engine.save_table(f.filename or "table", await f.read())
        except ValueError as e:
            errors.append(str(e))
    if saved:
        audit(u, "tables.upload", ", ".join(saved))
    if not saved and errors:
        raise HTTPException(400, "; ".join(errors))
    return {"saved": saved, "errors": errors}


@app.get("/api/v1/metrics")
def metrics(u: dict = Depends(ANY)):
    return engine.metrics()


class MetricStatusIn(BaseModel):
    status: str


@app.put("/api/v1/metrics/{name}/status")
def set_metric_status(name: str, body: MetricStatusIn, u: dict = Depends(ADMIN)):
    from semantic.auto_model import set_status
    try:
        r = set_status(name, body.status, by=u["email"])
    except KeyError:
        raise HTTPException(404, "No such metric.")
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit(u, "metrics.status", name, {"status": body.status})
    return r


@app.get("/api/v1/graph")
def graph(q: str = "", u: dict = Depends(ANY)):
    """The graph explorer: company admins see everything; anyone else only what their data sets' passages support."""
    groups = None
    if u["role"] not in ("owner", "admin"):
        from . import access
        groups, _ = access.for_agent(u["email"], u["role"])
        groups = sorted(set(groups or ["public"]) | {f"layer:user:{u['id']}"})
    return {"overview": engine.graph_overview(), **engine.graph_explore(q.strip()[:100], groups=groups)}


# ------------------------------------------------------------------ jobs

@app.post("/api/v1/jobs/{kind}")
def start_job(kind: str, u: dict = Depends(ADMIN)):
    if kind not in engine.JOBS:
        raise HTTPException(404, "Unknown job.")
    label, fn, _ = engine.JOBS[kind]
    try:
        return {"job": jobs.start(kind, label, fn, u["email"])}
    except RuntimeError as e:
        raise HTTPException(409, str(e))


@app.get("/api/v1/jobs")
def list_jobs(u: dict = Depends(ANY)):
    return {"jobs": jobs.list_jobs(), "running": jobs.running() and jobs.public(jobs.running())}


@app.get("/api/v1/jobs/{job_id}")
def get_job(job_id: str, u: dict = Depends(ANY)):
    j = jobs.get(job_id)
    if j is None:
        raise HTTPException(404, "No such job.")
    return {"job": j}


# ------------------------------------------------------------------ agents

class AgentIn(BaseModel):
    name: str
    description: str = ""
    groups: list[str] = Field(default_factory=lambda: ["public"])
    capabilities: list[str] = Field(default_factory=lambda: ["documents"])
    scope: str = "agent"                         # agent | delegate (an app acting for its own end users)
    expires_days: int | None = None
    allowed_ips: list[str] = Field(default_factory=list)


class AgentPatch(BaseModel):
    groups: list[str] | None = None
    capabilities: list[str] | None = None
    allowed_ips: list[str] | None = None


@app.get("/api/v1/agents")
def agents(u: dict = Depends(ANY)):
    return {"agents": core.list_agents(), "groups": engine.all_groups(), "capabilities": list(core.CAPABILITIES),
            "tools": [{"name": t.name, "description": t.description} for t in M.mcp._tool_manager.list_tools()]}


@app.post("/api/v1/agents")
def create_agent(body: AgentIn, u: dict = Depends(ADMIN)):
    try:
        groups = engine.clean_groups(body.groups)
        a, token = core.create_agent(body.name, body.description, groups, body.capabilities, u["email"],
                                     body.scope, body.expires_days, body.allowed_ips)
    except ValueError as e:
        raise HTTPException(400, str(e))
    out = {"agent": a, "token": token}
    if a["scope"] == "delegate":                 # shown once: the app's backend signs each end user's identity
        from . import delegate
        out["signing_secret"] = delegate.new_signing_secret(a["id"])
    audit(u, "agents.create", a["name"], {"groups": a["groups"], "capabilities": a["capabilities"], "scope": a["scope"],
                                          "expires": a["expires"], "allowed_ips": a["allowed_ips"]})
    return out


@app.patch("/api/v1/agents/{aid}")
def update_agent(aid: str, body: AgentPatch, u: dict = Depends(ADMIN)):
    try:
        groups = engine.clean_groups(body.groups) if body.groups is not None else None
        a = core.update_agent(aid, groups, body.capabilities, body.allowed_ips)
    except KeyError:
        raise HTTPException(404, "No such agent.")
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit(u, "agents.update", a["name"], {"groups": a["groups"], "capabilities": a["capabilities"]})
    return {"agent": a}


# ---- the end users of apps with delegate keys (app/delegate.py)

class ExternalUserIn(BaseModel):
    name: str = ""
    datasets: list[str] = Field(default_factory=lambda: ["public"])
    roles: list[str] = Field(default_factory=list)
    disabled: bool = False


@app.get("/api/v1/external-users")
def external_users(u: dict = Depends(ADMIN)):
    from . import delegate
    return {"users": delegate.list_users()}


@app.put("/api/v1/external-users/{uid}")
def external_user_put(uid: str, body: ExternalUserIn, u: dict = Depends(ADMIN)):
    from . import delegate
    try:
        r = delegate.put_user(uid, body.datasets, body.roles, body.name, body.disabled)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    audit(u, "external_users.put", uid, {"datasets": r["datasets"], "roles": r["roles"], "disabled": r["disabled"]})
    return r


@app.delete("/api/v1/external-users/{uid}")
def external_user_delete(uid: str, u: dict = Depends(ADMIN)):
    from . import delegate
    if not delegate.delete_user(uid):
        raise HTTPException(404, "No such app user.")
    audit(u, "external_users.delete", uid)
    return {"ok": True}


@app.post("/api/v1/agents/{aid}/revoke")
def revoke_agent(aid: str, u: dict = Depends(ADMIN)):
    try:
        a = core.revoke_agent(aid)
    except KeyError:
        raise HTTPException(404, "No such agent.")
    audit(u, "agents.revoke", a["name"])
    return {"agent": a}


# ------------------------------------------------------------------ playground

class PlaygroundIn(BaseModel):
    question: str
    agent_id: str | None = None
    groups: list[str] | None = None
    capabilities: list[str] | None = None
    mode: str = "auto"


@app.post("/api/v1/playground")
def playground(body: PlaygroundIn, u: dict = Depends(ANY)):
    if body.agent_id:
        a = core.get_agent(body.agent_id)
        if a is None or a["revoked"]:
            raise HTTPException(400, "Pick an active agent.")
        groups, caps, who = a["groups"], a["capabilities"], f"as agent {a['name']}"
    else:
        groups = engine.clean_groups(body.groups or ["public"])
        caps = [c for c in (body.capabilities or list(core.CAPABILITIES)) if c in core.CAPABILITIES]
        who = f"as groups {', '.join(groups)}"
    mode = body.mode if body.mode in ("auto", "documents", "metrics", "graph", "overview") else None
    if mode is None:
        raise HTTPException(400, "Unknown mode.")
    t = core.timer()
    try:
        from semantic.access import SCOPE
        SCOPE.set(None if groups is None else {"groups": groups, "teams": [], "roles": []})   # row rules by data set
        out = engine.ask(body.question, groups, caps, mode)
    except (ValueError, PermissionError, RuntimeError) as e:
        core.log_event("user", u["email"], "playground.ask", body.question[:200], {"who": who, "error": str(e)}, False, t())
        raise HTTPException(400, str(e))
    core.log_event("user", u["email"], "playground.ask", body.question[:200], {"who": who, "route": out["route"]}, True, t())
    return out | {"asked": who}


# ------------------------------------------------------------------ chat (Claude with each person's own API key)

class ChatKeyIn(BaseModel):
    api_key: str | None = None        # None: keep the saved key; "": remove it
    model: str = ""


class ChatTitleIn(BaseModel):
    title: str


class ChatMessageIn(BaseModel):
    text: str = ""
    uploads: list[dict] = Field(default_factory=list)


class ChatSettingsIn(BaseModel):
    company_key_for: str = "nobody"
    uploads: str = "everyone"
    company_model: str = ""
    company_key: str | None = None    # None: keep; "": remove
    guests: bool = False


def chat_user(request: Request, response: Response) -> dict:
    """The signed-in person, or a guest when an owner allowed chat without signing in (this computer only)."""
    u = core.session_user(request.cookies.get(COOKIE))
    if u is not None:
        return u
    from .public import classify
    if not chat.guests_on() or classify(request.scope) != "local":
        raise HTTPException(401, "Sign in first.")
    u, new = chat.guest_user(request.cookies.get(chat.GUEST_COOKIE))
    if new:
        response.set_cookie(chat.GUEST_COOKIE, new, max_age=365 * 86400, httponly=True, samesite="lax",
                            secure=request.url.scheme == "https", path="/")
    return u


CHAT = Depends(chat_user)


def _chat_me(u: dict) -> dict:
    p = chat.principal(u)
    me = chat.my_key(u["id"]) | {"can_upload": "upload" in p["capabilities"], "groups": p["groups"],
                                 "all_groups": p["all_groups"], "guest": chat.is_guest(u)}
    if me["guest"]:
        me |= {"can_chat": chat.guests_on(), "company_key_available": True, "key_set": False}
    return me


@app.get("/api/v1/chat/me")
def chat_me(u: dict = CHAT):
    return _chat_me(u)


@app.put("/api/v1/chat/key")
def chat_set_key(body: ChatKeyIn, u: dict = CHAT):
    if chat.is_guest(u):
        raise HTTPException(403, "Sign in to Context Layer to use your own Claude key.")
    if body.api_key:
        _rate_limit(f"chatkey:{u['id']}", 10, 600)
    try:
        chat.set_my_key(u["id"], body.api_key, body.model)
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit(u, "chat.key", "removed" if body.api_key == "" else "saved" if body.api_key else f"model {body.model}")
    return _chat_me(u)


@app.get("/api/v1/chats")
def chat_list(u: dict = CHAT):
    return {"chats": chat.list_chats(u["id"])}


@app.post("/api/v1/chats")
def chat_new(u: dict = CHAT):
    return {"chat": chat.new_chat(u["id"])}


def _chat_call(fn):
    try:
        return fn()
    except KeyError:
        raise HTTPException(404, "No such chat.")
    except (ValueError, chat.ChatError) as e:
        raise HTTPException(400, str(e))


@app.get("/api/v1/chats/{cid}")
def chat_get(cid: str, u: dict = CHAT):
    return _chat_call(lambda: chat.history(cid, u["id"]))


@app.patch("/api/v1/chats/{cid}")
def chat_rename(cid: str, body: ChatTitleIn, u: dict = CHAT):
    _chat_call(lambda: chat.rename_chat(cid, u["id"], body.title))
    return {"ok": True}


@app.delete("/api/v1/chats/{cid}")
def chat_delete(cid: str, u: dict = CHAT):
    _chat_call(lambda: chat.delete_chat(cid, u["id"]))
    return {"ok": True}


@app.post("/api/v1/chats/{cid}/files")
async def chat_files(cid: str, files: list[UploadFile] = File(...), groups: str = Form(""), u: dict = CHAT):
    if len(files) > 10:
        raise HTTPException(400, "Up to 10 files at a time.")
    data = [(f.filename or "file", await f.read()) for f in files]
    gl = [g for g in groups.split(",") if g.strip()] or None
    return _chat_call(lambda: chat.add_files(u, cid, data, gl))


@app.get("/api/v1/chat/jobs/{job_id}")
def chat_job(job_id: str, u: dict = CHAT):
    """Progress of an upload build, for the file chips (no build log: guests may poll this)."""
    j = jobs.get(job_id)
    if j is None or j["kind"] != "rebuild":
        raise HTTPException(404, "No such job.")
    return {"job": {"id": j["id"], "status": j["status"], "error": j["error"] and "The build failed."}}


@app.post("/api/v1/chats/{cid}/messages")
def chat_send(cid: str, body: ChatMessageIn, u: dict = CHAT):
    _chat_call(lambda: chat._own(cid, u["id"]))

    def stream():
        try:
            for ev in chat.send(u, cid, body.text, body.uploads):
                yield json.dumps(ev, ensure_ascii=False, default=str) + "\n"
        except chat.ChatError as e:
            yield json.dumps({"type": "error", "text": str(e)}) + "\n"
        except Exception as e:                                  # noqa: BLE001 - show, don't hang the page
            core.log_event("user", u["email"], "chat.answer", body.text[:200], {"chat": cid, "error": repr(e)[:300]}, False)
            yield json.dumps({"type": "error", "text": "Something went wrong on the server. Try again."}) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson", headers={"Cache-Control": "no-cache",
                                                                                     "X-Accel-Buffering": "no"})


@app.get("/api/v1/settings/chat")
def chat_settings(u: dict = Depends(ANY)):
    return chat.public_settings()


@app.put("/api/v1/settings/chat")
def chat_set_settings(body: ChatSettingsIn, u: dict = Depends(OWNER)):
    try:
        chat.save_settings(body.company_key_for, body.uploads, body.company_model, body.company_key, body.guests)
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit(u, "settings.chat", f"company key for {body.company_key_for}, uploads {body.uploads}, guests {body.guests}"
          + ("" if body.company_key is None else " (company key changed)" if body.company_key else " (company key removed)"))
    return chat.public_settings()


# ------------------------------------------------------------------ activity

@app.get("/api/v1/events")
def events(type: str | None = None, actor: str | None = None, q: str | None = None, limit: int = 200,
           u: dict = Depends(ANY)):
    if type not in (None, "", "agent", "user"):
        raise HTTPException(400, "type must be agent or user")
    return {"events": core.list_events(type or None, actor or None, (q or "").strip()[:100] or None, limit=limit)}


@app.get("/api/v1/events.csv")
def events_csv(type: str | None = None, u: dict = Depends(ADMIN)):
    rows = core.list_events(type or None, limit=5000)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time", "actor_type", "actor", "action", "target", "ok", "seconds", "detail"])
    for e in rows:
        w.writerow([e["time"], e["actor_type"], e["actor"], e["action"], e["target"], e["ok"], e["seconds"], e["detail"]])
    audit(u, "events.export", type or "all")
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": 'attachment; filename="context-layer-activity.csv"'})


# ------------------------------------------------------------------ quality

@app.get("/api/v1/quality")
def quality(u: dict = Depends(ANY)):
    """Scores after every build (deterministic, no AI model) with their trend; see app/quality.py."""
    from . import quality as Q
    return {"suites": Q.overview(), "after_build": Q.enabled(), "runs": Q.history(limit=40)}


class QualityRunIn(BaseModel):
    suites: list[str] = Field(default_factory=lambda: ["documents", "metrics", "graph"])


@app.post("/api/v1/quality/run")
def quality_run(body: QualityRunIn, u: dict = Depends(ADMIN)):
    from . import quality as Q
    bad = [s for s in body.suites if s not in Q.SUITES]
    if bad or not body.suites:
        raise HTTPException(400, f"Suites are {', '.join(Q.SUITES)}.")
    job = jobs.enqueue("quality", "Scoring quality", lambda i: Q.run(i["suites"], i["trigger"]), u["email"],
                       {"suites": body.suites, "trigger": "manual"})
    audit(u, "quality.run", ", ".join(body.suites))
    return {"job": job}


class QualitySettingsIn(BaseModel):
    after_build: bool


@app.put("/api/v1/quality/settings")
def quality_settings(body: QualitySettingsIn, u: dict = Depends(ADMIN)):
    core.set_setting("quality_after_build", body.after_build)
    audit(u, "quality.settings", "after_build", {"after_build": body.after_build})
    return {"after_build": body.after_build}


# ------------------------------------------------------------------ access: groups and team

@app.get("/api/v1/groups")
def groups(u: dict = Depends(ANY)):
    docs, agents_ = engine.documents(), [a for a in core.list_agents() if not a["revoked"]]
    return {"groups": [{"name": g, "documents": [d["name"] for d in docs if g in d["groups"]],
                        "agents": [a["name"] for a in agents_ if g in a["groups"]]} for g in engine.all_groups()]}


class UserIn(BaseModel):
    email: str
    name: str
    role: str
    password: str
    temporary: bool = False                      # they must choose their own password at first sign-in


class UserPatch(BaseModel):
    role: str | None = None
    disabled: bool | None = None
    groups: list[str] | None = None


@app.get("/api/v1/users")
def users(u: dict = Depends(ANY)):
    return {"users": core.list_users(), "roles": list(core.ROLES)}


@app.post("/api/v1/users")
def add_user(body: UserIn, u: dict = Depends(OWNER)):
    try:
        nu = core.create_user(body.email, body.name, body.password, body.role)
        if body.temporary:
            core.set_password(nu["id"], body.password, must_change=True)
            nu["must_change"] = True
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit(u, "users.add", nu["email"], {"role": nu["role"], "temporary": body.temporary})
    return {"user": nu}


# ------------------------------------------------------------------ people and teams directory (admins)

class PersonIn(BaseModel):
    email: str
    name: str
    title: str = ""
    team: str | None = None
    manager_email: str | None = None
    location: str = ""
    status: str = "active"


class TeamIn(BaseModel):
    name: str
    lead_email: str | None = None
    parent: str | None = None
    description: str = ""


class CsvIn(BaseModel):
    text: str


class InviteIn(BaseModel):
    email: str
    role: str = "viewer"


@app.get("/api/v1/directory")
def directory_list(q: str = "", team: str | None = None, left: bool = False, u: dict = Depends(ADMIN)):
    from . import directory
    return {"people": directory.list_people(q, team, left), "teams": directory.list_teams()}


@app.post("/api/v1/directory/people")
def directory_person(body: PersonIn, u: dict = Depends(ADMIN)):
    from . import directory
    try:
        p = directory.save_person(body.model_dump(), u["email"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit(u, "directory.person", p["email"], p)
    return {"person": p}


class OntologyIn(BaseModel):
    entity_types: list[dict]
    relation_types: list[dict] = []


@app.get("/api/v1/ontology")
def ontology_get(u: dict = Depends(ADMIN)):
    from graph import extract_ai, ontology
    cfg = extract_ai.config()
    return {"current": ontology.current(), "proposal": ontology.proposal(),
            "ai": {"provider": cfg["provider"], "model": cfg["model"], "where": extract_ai.destination(cfg),
                   "local": extract_ai.is_local(cfg), "problems": extract_ai.problems(cfg)}}


@app.post("/api/v1/ontology/propose")
def ontology_propose(u: dict = Depends(ADMIN)):
    from graph import extract_ai, ontology
    from . import jobs
    bad = extract_ai.problems()
    if bad:
        raise HTTPException(400, " ".join(bad) + " (Settings, AI extraction.)")
    audit(u, "ontology.propose", extract_ai.config()["model"])
    return jobs.enqueue("ontology", "Proposing knowledge-graph types with AI", lambda info: ontology.propose(), u["email"], {})


@app.post("/api/v1/ontology/apply")
def ontology_apply(body: OntologyIn, u: dict = Depends(ADMIN)):
    from graph import ontology
    try:
        r = ontology.apply(body.entity_types, body.relation_types, u["email"])
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    audit(u, "ontology.apply", f"{r['entity_types']} entity types, {r['relation_types']} link types")
    return r


@app.get("/api/v1/demo")
def demo_info(u: dict = Depends(ADMIN)):
    from . import demo
    return demo.summary()


@app.post("/api/v1/demo/load")
def demo_load(u: dict = Depends(OWNER)):
    from . import demo
    if not demo.available():
        raise HTTPException(404, "The demo files are missing from this installation.")
    audit(u, "demo.load", "Kestrel Labs")
    return demo.start(u["email"])


@app.post("/api/v1/directory/import")
def directory_import(body: CsvIn, u: dict = Depends(ADMIN)):
    from . import directory
    try:
        return directory.import_csv(body.text, u["email"])
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/v1/directory/teams")
def directory_team(body: TeamIn, u: dict = Depends(ADMIN)):
    from . import directory
    try:
        t = directory.save_team(body.name, body.lead_email, body.parent, body.description)
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit(u, "directory.team", t["name"])
    return {"team": t}


@app.delete("/api/v1/directory/teams/{name}")
def directory_team_delete(name: str, u: dict = Depends(ADMIN)):
    from . import directory, teams
    try:
        if any(t.get("parent") == name for t in directory.list_teams()):
            raise ValueError("Move or delete the teams inside it first.")
        directory.delete_team(name)
        teams.db().execute("DELETE FROM team_admins WHERE team = ?", (name,))
        from . import access
        access.db().execute("DELETE FROM ac_team WHERE team = ?", (name,))
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit(u, "directory.team.delete", name)
    return {"ok": True}


@app.post("/api/v1/directory/invites")
def directory_invite(body: InviteIn, request: Request, u: dict = Depends(ADMIN)):
    from . import directory
    from .public import base_url
    if body.role not in core.ROLES or body.role == "owner":
        raise HTTPException(400, "Invite as admin, publisher or viewer.")
    try:
        return directory.invite(body.email, u["email"], _public_url_base() or base_url(request.scope), body.role)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ------------------------------------------------------------------ teams as a hierarchy, team admins, member home

def _as_agent(u: dict) -> dict:
    return {"kind": "user", "user_id": u["id"], "email": u["email"], "role": u["role"], "name": u.get("name", ""),
            "groups": u.get("groups") or ["public"]}


def _team_by_slug(slug: str) -> str:
    from . import directory, teams
    t = next((x for x in directory.team_names() if teams.slug(x) == slug), None)
    if not t:
        raise HTTPException(404, "No such team.")
    return t


def _need_team(u: dict, team: str):
    from . import teams
    if not teams.manages(_as_agent(u), team):
        raise HTTPException(403, "You do not manage this team.")


class TeamIn(BaseModel):
    name: str
    parent: str | None = None
    description: str = ""


class TeamAdminIn(BaseModel):
    email: str
    admin: bool = True


class TeamPersonIn(BaseModel):
    email: str
    name: str = ""
    title: str = ""
    remove: bool = False


class TeamLinkIn(BaseModel):
    people: list[str] = []
    teams: list[str] = []
    remove: dict | None = None


@app.get("/api/v1/home")
def home(request: Request, u: dict = Depends(ANY)):
    """What a person sees first: their links, what they can reach, their changes and what waits for them."""
    from . import directory, sharing, teams
    from .public import base_url
    from semantic import defstore
    a = _as_agent(u)
    base = _public_url_base() or base_url(request.scope)
    org = core.get_setting("organisation", "") or "the company"
    links = [{"name": org, "kind": "company", "url": f"{base}/mcp"}]
    reach = [{"name": "Company documents and metrics", "kind": "company"}]
    for t in teams.reading_teams(a):
        lid = teams.layer_id(t)
        links.append({"name": f"{t} team", "kind": "team", "team": t, "url": f"{base}/mcp/teams/{sharing.slug_for(lid)}"})
        reach.append({"name": f"{t} team layer", "kind": "team"})
    shared, changes, pending = [], [], []
    if defstore.enabled():
        try:
            from . import context_tools as CT
            for lid in sharing.shared_with_me(a):
                if not lid.startswith("team:") or teams.team_for_layer(lid) not in teams.reading_teams(a):
                    shared.append({"layer": lid, "name": CT.layer_label(lid), "url": sharing.link(base, lid)})
            own = CT.own_layer(a)
            changes = [CT._h(h) for h in defstore.history([own], None, None, 8)]
        except Exception:                                  # noqa: BLE001 - store unreachable: show the rest
            pass
        try:
            from . import approvals
            pending = approvals.waiting_for(a, limit=100)
        except Exception:                                  # noqa: BLE001
            pass
    person = directory.db().execute("SELECT * FROM people WHERE email = ?", (u["email"],)).fetchone()
    return {"user": {"name": u.get("name"), "email": u["email"], "role": u["role"],
                     "title": person["title"] if person else "", "teams": teams.member_teams(u["email"])},
            "organisation": org, "links": links, "reach": reach, "shared_with_me": shared, "my_changes": changes,
            "waiting_for_me": pending, "manages": teams.managed_teams(a),
            "company_admin": teams.is_company_admin(a)}


# ------------------------------------------------------------------ people: one list for teams, accounts and admins

class GrantIn(BaseModel):
    datasets: list[str] = []
    connectors: list[str] = []
    roles: list[str] = []


class PeopleIn(BaseModel):
    email: str
    name: str = ""
    title: str = ""
    team: str | None = None
    level: str | None = None          # member | team_admin | company_admin (None: leave as it is)
    role: str | None = None           # admin | user: company admin or not (None: leave as it is)
    admin_of: list[str] | None = None  # the teams this person administers (None: leave as it is)
    access: GrantIn | None = None      # unique access for this person (None: leave as it is)


def _level(email: str, role: str | None, team: str | None) -> str:
    from . import teams
    if role == "owner":
        return "owner"
    if role == "admin":
        return "company_admin"
    if team and email in teams.admins_of(team):
        return "team_admin"
    return "member"


def _extra(email: str) -> dict:
    from . import access
    g = access.user_grant(email)
    return {"roles": g["roles"], "unique": bool(g["datasets"] or g["connectors"])}


@app.get("/api/v1/people")
def people_list(request: Request, u: dict = Depends(ANY)):
    """Everyone the caller looks after: company admins see all, team admins the teams they manage."""
    from . import directory, teams
    a = _as_agent(u)
    company = teams.is_company_admin(a)
    mine = set(teams.managed_teams(a))
    if not company and not mine:
        raise HTTPException(403, "Only admins manage people.")
    accounts = {r["email"]: r for r in core.db().execute("SELECT id, email, name, role, disabled, last_login FROM users")}
    pend = {r["email"] for r in directory.db().execute("SELECT email FROM invites WHERE used IS NULL AND expires > ?",
                                                       (core.now(),))}
    apps: dict = {}
    for g in oauth.grants(None):
        apps.setdefault(g["user"], set()).add(g["app"])
    admin_of: dict = {}
    for r in teams.db().execute("SELECT team, email FROM team_admins ORDER BY team"):
        admin_of.setdefault(r["email"], []).append(r["team"])
    out = []
    for p in directory.list_people():
        if not company and p.get("team") not in mine:
            continue
        acc = accounts.get(p["email"])
        out.append({"email": p["email"], "name": p["name"], "title": p.get("title") or "", "team": p.get("team"),
                    "admin_of": admin_of.get(p["email"], []), **_extra(p["email"]), "apps": sorted(apps.get(p["email"], [])),
                    "level": _level(p["email"], acc["role"] if acc else None, p.get("team")),
                    "account": "disabled" if acc and acc["disabled"] else "active" if acc else
                               "invited" if p["email"] in pend else "none",
                    "last_login": acc["last_login"] if acc else None})
    if company:                                    # accounts that are not in the directory yet
        known = {x["email"] for x in out}
        for e, acc in accounts.items():
            if e not in known and not e.endswith("@guest.local"):
                out.append({"email": e, "name": acc["name"] or e.split("@")[0], "title": "", "team": None,
                            "admin_of": admin_of.get(e, []), **_extra(e), "apps": sorted(apps.get(e, [])),
                            "level": _level(e, acc["role"], None), "account": "disabled" if acc["disabled"] else "active",
                            "last_login": acc["last_login"]})
    tr = teams.tree()
    if not company:
        tr = [t for t in tr if t["name"] in mine]
    from .public import base_url
    return {"people": sorted(out, key=lambda x: x["name"].lower()), "teams": tr, "company_admin": company,
            "you": u["email"], "base": _public_url_base() or base_url(request.scope)}


@app.post("/api/v1/people")
def people_save(body: PeopleIn, request: Request, u: dict = Depends(ANY)):
    """Add someone or change their team or access level; invites them if they have no account."""
    from . import directory, teams
    from .public import base_url
    a = _as_agent(u)
    company = teams.is_company_admin(a)
    email = body.email.strip().lower()
    row = directory.db().execute("SELECT * FROM people WHERE email = ?", (email,)).fetchone()
    old_team = row["team"] if row else None
    new_team = (body.team or None) if body.team is not None else old_team
    if not company:                                 # team admins: only within the teams they manage
        for t in {old_team, new_team} - {None}:
            if not teams.manages(a, t):
                raise HTTPException(403, f"You do not manage {t}.")
        if new_team is None or body.level == "company_admin":
            raise HTTPException(403, "Only a company admin can do that.")
    acc = core.db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if acc and acc["role"] == "owner" and body.level and body.level != "owner":
        raise HTTPException(400, "The owner stays the owner.")
    name = body.name or (row["name"] if row else "") or email.split("@")[0].replace(".", " ").title()
    try:
        directory.save_person({**(dict(row) if row else {}), "email": email, "name": name,
                               "title": body.title or (row["title"] if row else ""), "team": new_team,
                               "status": "active"}, u["email"])
        if body.admin_of is not None:
            if not company:
                raise HTTPException(403, "Only a company admin chooses team admins.")
            want, have = set(body.admin_of), {t for t in teams.managed_teams_direct(email)}
            for t in want - have:
                teams.set_admin(a, t, email, True)
            for t in have - want:
                teams.set_admin(a, t, email, False)
        if body.level and new_team:
            want = body.level == "team_admin"
            if want != (email in teams.admins_of(new_team)):
                teams.set_admin(a, new_team, email, want)
        if body.role and not body.level:
            body.level = "company_admin" if body.role == "admin" else "member"
        if body.level and acc and acc["role"] != "owner":
            role = "admin" if body.level == "company_admin" else ("viewer" if acc["role"] == "admin" else acc["role"])
            if role != acc["role"]:
                if not can_owner(u):
                    raise HTTPException(403, "Only the owner changes company admins.")
                core.update_user(acc["id"], role=role)
        if body.access is not None:
            _set_user_grant(a, u, email, body.access.datasets, body.access.connectors, body.access.roles)
        out = {"email": email, "team": new_team}
        if not acc:
            role = "admin" if body.level == "company_admin" and can_owner(u) else "viewer"
            out["invite"] = directory.invite(email, u["email"], _public_url_base() or base_url(request.scope), role)
    except PermissionError as e:
        raise HTTPException(403, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    audit(u, "people.save", email, {"team": new_team, "level": body.level})
    return out


@app.post("/api/v1/people/invite")
def people_invite(body: PeopleIn, request: Request, u: dict = Depends(ANY)):
    """A fresh sign-up link for someone without an account (the old one stops working when used)."""
    from . import directory
    from .public import base_url
    a = _admin_agent(u)
    email = body.email.strip().lower()
    _may_manage_person(a, email)
    role = core.get_setting(f"invite_role:{email}", "viewer")
    r = directory.invite(email, u["email"], _public_url_base() or base_url(request.scope), role)
    audit(u, "directory.invite", email)
    return r


@app.post("/api/v1/people/remove")
def people_remove(body: PeopleIn, u: dict = Depends(ANY)):
    """Company admin: the person left (account off, apps signed out). Team admin: out of the team."""
    from . import directory, teams
    a = _as_agent(u)
    email = body.email.strip().lower()
    row = directory.db().execute("SELECT * FROM people WHERE email = ?", (email,)).fetchone()
    if email == u["email"]:
        raise HTTPException(400, "You cannot remove yourself.")
    if teams.is_company_admin(a):
        if row:
            directory.save_person({**dict(row), "status": "left"}, u["email"])
        else:
            acc = core.db().execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
            if acc:
                core.update_user(acc["id"], disabled=True)
        audit(u, "people.left", email)
        return {"left": email}
    if not row or not row["team"] or not teams.manages(a, row["team"]):
        raise HTTPException(403, "You can only take people out of teams you manage.")
    if email in teams.admins_of(row["team"]):
        teams.set_admin(a, row["team"], email, False)
    directory.save_person({**dict(row), "team": None}, u["email"])
    audit(u, "team.person_removed", f"{row['team']}: {email}")
    return {"removed": email}


# ------------------------------------------------------------------ access: data sets, roles, team and personal grants

class DatasetIn(BaseModel):
    name: str
    description: str = ""


class RoleIn(BaseModel):
    name: str
    description: str = ""
    datasets: list[str] = []
    connectors: list[str] = []
    old: str | None = None


class UserGrantIn(GrantIn):
    email: str


def _admin_agent(u: dict) -> dict:
    from . import teams
    a = _as_agent(u)
    if not teams.is_company_admin(a) and not teams.managed_teams(a):
        raise HTTPException(403, "Only admins manage access.")
    return a


def _company(u: dict) -> dict:
    from . import teams
    a = _as_agent(u)
    if not teams.is_company_admin(a):
        raise HTTPException(403, "Only company admins can change this.")
    return a


@app.get("/api/v1/access")
def access_overview(request: Request, u: dict = Depends(ANY)):
    from . import access, directory, teams
    from .public import base_url
    a = _admin_agent(u)
    company = teams.is_company_admin(a)
    mine = set(directory.team_names() if company else teams.managed_teams(a))
    docs = engine.documents()
    ds = []
    for d in access.datasets():
        if not company and d["kind"] == "team" and d["team"] not in mine:
            continue
        ds.append({**d, "files": sum(1 for f in docs if d["name"] in f["groups"]), "who": access.who_can_see(d["name"])})
    tlist = []
    for t in teams.tree():
        if t["name"] in mine:
            tlist.append({**t, "dataset": access.team_set(t["name"]), "grant": access.team_grant(t["name"]),
                          "link": f"{_public_url_base() or base_url(request.scope)}/mcp/teams/{t['slug']}"})
    return {"datasets": ds, "roles": access.roles(), "connectors": access.CONNECTORS, "teams": tlist,
            "can_give": access.manageable(a), "company_admin": company}


# ---- row rules for numbers: metrics only count the rows a data set / team / role may see (semantic/access.py)

class RowRuleIn(BaseModel):
    entity: str
    column: str
    kind: str                              # dataset | team | role
    audience: str
    values: list[str]


@app.get("/api/v1/access/row-rules")
def row_rules(u: dict = Depends(ADMIN)):
    from semantic import access as RA
    from semantic.yamlio import load_model
    try:
        ents = [{"name": e.name, "label": e.label or e.name,
                 "columns": [d.column for d in e.dimensions if d.type in ("category", "key", "text")]}
                for e in load_model().entities]
    except Exception:                                      # noqa: BLE001 - no data layer yet
        ents = []
    return {"rules": RA.rules(), "entities": ents, "kinds": list(RA.KINDS)}


@app.put("/api/v1/access/row-rules")
def row_rule_put(body: RowRuleIn, u: dict = Depends(ADMIN)):
    from semantic import access as RA
    try:
        RA.put_rule(body.entity, body.column, body.kind, body.audience, body.values, "manual", u["email"])
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    engine.clear_ask_cache()
    audit(u, "access.row_rule", f"{body.entity}.{body.column}", {"for": f"{body.kind}:{body.audience}",
                                                                 "values": body.values[:20]})
    return {"rules": RA.rules()}


@app.delete("/api/v1/access/row-rules")
def row_rule_delete(entity: str, column: str, kind: str, audience: str, u: dict = Depends(ADMIN)):
    from semantic import access as RA
    if not RA.delete_rule(entity, column, kind, audience):
        raise HTTPException(404, "No such rule.")
    engine.clear_ask_cache()
    audit(u, "access.row_rule.delete", f"{entity}.{column}", {"for": f"{kind}:{audience}"})
    return {"rules": RA.rules()}


@app.post("/api/v1/access/datasets")
def access_dataset_add(body: DatasetIn, u: dict = Depends(ANY)):
    from . import access
    _company(u)
    try:
        r = access.add_dataset(body.name, body.description, u["email"])
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    audit(u, "access.dataset", r["name"])
    return r


@app.delete("/api/v1/access/datasets/{name}")
def access_dataset_delete(name: str, u: dict = Depends(ANY)):
    from . import access
    _company(u)
    try:
        access.delete_dataset(name)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    audit(u, "access.dataset_deleted", name)
    return {"ok": True}


@app.post("/api/v1/access/roles")
def access_role_save(body: RoleIn, u: dict = Depends(ANY)):
    from . import access
    _company(u)
    try:
        r = access.save_role(body.name, body.description, body.datasets, body.connectors, u["email"], body.old)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    audit(u, "access.role", r["name"], r)
    return r


@app.delete("/api/v1/access/roles/{name}")
def access_role_delete(name: str, u: dict = Depends(ANY)):
    from . import access
    _company(u)
    try:
        access.delete_role(name)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    audit(u, "access.role_deleted", name)
    return {"ok": True}


@app.put("/api/v1/access/teams/{slug}")
def access_team_save(slug: str, body: GrantIn, u: dict = Depends(ANY)):
    from . import access
    _company(u)
    t = _team_by_slug(slug)
    try:
        access.set_team_grant(t, body.datasets, body.connectors, body.roles, u["email"])
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    audit(u, "access.team", t, body.model_dump())
    return {"team": t, "grant": access.team_grant(t)}


def _may_manage_person(a: dict, email: str):
    from . import directory, teams
    if teams.is_company_admin(a):
        return
    row = directory.db().execute("SELECT team FROM people WHERE email = ?", (email,)).fetchone()
    if not row or not row["team"] or not teams.manages(a, row["team"]):
        raise HTTPException(403, "You can only manage people in your teams.")


@app.get("/api/v1/access/user")
def access_user(email: str, u: dict = Depends(ANY)):
    from . import access
    a = _admin_agent(u) if email.lower() != u["email"] else _as_agent(u)
    email = email.strip().lower()
    if email != u["email"]:
        _may_manage_person(a, email)
    acc = core.db().execute("SELECT role FROM users WHERE email = ?", (email,)).fetchone()
    e = access.effective(email, acc["role"] if acc else None)
    labels = {d["name"]: d["label"] for d in access.datasets()}
    return {"email": email, "grant": access.user_grant(email), "effective": e, "labels": labels,
            "connectors": access.CONNECTORS}


def _set_user_grant(a: dict, u: dict, email: str, ds: list[str], cons: list[str], rs: list[str]):
    from . import access
    _may_manage_person(a, email)
    can = access.manageable(a)
    if not can["all"]:
        old = access.user_grant(email)
        extra_ds = [d for d in ds if d not in can["datasets"] and d not in old["datasets"]]
        extra_c = [c for c in cons if c not in can["connectors"] and c not in old["connectors"]]
        extra_r = [r for r in rs if r not in can["roles"] and r not in old["roles"]]
        if extra_ds or extra_c or extra_r:
            raise HTTPException(403, "You can only give access your teams have: " +
                                ", ".join(extra_ds + extra_c + extra_r))
        # what a company admin gave stays: a team admin only changes what they could have given
        ds = sorted(set(ds) | {d for d in old["datasets"] if d not in can["datasets"]})
        cons = list(dict.fromkeys(list(cons) + [c for c in old["connectors"] if c not in can["connectors"]]))
        rs = sorted(set(rs) | {r for r in old["roles"] if r not in can["roles"]})
    try:
        access.set_user_grant(email, ds, cons, rs, u["email"])
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.put("/api/v1/access/user")
def access_user_save(body: UserGrantIn, u: dict = Depends(ANY)):
    a = _admin_agent(u)
    email = body.email.strip().lower()
    _set_user_grant(a, u, email, body.datasets, body.connectors, body.roles)
    audit(u, "access.user", email, body.model_dump())
    return access_user(email, u)


# ------------------------------------------------------------------ data: files and tables

def _data_sets_for(a: dict) -> set[str] | None:
    """Data sets this admin may put files in (None: any)."""
    from . import access, teams
    if teams.is_company_admin(a):
        return None
    return {access.team_set(t) for t in teams.managed_teams(a)}


@app.get("/api/v1/data")
def data_overview(u: dict = Depends(ANY)):
    from . import access, teams
    a = _admin_agent(u)
    company = teams.is_company_admin(a)
    allowed = _data_sets_for(a)
    labels = {d["name"]: d["label"] for d in access.datasets()}
    files = []
    owners = {}
    for r in core.db().execute("SELECT id, name, email FROM users"):
        owners[f"layer:user:{r['id']}"] = r["name"] or r["email"]
    visible = None if company else set(access.manageable(a)["datasets"])
    for f in engine.documents():
        sets = [g for g in f["groups"] if not g.startswith("layer:")]
        private = [owners.get(g, "a person") for g in f["groups"] if g.startswith("layer:user:")]
        if visible is not None and not (set(sets) & visible):
            continue
        files.append({**f, "datasets": sets, "private_to": private,
                      "editable": company or (bool(sets) and set(sets) <= allowed)})
    ds = [d for d in access.datasets() if company or d["name"] in visible]
    run = jobs.running()
    return {"files": files, "datasets": [{**d, "files": sum(1 for f in files if d["name"] in f["datasets"])} for d in ds],
            "labels": labels, "upload_to": sorted(allowed) if allowed is not None else [d["name"] for d in ds],
            "tables": engine.tables() if company else [], "company_admin": company,
            "pending": sum(1 for f in files if f["status"] != "ready"), "job": run and jobs.public(run),
            "sources": _sources_summary() if company else []}


def _sources_summary() -> list[dict]:
    try:
        from sources import manage
        return manage.list_all()
    except Exception:                                      # noqa: BLE001 - the Data tab works without sources
        return []


@app.post("/api/v1/data/files")
async def data_upload(files: list[UploadFile] = File(...), datasets: str = Form("public"), u: dict = Depends(ANY)):
    a = _admin_agent(u)
    allowed = _data_sets_for(a)
    want = [d for d in datasets.split(",") if d.strip()] or ["public"]
    if allowed is not None and not set(want) <= allowed:
        raise HTTPException(403, "Team admins add files to their team's data set.")
    saved, errors = [], []
    for f in files:
        data = await f.read()
        if len(data) > MAX_UPLOAD_MB * 2 ** 20:
            errors.append(f"{f.filename}: larger than {MAX_UPLOAD_MB} MB")
            continue
        try:
            saved.append(engine.save_document(f.filename or "file", data, want))
        except ValueError as e:
            errors.append(str(e))
    if saved:
        audit(u, "documents.upload", ", ".join(x["name"] for x in saved), {"groups": want})
    if not saved and errors:
        raise HTTPException(400, "; ".join(errors))
    return {"saved": saved, "errors": errors}


def _editable_file(a: dict, name: str) -> dict:
    allowed = _data_sets_for(a)
    f = next((x for x in engine.documents() if x["name"] == name), None)
    if f is None:
        raise HTTPException(404, "No such file.")
    sets = {g for g in f["groups"] if not g.startswith("layer:")}
    if allowed is not None and not (sets and sets <= allowed):
        raise HTTPException(403, "This file is not only in your team's data set.")
    return f


@app.patch("/api/v1/data/files/{name}")
def data_file_sets(name: str, body: GroupsIn, u: dict = Depends(ANY)):
    a = _admin_agent(u)
    _editable_file(a, name)
    allowed = _data_sets_for(a)
    if allowed is not None and not set(body.groups) <= allowed:
        raise HTTPException(403, "Team admins keep files in their team's data set.")
    return set_doc_groups(name, body, u)


@app.delete("/api/v1/data/files/{name}")
def data_file_delete(name: str, u: dict = Depends(ANY)):
    a = _admin_agent(u)
    _editable_file(a, name)
    return delete_doc(name, u)


@app.post("/api/v1/data/prepare")
def data_prepare(u: dict = Depends(ANY)):
    _admin_agent(u)
    label, fn, _ = engine.JOBS["documents"]
    try:
        return {"job": jobs.start("documents", label, fn, u["email"])}
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e


# ------------------------------------------------------------------ sources: GitHub, databases, S3 folders (company admins)
# ("connectors" in this app means what Claude may use for a person; outside systems are "sources".)

class SourceIn(BaseModel):
    kind: str
    name: str = ""
    config: dict = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)
    dataset: str | None = None
    every_minutes: int = 360


class SourcePatch(BaseModel):
    name: str | None = None
    config: dict | None = None
    secrets: dict[str, str] | None = None
    dataset: str | None = None
    every_minutes: int | None = None
    status: str | None = None


@app.get("/api/v1/sources")
def sources_list(u: dict = Depends(ADMIN)):
    from sources import manage
    return {"sources": manage.list_all(), "kinds": manage.catalog()}


@app.post("/api/v1/sources")
def sources_create(body: SourceIn, u: dict = Depends(ADMIN)):
    from sources import manage
    try:
        s = manage.create(body.kind, body.name, body.config, body.secrets, body.dataset, body.every_minutes, u["email"])
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    audit(u, "source.create", s["name"], {"kind": s["kind"], "dataset": s["dataset"]})
    return s


@app.patch("/api/v1/sources/{sid}")
def sources_update(sid: str, body: SourcePatch, u: dict = Depends(ADMIN)):
    from sources import manage
    try:
        s = manage.update(sid, body.config, body.secrets, body.dataset, body.every_minutes, body.status, body.name,
                          by=u["email"])
    except KeyError:
        raise HTTPException(404, "No such source.")
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    audit(u, "source.update", s["name"], {"fields": [k for k, v in body.model_dump().items()
                                                     if v is not None and k != "secrets"]
                                          + (["secrets"] if body.secrets else [])})
    return s


@app.delete("/api/v1/sources/{sid}")
def sources_delete(sid: str, u: dict = Depends(ADMIN)):
    from sources import manage
    try:
        out = manage.delete(sid, u["email"])
    except KeyError:
        raise HTTPException(404, "No such source.")
    audit(u, "source.delete", sid, out)
    return out


@app.post("/api/v1/sources/{sid}/test")
def sources_test(sid: str, u: dict = Depends(ADMIN)):
    from sources import manage
    try:
        return manage.test(sid)
    except KeyError:
        raise HTTPException(404, "No such source.")


@app.post("/api/v1/sources/{sid}/sync")
def sources_sync(sid: str, u: dict = Depends(ADMIN)):
    from sources import manage
    try:
        job = manage.queue_sync(sid, u["email"])
    except KeyError:
        raise HTTPException(404, "No such source.")
    audit(u, "source.sync", sid)
    return {"job": job}


@app.post("/hooks/{kind}/{sid}", include_in_schema=False)
async def source_webhook(kind: str, sid: str, request: Request):
    """Signed webhook from a source (GitHub: X-Hub-Signature-256). Queues a sync; the schedule is the fallback."""
    from sources import manage
    body = await request.body()
    if len(body) > 5 * 2 ** 20:
        raise HTTPException(413, "Too large.")
    _rate_limit(f"hook:{sid}", limit=60, window=60)
    code, msg = manage.webhook(kind, sid, body, request.headers.get("x-hub-signature-256", ""),
                               request.headers.get("x-github-event", ""))
    return JSONResponse({"detail": msg}, status_code=code)


# ------------------------------------------------------------------ activity for admins (plain words)

_KIND = {"questions": ("tool.",), "changes": ("access.", "people.", "team.", "documents.", "definitions.", "directory.",
                                              "approval.", "users.", "settings", "tables."),
         "signins": ("login", "oauth.login", "logout")}


def _plain(e: dict, names: dict | None = None) -> str:
    a, t = e["action"], e["target"] or ""
    t = (names or {}).get(t, t)
    if a.startswith("tool."):
        q = (e.get("detail") or {}).get("args", {}) if isinstance(e.get("detail"), dict) else {}
        ask = q.get("question") or q.get("query") or q.get("rule") or q.get("term") or ""
        return f"used {a[5:].replace('_', ' ')}" + (f": “{str(ask)[:120]}”" if ask else "")
    words = {"login": "signed in to the web app", "oauth.login": f"connected {t}", "logout.everywhere": "signed out everywhere",
             "documents.upload": f"added files: {t}", "documents.delete": f"deleted the file {t}",
             "documents.groups": f"changed who can see {t}", "people.save": f"updated {t}", "people.left": f"removed {t}",
             "team.person_removed": f"took {t.split(': ')[-1]} out of {t.split(': ')[0]}", "access.user": f"changed access for {t}",
             "access.team": f"changed access for the team {t}", "access.role": f"saved the role {t}",
             "access.role_deleted": f"deleted the role {t}", "access.dataset": f"added the data set {t}",
             "team.create": f"created the team {t}", "approval.approve": f"approved {t}", "approval.decline": f"declined {t}",
             "definitions.undo": "undid a change", "directory.team.delete": f"deleted the team {t}",
             "users.add": f"created an account for {t}", "directory.invite": f"invited {t}", "setup": f"set up {t}",
             "access.dataset_deleted": f"deleted the data set {t}",
             "tables.upload": f"added the table {t}", "users.update": f"changed the account of {t}"}
    return words.get(a, f"{a.replace('.', ' ')} {t}".strip())


@app.get("/api/v1/activity")
def activity(kind: str = "", q: str = "", limit: int = 150, u: dict = Depends(ANY)):
    from . import directory, teams
    a = _admin_agent(u)
    rows = core.list_events(None, None, (q or "").strip()[:100] or None, limit=2000)
    if not teams.is_company_admin(a):
        mine = set(teams.managed_teams(a))
        ppl = [p for p in directory.list_people() if p.get("team") in mine]
        emails = {p["email"] for p in ppl}
        names = {p["name"] for p in ppl}
        rows = [e for e in rows if e["actor"] in emails or e["actor"].split(" via ")[0] in names]
    if kind in _KIND:
        rows = [e for e in rows if e["action"].startswith(_KIND[kind])]
    names = {r["email"]: r["name"] for r in core.db().execute("SELECT email, name FROM users")}
    out = [{"id": e["id"], "time": e["time"], "who": names.get(e["actor"], e["actor"].split(" via ")[0]), "app": e["actor"].split(" via ")[1]
            if " via " in e["actor"] else ("web app" if e["actor_type"] == "user" else "key"),
            "what": _plain(e, names), "ok": e["ok"]} for e in rows[:max(1, min(limit, 500))]]
    try:
        from . import approvals
        waiting = approvals.waiting_for(a, limit=50)
    except Exception:                                      # noqa: BLE001
        waiting = []
    return {"events": out, "waiting": waiting}


class OrgIn(BaseModel):
    organisation: str


@app.get("/api/v1/admin/settings")
def admin_settings(request: Request, u: dict = Depends(ANY)):
    _company(u)
    return {"organisation": core.get_setting("organisation", ""),
            "link": _public_url() or str(request.base_url).rstrip("/") + "/mcp",
            "session_idle_minutes": core.SESSION_IDLE_MIN, "session_hours": core.SESSION_DAYS * 24,
            "min_password": core.MIN_PASSWORD, "version": core.VERSION,
            "apps": len(oauth.grants(None)), "users": len(core.list_users())}


def can_owner(u: dict) -> bool:
    return u.get("role") == "owner"


@app.post("/api/v1/home/changes/{hid}/undo")
def home_undo(hid: int, u: dict = Depends(ANY)):
    """Anyone can undo a change in their own layer (the same rule as the undo_change tool)."""
    from . import context_tools as CT
    _defs()
    try:
        with CT.defstore.acting(u["email"], "web app"):
            r = CT.undo_change(_as_agent(u), hid)
    except CT.Refused as e:
        raise HTTPException(403, str(e)) from e
    audit(u, "definitions.undo", str(hid), {})
    return r


@app.get("/api/v1/teams")
def teams_list(u: dict = Depends(ANY)):
    from . import directory, teams
    a = _as_agent(u)
    mine = set(teams.managed_teams(a))
    rows = [r for r in teams.tree() if teams.is_company_admin(a) or r["name"] in mine]
    unassigned = directory.db().execute("SELECT COUNT(*) AS n FROM people WHERE status = 'active' AND "
                                        "(team IS NULL OR team = '')").fetchone()["n"]
    invites = directory.db().execute("SELECT COUNT(*) AS n FROM invites WHERE used IS NULL AND expires > ?",
                                     (core.now(),)).fetchone()["n"]
    return {"teams": rows, "unassigned": unassigned, "pending_invites": invites,
            "company_admin": teams.is_company_admin(a)}


@app.post("/api/v1/teams")
def team_create(body: TeamIn, u: dict = Depends(ANY)):
    from . import directory, teams
    a = _as_agent(u)
    if not teams.is_company_admin(a) and not (body.parent and teams.manages(a, body.parent)):
        raise HTTPException(403, "Company admins add top-level teams; a team admin can add teams under their own.")
    try:
        teams.check_parent(body.name, body.parent)
        r = directory.save_team(body.name, None, body.parent, body.description)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    audit(u, "team.create", r["name"], {"parent": body.parent})
    return r


@app.get("/api/v1/teams/{slug}")
def team_get(slug: str, request: Request, u: dict = Depends(ANY)):
    from . import directory, teams
    from .public import base_url
    t = _team_by_slug(slug)
    a = _as_agent(u)
    if not (teams.manages(a, t) or t in teams.reading_teams(a)):
        raise HTTPException(403, "You are not in this team.")
    out = teams.summary(t, a, _public_url_base() or base_url(request.scope))
    names = {r["email"]: r for r in core.db().execute("SELECT email, last_login, disabled FROM users")}
    pend = {r["email"] for r in directory.db().execute("SELECT email FROM invites WHERE used IS NULL AND expires > ?",
                                                       (core.now(),))}
    admins = set(out["admins"])
    out["people"] = [{**p, "is_admin": p["email"] in admins,
                      "account": "active" if p["email"] in names and not names[p["email"]]["disabled"] else
                                 "invited" if p["email"] in pend else "none",
                      "last_login": names.get(p["email"], {}) and names[p["email"]]["last_login"]}
                     for p in directory.list_people(team=t)]
    if out["you_manage"]:
        try:
            from . import approvals
            out["waiting"] = approvals.waiting_for(a, team=t)
        except Exception:                                  # noqa: BLE001
            out["waiting"] = []
        try:
            from semantic import defstore
            out["items"] = [i for i in defstore.items(out["layer"]) if i["kind"] != "meta"] if defstore.enabled() else []
        except Exception:                                  # noqa: BLE001
            out["items"] = []
        out["activity"] = [dict(e) for e in core.db().execute(
            "SELECT time, actor, action, target FROM events WHERE target LIKE ? ORDER BY id DESC LIMIT 20",
            (f"%{t}%",))]
    return out


@app.post("/api/v1/teams/{slug}/admins")
def team_admin_set(slug: str, body: TeamAdminIn, u: dict = Depends(ANY)):
    from . import teams
    t = _team_by_slug(slug)
    try:
        teams.set_admin(_as_agent(u), t, body.email, body.admin)
    except PermissionError as e:
        raise HTTPException(403, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    audit(u, "team.admin" if body.admin else "team.admin_removed", f"{t}: {body.email}")
    return {"admins": teams.admins_of(t)}


@app.post("/api/v1/teams/{slug}/people")
def team_person(slug: str, body: TeamPersonIn, request: Request, u: dict = Depends(ANY)):
    """Add someone to the team (and invite them if they have no account), or take them out of it."""
    from . import directory
    from .public import base_url
    t = _team_by_slug(slug)
    _need_team(u, t)
    email = body.email.strip().lower()
    row = directory.db().execute("SELECT * FROM people WHERE email = ?", (email,)).fetchone()
    if body.remove:
        if row and row["team"] == t:
            directory.save_person({**dict(row), "team": None}, u["email"])
        audit(u, "team.person_removed", f"{t}: {email}")
        return {"removed": email}
    p = dict(row) if row else {"email": email, "name": body.name or email.split("@")[0].replace(".", " ").title(),
                               "title": body.title}
    try:
        directory.save_person({**p, "team": t, "status": "active"}, u["email"])
        out = {"added": email}
        if not core.db().execute("SELECT 1 FROM users WHERE email = ? AND disabled = 0", (email,)).fetchone():
            out["invite"] = directory.invite(email, u["email"], _public_url_base() or base_url(request.scope), "viewer")
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    audit(u, "team.person_added", f"{t}: {email}")
    return out


@app.post("/api/v1/teams/{slug}/link")
def team_link(slug: str, body: TeamLinkIn, u: dict = Depends(ANY)):
    """Who else may use the team's link (its layer): people and other teams. Members always may."""
    from . import sharing, teams
    t = _team_by_slug(slug)
    _need_team(u, t)
    lid = teams.ensure_layer(t)
    if body.remove:
        sharing.remove_share(lid, body.remove.get("kind", ""), body.remove.get("target", ""))
    added = []
    if body.people or body.teams:
        try:
            added = sharing.add_shares(lid, u["email"], body.people, body.teams)
        except Exception as e:                             # noqa: BLE001 - Refused: unclear name
            raise HTTPException(400, str(e)) from e
    audit(u, "team.link", t, {"added": added, "removed": body.remove})
    return {"link_extra": sharing.share_targets(lid)}


class DecideIn(BaseModel):
    approve: bool
    reason: str = ""


@app.get("/api/v1/approvals")
def approvals_list(status: str = "waiting", u: dict = Depends(ANY)):
    from . import approvals, teams
    a = _as_agent(u)
    if not teams.is_company_admin(a) and not teams.managed_teams(a):
        raise HTTPException(403, "Only admins approve changes.")
    checks = []
    if teams.is_company_admin(a):                        # metrics the nightly check took out (semantic/monitor.py)
        from semantic import monitor
        checks = monitor.taken_out()
    if status == "waiting":
        return {"items": approvals.waiting_for(a), "metric_checks": checks}
    return {"items": approvals.history(a, status), "metric_checks": checks}


@app.post("/api/v1/metric-check/run")
def metric_check_run(u: dict = Depends(ADMIN)):
    """Run the nightly metric check now (e.g. after fixing a renamed column)."""
    from semantic import monitor
    job = jobs.enqueue("monitor", "Checking metric definitions", lambda info: monitor.run(u["email"]), u["email"], {})
    audit(u, "metrics.check", "now")
    return {"job": job}


@app.post("/api/v1/approvals/file/{rid}")
def approvals_decide_file(rid: int, body: DecideIn, u: dict = Depends(ANY)):
    from . import doc_requests
    try:
        r = doc_requests.decide(_as_agent(u), rid, body.approve, body.reason)
    except KeyError as e:
        raise HTTPException(409, str(e)) from e
    except PermissionError as e:
        raise HTTPException(403, str(e)) from e
    audit(u, "approval.approve" if body.approve else "approval.decline", r["item"], r)
    return r


@app.post("/api/v1/approvals/{rid}")
def approvals_decide(rid: int, body: DecideIn, u: dict = Depends(ANY)):
    from . import approvals
    try:
        r = approvals.decide(_as_agent(u), rid, body.approve, body.reason)
    except KeyError as e:
        raise HTTPException(409, str(e)) from e
    except PermissionError as e:
        raise HTTPException(403, str(e)) from e
    audit(u, "approval.approve" if body.approve else "approval.decline", r["item"], r)
    return r


def _public_url_base() -> str:
    url = _public_url() or ""
    return url.removesuffix("/mcp") if url else ""


# ------------------------------------------------------------------ layers, sharing, definition history (admins)

def _defs():
    from semantic import defstore
    if not defstore.enabled():
        raise HTTPException(409, "The definitions store is off (CL_DEFS_DSN not set).")
    return defstore


@app.get("/api/v1/layers")
def layers_overview(u: dict = Depends(ADMIN)):
    ds = _defs()
    from . import sharing
    from .context_tools import layer_label
    x = sharing._db()
    out = []
    for lay in x.all("SELECT * FROM layers ORDER BY kind, created"):
        lid = lay["id"]
        n = x.one("SELECT COUNT(*) AS n FROM definitions WHERE layer_id = ? AND kind != 'meta'", (lid,))["n"]
        last = x.one("SELECT by_user, at FROM history WHERE layer_id = ? ORDER BY id DESC LIMIT 1", (lid,))
        using = x.one("SELECT COUNT(*) AS n FROM layer_use WHERE layer_id = ?", (lid,))["n"]
        out.append({"id": lid, "name": layer_label(lid), "kind": lay["kind"], "owner": lay["owner"], "items": n,
                    "last_change": last, "shared_with": sharing._list(lid) if lid != ds.COMPANY else ["everyone"],
                    "link": (sharing.link(_public_url_base(), lid) if lid != ds.COMPANY and _public_url_base() else None),
                    "in_use_by": using})
    # clashes: the same metric defined differently in more than one layer
    rows = x.all("SELECT layer_id, key FROM definitions WHERE kind = 'metric' AND removed = 0")
    by: dict = {}
    for r in rows:
        by.setdefault(r["key"], []).append(layer_label(r["layer_id"]))
    clashes = [{"metric": k, "layers": v} for k, v in sorted(by.items()) if len(v) > 1]
    locks = [r["key"] for r in x.all("SELECT key FROM definitions WHERE layer_id = 'company' AND kind = 'lock' AND removed = 0")]
    return {"layers": out, "clashes": clashes, "locked": locks}


@app.get("/api/v1/layers/{lid}/items")
def layer_items(lid: str, u: dict = Depends(ADMIN)):
    ds = _defs()
    return {"items": [i for i in ds.items(lid) if i["kind"] != "meta"]}


@app.get("/api/v1/definitions/history")
def definitions_history(layer: str | None = None, kind: str | None = None, key: str | None = None, limit: int = 100,
                        u: dict = Depends(ADMIN)):
    ds = _defs()
    from .context_tools import layer_label
    rows = ds.history([layer] if layer else None, kind, key, min(limit, 500))
    for r in rows:
        r["layer_name"] = layer_label(r["layer_id"])
    return {"changes": rows}


@app.post("/api/v1/definitions/history/{hid}/undo")
def definitions_undo(hid: int, u: dict = Depends(ADMIN)):
    ds = _defs()
    try:
        with ds.acting(u["email"], "admin console"):
            r = ds.undo(hid)
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e))
    audit(u, "definitions.undo", str(hid), r)
    return r


class LockIn(BaseModel):
    metric: str
    locked: bool = True


@app.post("/api/v1/definitions/locks")
def definitions_lock(body: LockIn, u: dict = Depends(ADMIN)):
    ds = _defs()
    with ds.acting(u["email"], "admin console"):
        if body.locked:
            ds.put(ds.COMPANY, "lock", body.metric, {"locked": True}, note="locked")
        else:
            ds.remove(ds.COMPANY, "lock", body.metric, note="unlocked")
    audit(u, "definitions.lock", body.metric, {"locked": body.locked})
    return {"metric": body.metric, "locked": body.locked}


@app.post("/api/v1/layers/{lid}/revoke")
def layer_revoke_all(lid: str, u: dict = Depends(ADMIN)):
    _defs()
    from . import sharing
    sharing._db().q("UPDATE shares SET revoked = ? WHERE layer_id = ? AND revoked IS NULL", (core.now(), lid))
    audit(u, "layer.revoke_all", lid)
    return {"ok": True}


@app.get("/api/v1/definitions/export")
def definitions_export(layer: str = "company", u: dict = Depends(ADMIN)):
    """A layer as YAML files in a zip (for Git review or another installation)."""
    import tempfile
    import zipfile
    ds = _defs()
    stack = (ds.COMPANY,) if layer == ds.COMPANY else (ds.COMPANY, layer)
    with tempfile.TemporaryDirectory() as d:
        ds.export(Path(d), stack)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for f in Path(d).glob("*.yaml"):
                z.write(f, f.name)
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="layer-{layer.replace(":", "-")}.zip"'})


# ------------------------------------------------------------------ invite links and My account (public pages)

@app.get("/invite/{token}", include_in_schema=False)
def invite_page(token: str):
    import html as H
    from . import directory
    inv = directory.invite_for(token)
    if inv is None:
        return oauth.page("Invite expired", "<h1>This invite has expired</h1><p>Ask for a new invite.</p>", 404)
    return oauth.page("Join Context Layer", f"""<h1>Join Context Layer</h1><p class="muted">for {H.escape(inv['email'])}</p>
<form method="post"><label for="nm">Your name</label>
<input id="nm" name="name" type="text" required style="width:100%;box-sizing:border-box;padding:9px 10px;border:1px solid #d6d5d0;border-radius:8px;font:inherit">
<label for="p">Choose a password ({core.MIN_PASSWORD}+ characters; a short sentence works well)</label><input id="p" name="password" type="password" autocomplete="new-password" minlength="{core.MIN_PASSWORD}" required>
<button type="submit">Create account</button></form>""")


@app.post("/invite/{token}", include_in_schema=False)
async def invite_accept(token: str, request: Request):
    from . import directory
    form = await request.form()
    try:
        u = directory.accept_invite(token, str(form.get("name", "")).strip(), str(form.get("password", "")))
    except ValueError as e:
        import html as H
        return oauth.page("Try again", f"<h1>Could not create the account</h1><div class=\"err\">{H.escape(str(e))}</div>", 400)
    return oauth.page("Welcome", f"""<h1>You're in</h1><div class="ok">Account created for {u['email']}.</div>
<p>In Claude: Settings, Connectors, Add custom connector, and paste the Context Layer link you were given.
Sign in with this email and password.</p>""")


def _account_user(request: Request):
    return core.session_user(request.cookies.get(oauth.COOKIE))


@app.get("/account", include_in_schema=False)
def account_page(request: Request):
    import html as H
    u = _account_user(request)
    if u is None:
        return oauth.page("Sign in", """<h1>My account</h1><p class="muted">Sign in to Context Layer</p>
<form method="post" action="/account/login"><label for="e">Email</label><input id="e" name="email" type="email" required autofocus>
<label for="p">Password</label><input id="p" name="password" type="password" required><button type="submit">Sign in</button></form>""")
    apps = oauth.grants(u["id"])
    rows = "".join(f"<li>{H.escape(a['app'])} <span class='muted'>since {a['since'][:10]}</span> "
                   f"<form method='post' action='/account/apps/{a['id']}/revoke' style='display:inline'>"
                   f"<button class='secondary' style='margin:0 0 0 8px;padding:2px 10px'>Disconnect</button></form></li>"
                   for a in apps) or "<li class='muted'>None</li>"
    shared = ""
    try:
        from semantic import defstore
        if defstore.enabled():
            from . import sharing
            from .public import base_url
            st = sharing.sharing_status({"kind": "user", "user_id": u["id"], "email": u["email"],
                                         "groups": u.get("groups", [])}, _public_url_base() or base_url(request.scope))
            if st["my_layer"]:
                shared = (f"<h2 style='font-size:16px;margin-top:20px'>My layer</h2><p><code>{H.escape(st['my_layer']['link'])}</code></p>"
                          f"<p class='muted'>Shared with: {H.escape(', '.join(st['my_layer']['shared_with']))}</p>")
            if st["shared_with_me"]:
                shared += "<h2 style='font-size:16px;margin-top:20px'>Shared with me</h2><ul>" + "".join(
                    f"<li>{H.escape(x['layer'])}{' (in use)' if x['in_use'] else ''}</li>" for x in st["shared_with_me"]) + "</ul>"
    except Exception:                                      # noqa: BLE001 - optional section
        pass
    must = "<div class='err'>You are using a temporary password: choose your own below before using Context Layer.</div>" \
        if u.get("must_change") else ""
    return oauth.page("My account", f"""<h1>{H.escape(u['name'])}</h1><p class="muted">{H.escape(u['email'])} · {H.escape(u['role'])}</p>{must}
<h2 style="font-size:16px;margin-top:20px">Connected apps</h2><ul>{rows}</ul>{shared}
<h2 style="font-size:16px;margin-top:20px">Change password</h2>
<form method="post" action="/account/password"><label for="o">Current password</label><input id="o" name="old" type="password" required>
<label for="n">New password ({core.MIN_PASSWORD}+ characters)</label><input id="n" name="new" type="password" minlength="{core.MIN_PASSWORD}" required>
<button type="submit">Change password</button></form>""")


@app.post("/account/login", include_in_schema=False)
async def account_login(request: Request):
    form = await request.form()
    if oauth._limited("login:" + oauth._ip(request), 8):
        return oauth.page("Wait", "<h1>Too many attempts</h1><p>Wait a few minutes.</p>", 429)
    r = core.login(str(form.get("email", "")), str(form.get("password", "")))
    if r is None:
        return oauth.page("Sign in", "<h1>My account</h1><div class='err'>Email or password is not correct.</div>"
                                     "<p><a href='/account'>Try again</a></p>", 401)
    from fastapi.responses import RedirectResponse
    from .public import base_url
    resp = RedirectResponse("/account", 303)
    resp.set_cookie(oauth.COOKIE, r[1], max_age=core.SESSION_DAYS * 86400, httponly=True, samesite="lax",
                    secure=base_url(request.scope).startswith("https://"), path="/")
    return resp


@app.post("/account/password", include_in_schema=False)
async def account_password(request: Request):
    u = _account_user(request)
    if u is None:
        return oauth.page("Sign in", "<h1>Signed out</h1><p><a href='/account'>Sign in again</a></p>", 401)
    form = await request.form()
    row = core.db().execute("SELECT pw_hash FROM users WHERE id = ?", (u["id"],)).fetchone()
    if not core.check_password(str(form.get("old", "")), row["pw_hash"]):
        return oauth.page("Try again", "<h1>Change password</h1><div class='err'>The current password is not correct.</div>"
                                       "<p><a href='/account'>Back</a></p>", 400)
    try:
        core.set_password(u["id"], str(form.get("new", "")))
    except ValueError as e:
        return oauth.page("Try again", f"<h1>Change password</h1><div class='err'>{e}</div><p><a href='/account'>Back</a></p>", 400)
    core.log_event("user", u["email"], "account.password", "")
    return oauth.page("Done", "<h1>Password changed</h1><p><a href='/account'>Back to my account</a></p>")


@app.post("/account/apps/{gid}/revoke", include_in_schema=False)
def account_disconnect(gid: str, request: Request):
    u = _account_user(request)
    if u is None:
        return oauth.page("Sign in", "<h1>Signed out</h1><p><a href='/account'>Sign in again</a></p>", 401)
    oauth.revoke_grant(gid, u["id"])
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/account", 303)


@app.patch("/api/v1/users/{uid}")
def update_user(uid: int, body: UserPatch, u: dict = Depends(OWNER)):
    try:
        nu = core.update_user(uid, body.role, body.disabled, body.groups)
    except KeyError:
        raise HTTPException(404, "No such user.")
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit(u, "users.update", nu["email"], {"role": nu["role"], "disabled": nu["disabled"], "groups": nu["groups"]})
    return {"user": nu}


# ------------------------------------------------------------------ apps signed in from Claude / ChatGPT

@app.get("/api/v1/connected-apps")
def connected_apps(u: dict = Depends(ANY)):
    return {"apps": oauth.grants(None if u["role"] in ("owner", "admin") else u["id"])}


@app.post("/api/v1/connected-apps/{gid}/revoke")
def disconnect_app(gid: str, u: dict = Depends(ANY)):
    if not oauth.revoke_grant(gid, None if u["role"] in ("owner", "admin") else u["id"]):
        raise HTTPException(404, "No such connected app.")
    audit(u, "oauth.revoke", gid)
    return {"ok": True}


# ------------------------------------------------------------------ one-time upload links (create_upload_link)

def _link(token: str):
    r = core.db().execute("SELECT * FROM upload_links WHERE token_hash = ? AND expires > ?",
                          (core.token_hash(token), core.now())).fetchone()
    return r


UPLOAD_FORM = """<h1>Add files</h1><p class="muted">They are added to Context Layer for <b>{who}</b>, visible to: {groups}.
This page works until {until} UTC.</p>{msg}
<form method="post" enctype="multipart/form-data"><label for="f">Files (PDF, Word, PowerPoint, Excel, CSV, JSON, text, HTML, images)</label>
<input id="f" type="file" name="files" multiple required><button type="submit">Upload</button></form>"""


@app.get("/upload/{token}", include_in_schema=False)
def upload_page(token: str):
    r = _link(token)
    if r is None:
        return oauth.page("Link expired", "<h1>This link has expired</h1><p>Ask your AI app for a new upload link.</p>", 404)
    import html as H
    return oauth.page("Add files", UPLOAD_FORM.format(who=H.escape(r["principal"]), groups=H.escape(", ".join(json.loads(r["groups"]))),
                                                      until=r["expires"][:16].replace("T", " "), msg=""))


@app.post("/upload/{token}", include_in_schema=False)
async def upload_files(token: str, request: Request, files: list[UploadFile] = File(...)):
    import html as H
    r = _link(token)
    if r is None:
        return oauth.page("Link expired", "<h1>This link has expired</h1><p>Ask your AI app for a new upload link.</p>", 404)
    if r["files"] + len(files) > 50:
        return oauth.page("Too many files", "<h1>Too many files</h1><p>One link takes up to 50 files.</p>", 400)
    saved, errors = [], []
    for f in files:
        data = await f.read()
        if len(data) > MAX_UPLOAD_MB * 2 ** 20:
            errors.append(f"{f.filename}: larger than {MAX_UPLOAD_MB} MB")
            continue
        try:
            saved.append(engine.ingest_upload(f.filename or "file", data, json.loads(r["groups"])))
        except ValueError as e:
            errors.append(str(e))
    core.db().execute("UPDATE upload_links SET files = files + ? WHERE token_hash = ?", (len(saved), core.token_hash(token)))
    err = "".join(f'<div class="err">{H.escape(e)}</div>' for e in errors)
    if not saved:
        return oauth.page("Add files", UPLOAD_FORM.format(who=H.escape(r["principal"]), groups=H.escape(", ".join(json.loads(r["groups"]))),
                                                          until=r["expires"][:16].replace("T", " "), msg=err), 400)
    job = engine.queue_rebuild(saved, r["principal"])
    core.log_event("agent", r["principal"], "upload_link.files", ", ".join(s["file"] for s in saved), {"job": job["id"]})
    names = "".join(f"<li>{H.escape(s['file'])} ({s['kind']})</li>" for s in saved)
    return oauth.page("Uploaded", f"""<h1>Got it</h1><div class="ok">Added {len(saved)} file(s). Context Layer is building them now.</div>
<ul>{names}</ul>{err}<p>Go back to your chat and say <b>"check the upload"</b>. Job id: <code>{job['id']}</code></p>""")


# ------------------------------------------------------------------ settings

class GraphModeIn(BaseModel):
    mode: str


@app.get("/api/v1/settings")
def settings(request: Request, u: dict = Depends(ANY)):
    from graph.config import PG_DSN
    pg_ok, pg_msg = engine.postgres_ok()
    return {
        "organisation": core.get_setting("organisation", ""),
        "ai_model": {"inside": engine.graph_mode() == "ai",
                     "note": "Agents over MCP: Context Layer finds the evidence (passages, numbers, graph links) and "
                     "the agent writes the answer. The Chat page does the same with Claude, on each person's own "
                     "Claude API key (or the company key, if allowed below)." + (
                         " The knowledge graph is built with AI extraction (Settings below)."
                         if engine.graph_mode() == "ai" else "")},
        "mcp": {"url": str(request.base_url).rstrip("/") + "/mcp", "public_url": _public_url(),
                "transport": "Streamable HTTP",
                "auth": "Sign in from Claude or ChatGPT (OAuth), or Authorization: Bearer <agent key>"},
        "models": {"embeddings": "BAAI/bge-m3 (local)", "keyword search": "BM25 (local)",
                   "re-ranker": "BAAI/bge-reranker-v2-m3 (local)",
                   "entities": ("GLiNER urchade/gliner_medium-v2.1 + spaCy en_core_web_lg (local)"
                                if engine.graph_mode() != "ai" else "AI extraction (see below)")},
        "graph_mode": engine.graph_mode(),
        "ai_extraction": _ai_settings(),
        "storage": {"data_folder": str(core.DATA_DIR), "graph_database": PG_DSN.split("password=")[0].strip(),
                    "graph_database_ok": pg_ok, "graph_database_message": pg_msg, "disk": engine.disk_usage()},
        "version": core.VERSION,
    }


def _public_url():
    from .public import public_mcp_url
    return public_mcp_url()


@app.put("/api/v1/settings/graph")
def set_graph_mode(body: GraphModeIn, u: dict = Depends(OWNER)):
    if body.mode not in engine.GRAPH_MODES:
        raise HTTPException(400, "mode is fast, accurate or ai")
    if body.mode == "ai":
        from graph.extract_ai import problems
        bad = problems()
        if bad:
            raise HTTPException(400, "Set up AI extraction first: " + " ".join(bad))
    core.set_setting("graph_mode", body.mode)
    audit(u, "settings.graph_mode", body.mode)
    return {"ok": True}


def _ai_settings() -> dict:
    from graph.extract_ai import PROVIDERS, config, destination, is_local, problems
    c = config()
    usage = None
    f = engine.OUT_DIR / "gr_ai_usage.json"
    if f.exists():
        try:
            usage = json.loads(f.read_text(encoding="utf-8"))
            usage.pop("errors", None)
        except ValueError:
            usage = None
    return {"provider": c["provider"], "model": c["model"], "base_url": c["base_url"], "workers": c["workers"],
            "key_set": bool(c["api_key"]), "key": core.mask(c["api_key"]), "destination": destination(c),
            "local": is_local(c), "ready": not problems(c), "problems": problems(c), "last_build": usage,
            "providers": {k: {kk: v[kk] for kk in ("label", "base_url", "model", "needs_key", "workers")}
                          for k, v in PROVIDERS.items()}}


class AIExtractIn(BaseModel):
    provider: str
    model: str = ""
    base_url: str = ""
    workers: int | None = None
    api_key: str | None = None        # None: keep the saved key; "": remove it


@app.put("/api/v1/settings/ai")
def set_ai_extraction(body: AIExtractIn, u: dict = Depends(OWNER)):
    from graph.extract_ai import KEY_ENV, PROVIDERS
    if body.provider not in PROVIDERS:
        raise HTTPException(400, "provider is anthropic, openai or local")
    base = (body.base_url or PROVIDERS[body.provider]["base_url"]).strip().rstrip("/")
    if not re.match(r"^https?://[^\s]+$", base):
        raise HTTPException(400, "The address must start with http:// or https://")
    model = body.model.strip()[:120] or PROVIDERS[body.provider]["model"]
    workers = max(1, min(32, body.workers or PROVIDERS[body.provider]["workers"]))
    core.set_setting("ai_extract", {"provider": body.provider, "model": model, "base_url": base, "workers": workers})
    if body.api_key is not None:
        core.save_secret(KEY_ENV, body.api_key.strip() or None)
    audit(u, "settings.ai_extraction", f"{body.provider} {model} at {base}"
          + ("" if body.api_key is None else (" (key changed)" if body.api_key.strip() else " (key removed)")))
    return {"ok": True, "ai_extraction": _ai_settings()}


@app.post("/api/v1/settings/ai/test")
def test_ai_extraction(u: dict = Depends(OWNER)):
    from graph.extract_ai import test_connection
    r = test_connection()
    audit(u, "settings.ai_extraction.test", "ok" if r["ok"] else r["message"][:120])
    return r


class OrgIn(BaseModel):
    organisation: str


@app.put("/api/v1/settings/organisation")
def set_org(body: OrgIn, u: dict = Depends(OWNER)):
    core.set_setting("organisation", body.organisation.strip()[:80])
    audit(u, "settings.organisation", body.organisation[:80])
    try:
        M.describe_org()
    except Exception:                                      # noqa: BLE001
        pass
    return {"ok": True}


@app.get("/api/v1/health")
def health():
    return {"ok": True, "version": core.VERSION}


# ------------------------------------------------------------------ web app (built React files) and MCP

if (WEB / "assets").exists():
    app.mount("/assets", StaticFiles(directory=WEB / "assets"), name="assets")


@app.get("/{path:path}", include_in_schema=False)
def spa(path: str):
    if path.startswith(("api/", "mcp")):
        raise HTTPException(404)
    f = WEB / path
    if path and f.is_file() and WEB in f.resolve().parents:
        return FileResponse(f)
    index = WEB / "index.html"
    if not index.exists():
        return JSONResponse({"detail": "The web app is not built. Run: cd web && npm install && npm run build"}, 503)
    return FileResponse(index, headers={"Cache-Control": "no-cache"})


def _request_workspace(scope) -> tuple[str | None, bool]:
    """(workspace id, look across workspaces?) of one request: the signed-in session's, the bearer token's, or the
    one a webhook / upload link / invite link belongs to. OAuth sign-in pages work with credentials only and look
    across workspaces. Anything else runs in the first workspace (and sees nothing without signing in)."""
    from . import db as D
    path = scope.get("path") or ""
    if path.startswith(("/oauth/", "/.well-known/")):
        return None, True
    headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
    tok = M._bearer(headers)
    if tok:
        a = core.principal_for_token(tok)
        return (a or {}).get("tenant_id"), False
    m = re.match(r"^/hooks/[a-z0-9_]+/([A-Za-z0-9_]+)$", path)
    if m:
        from sources import manage
        with D.system():
            r = manage.db().execute("SELECT tenant_id FROM sources WHERE id = ?", (m.group(1),)).fetchone() \
                if D.postgres() else None
        return (r["tenant_id"] if r else None), False
    m = re.match(r"^/(upload|invite)/([A-Za-z0-9_\-]+)$", path)
    if m and D.postgres():
        table = "upload_links" if m.group(1) == "upload" else "invites"
        if table == "invites":
            from . import directory
            directory.db()
        with D.system():
            r = core.db().execute(f"SELECT tenant_id FROM {table} WHERE token_hash = ?",
                                  (core.token_hash(m.group(2)),)).fetchone()
        return (r["tenant_id"] if r else None), False
    cookie = ""
    for part in headers.get("cookie", "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == COOKIE:
            cookie = v
    if cookie:
        u = core.session_user(cookie)
        return (u or {}).get("tenant_id"), False
    return None, False


class _Router:
    """/mcp goes to the MCP server; everything else to the FastAPI app. Each request runs in its workspace."""

    def __init__(self, api):
        self.api = api

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self._route(scope, receive, send)
        from starlette.concurrency import run_in_threadpool
        from . import db as D
        tid, bypass = await run_in_threadpool(_request_workspace, scope)
        t1, t2 = D.TENANT.set(tid), D.BYPASS.set(bypass)
        try:
            return await self._route(scope, receive, send)
        finally:
            D.TENANT.reset(t1)
            D.BYPASS.reset(t2)

    async def _route(self, scope, receive, send):
        if scope["type"] == "http":
            from .public import allowed
            ok, status, msg = allowed(scope)
            if not ok:
                return await JSONResponse({"detail": msg}, status)(scope, receive, send)
        if scope["type"] == "http" and (scope["path"] == "/mcp" or scope["path"].startswith("/mcp/")):
            m = re.match(r"^/mcp/(layers|teams)/([a-z0-9-]{1,80})/?$", scope["path"])
            if m:                                 # a shared layer's or a team's own link: same server, that layer added
                scope = dict(scope, path="/mcp", raw_path=b"/mcp", cl_layer_link=m.group(2),
                             cl_resource_path=f"/mcp/{m.group(1)}/{m.group(2)}")
            if _mcp_asgi is None:
                return await JSONResponse({"detail": "Starting up"}, 503)(scope, receive, send)
            return await _mcp_asgi(scope, receive, send)
        return await self.api(scope, receive, send)


asgi = _Router(app)


def serve(host: str = "127.0.0.1", port: int = 8080):
    import uvicorn
    print(f"Context Layer {core.VERSION}: http://{host}:{port}   (agents connect to http://{host}:{port}/mcp)")
    uvicorn.run(asgi, host=host, port=port, log_level="warning")
