"""Context Layer server: the web app (/), its API (/api/v1) and the MCP endpoint for agents (/mcp).

Run:  python -m app            (or: uvicorn app.main:app)
"""
import contextlib
import csv
import io
import json
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import core, engine, jobs, oauth
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
    async with M.mcp.session_manager.run():
        yield


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


ANY = role("owner", "admin", "viewer")
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
            "organisation": core.get_setting("organisation", "")}


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


@app.post("/api/v1/login")
def login(body: LoginIn, request: Request, response: Response):
    _rate_limit(f"login:{request.client.host if request.client else '?'}")
    res = core.login(body.email, body.password)
    if res is None:
        core.log_event("user", body.email.strip().lower()[:200], "login", "", ok=False)
        raise HTTPException(401, "Email or password is not correct.")
    u, token = res
    _set_session(response, token, request)
    audit(u, "login")
    return {"user": u}


@app.post("/api/v1/logout")
def logout(request: Request, response: Response):
    core.logout(request.cookies.get(COOKIE))
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


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
    return {"overview": engine.graph_overview(), **engine.graph_explore(q.strip()[:100])}


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


class AgentPatch(BaseModel):
    groups: list[str] | None = None
    capabilities: list[str] | None = None


@app.get("/api/v1/agents")
def agents(u: dict = Depends(ANY)):
    return {"agents": core.list_agents(), "groups": engine.all_groups(), "capabilities": list(core.CAPABILITIES),
            "tools": [{"name": t.name, "description": t.description} for t in M.mcp._tool_manager.list_tools()]}


@app.post("/api/v1/agents")
def create_agent(body: AgentIn, u: dict = Depends(ADMIN)):
    try:
        groups = engine.clean_groups(body.groups)
        a, token = core.create_agent(body.name, body.description, groups, body.capabilities, u["email"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit(u, "agents.create", a["name"], {"groups": a["groups"], "capabilities": a["capabilities"]})
    return {"agent": a, "token": token}


@app.patch("/api/v1/agents/{aid}")
def update_agent(aid: str, body: AgentPatch, u: dict = Depends(ADMIN)):
    try:
        groups = engine.clean_groups(body.groups) if body.groups is not None else None
        a = core.update_agent(aid, groups, body.capabilities)
    except KeyError:
        raise HTTPException(404, "No such agent.")
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit(u, "agents.update", a["name"], {"groups": a["groups"], "capabilities": a["capabilities"]})
    return {"agent": a}


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
        out = engine.ask(body.question, groups, caps, mode)
    except (ValueError, PermissionError, RuntimeError) as e:
        core.log_event("user", u["email"], "playground.ask", body.question[:200], {"who": who, "error": str(e)}, False, t())
        raise HTTPException(400, str(e))
    core.log_event("user", u["email"], "playground.ask", body.question[:200], {"who": who, "route": out["route"]}, True, t())
    return out | {"asked": who}


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
    return {"suites": engine.quality()}


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
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit(u, "users.add", nu["email"], {"role": nu["role"]})
    return {"user": nu}


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
        "ai_model": {"inside": False, "note": "Context Layer runs no AI model. It finds the evidence (passages, "
                     "numbers, graph links); the agent that connects over MCP writes the answer."},
        "mcp": {"url": str(request.base_url).rstrip("/") + "/mcp", "public_url": _public_url(),
                "transport": "Streamable HTTP",
                "auth": "Sign in from Claude or ChatGPT (OAuth), or Authorization: Bearer <agent key>"},
        "models": {"embeddings": "BAAI/bge-m3 (local)", "keyword search": "BM25 (local)",
                   "re-ranker": "BAAI/bge-reranker-v2-m3 (local)",
                   "entities": "GLiNER urchade/gliner_medium-v2.1 + spaCy en_core_web_lg (local)"},
        "graph_mode": engine.graph_mode(),
        "storage": {"data_folder": str(core.DATA_DIR), "graph_database": PG_DSN.split("password=")[0].strip(),
                    "graph_database_ok": pg_ok, "graph_database_message": pg_msg, "disk": engine.disk_usage()},
        "version": core.VERSION,
    }


def _public_url():
    from .public import public_mcp_url
    return public_mcp_url()


@app.put("/api/v1/settings/graph")
def set_graph_mode(body: GraphModeIn, u: dict = Depends(OWNER)):
    if body.mode not in ("fast", "accurate"):
        raise HTTPException(400, "mode is fast or accurate")
    core.set_setting("graph_mode", body.mode)
    audit(u, "settings.graph_mode", body.mode)
    return {"ok": True}


class OrgIn(BaseModel):
    organisation: str


@app.put("/api/v1/settings/organisation")
def set_org(body: OrgIn, u: dict = Depends(OWNER)):
    core.set_setting("organisation", body.organisation.strip()[:80])
    audit(u, "settings.organisation", body.organisation[:80])
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


class _Router:
    """/mcp goes to the MCP server; everything else to the FastAPI app."""

    def __init__(self, api):
        self.api = api

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            from .public import allowed
            ok, status, msg = allowed(scope)
            if not ok:
                return await JSONResponse({"detail": msg}, status)(scope, receive, send)
        if scope["type"] == "http" and (scope["path"] == "/mcp" or scope["path"].startswith("/mcp/")):
            if _mcp_asgi is None:
                return await JSONResponse({"detail": "Starting up"}, 503)(scope, receive, send)
            return await _mcp_asgi(scope, receive, send)
        return await self.api(scope, receive, send)


asgi = _Router(app)


def serve(host: str = "127.0.0.1", port: int = 8080):
    import uvicorn
    print(f"Context Layer {core.VERSION}: http://{host}:{port}   (agents connect to http://{host}:{port}/mcp)")
    uvicorn.run(asgi, host=host, port=port, log_level="warning")
