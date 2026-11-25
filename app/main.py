"""Context Layer server: the web app (/), its API (/api/v1) and the MCP endpoint for agents (/mcp).

Run:  python -m app            (or: uvicorn app.main:app)
"""
import contextlib
import csv
import io
import json
import os
import re
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import core, engine, jobs, oauth
from . import db as D
from . import mcp_server as M

core.load_secrets()
from connectors import manage as connectors   # noqa: E402  (registers the sync job and the scheduler hook)
connectors.install()
WEB = core.ROOT / "web" / "dist"
COOKIE = "cl_session"
MAX_UPLOAD_MB = 200
_mcp_asgi = None


@contextlib.asynccontextmanager
async def lifespan(app):
    # a fresh MCP app (and session manager) per server start: a manager can only be run once
    global _mcp_asgi
    _mcp_asgi = M.http_app()
    if os.environ.get("CL_WORKERS", "2") != "0":
        jobs.start_workers()
    from . import scheduler
    scheduler.start()
    try:
        async with M.mcp.session_manager.run():
            yield
    finally:
        scheduler.stop()
        jobs.stop_workers()


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


@app.middleware("http")
async def workspace_scope(request: Request, call_next):
    """Every web request runs in the workspace of the signed-in session (row-level security in Postgres keeps
    other workspaces' rows out). /mcp and upload links resolve their workspace from their own token."""
    tid = None
    tok = request.cookies.get(COOKIE)
    if tok and not request.url.path.startswith("/mcp"):
        u = await run_in_threadpool(core.session_user, tok)
        tid = u["tenant_id"] if u else None
    reset = D.TENANT.set(tid)
    try:
        return await call_next(request)
    finally:
        D.TENANT.reset(reset)


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
    workspace: str | None = None


def signup_mode() -> str:
    """CL_SIGNUP: open (anyone can create a workspace), invite (default: owners add people) or off."""
    m = os.environ.get("CL_SIGNUP", "invite").strip().lower()
    return m if m in ("open", "invite", "off") else "invite"


@app.get("/api/v1/setup")
def setup_state():
    return {"needs_setup": core.user_count() == 0, "version": core.VERSION, "signup": signup_mode(),
            "organisation": core.get_setting("organisation", "")}


@app.post("/api/v1/setup")
def setup(body: SetupIn, request: Request, response: Response):
    if core.user_count() > 0:
        raise HTTPException(409, "Context Layer is already set up. Sign in instead.")
    return _new_workspace(body, request, response, "setup")


def _new_workspace(body: SetupIn, request: Request, response: Response, action: str) -> dict:
    try:
        u = core.signup(body.email, body.name, body.password, body.organisation)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _, token = core.login(body.email, body.password, u["tenant_id"])
    _set_session(response, token, request)
    with core.tenant_scope(u["tenant_id"]):
        audit(u, action, body.organisation)
        if action == "setup":
            _seed_layer(u["email"])
        else:
            from .seed import seed_new_workspace
            seed_new_workspace(u["email"])
    return {"user": u}


def _seed_layer(by: str):
    """The server's first workspace starts with the shipped semantic layer and its tables (app/seed.py)."""
    from .seed import seed_first_workspace
    try:
        n = seed_first_workspace(by)
        if n:
            core.log_event("system", by, "semantic.import", "semantic_layer/", n)
    except Exception as e:                                   # noqa: BLE001 - a bad seed must not block setup
        core.log_event("system", by, "semantic.import", "semantic_layer/", {"error": str(e)[:300]}, ok=False)


@app.post("/api/v1/signup")
def signup(body: SetupIn, request: Request, response: Response):
    """Create a new workspace (CL_SIGNUP=open). The person becomes its owner."""
    if signup_mode() != "open":
        raise HTTPException(403, "Sign-up is closed on this server. Ask a workspace owner to add you.")
    _rate_limit(f"signup:{request.client.host if request.client else '?'}", 5, 3600)
    return _new_workspace(body, request, response, "signup")


@app.post("/api/v1/login")
def login(body: LoginIn, request: Request, response: Response):
    _rate_limit(f"login:{request.client.host if request.client else '?'}")
    res = core.login(body.email, body.password, body.workspace)
    if res is None:
        core.log_event("user", body.email.strip().lower()[:200], "login", "", ok=False)
        raise HTTPException(401, "Email or password is not correct.")
    u, token = res
    _set_session(response, token, request)
    with core.tenant_scope(u["tenant_id"]):
        audit(u, "login")
    return {"user": u}


@app.post("/api/v1/logout")
def logout(request: Request, response: Response):
    core.logout(request.cookies.get(COOKIE))
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@app.get("/api/v1/me")
def me(u: dict = Depends(current_user)):
    return {"user": u, "organisation": core.get_setting("organisation", ""), "version": core.VERSION,
            "workspace": core.get_tenant(u["tenant_id"]), "workspaces": core.user_tenants(u["id"])}


class SwitchIn(BaseModel):
    workspace: str


@app.post("/api/v1/workspaces/switch")
def switch_workspace(body: SwitchIn, request: Request, u: dict = Depends(current_user)):
    try:
        nu = core.switch_tenant(request.cookies.get(COOKIE), body.workspace)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    with core.tenant_scope(nu["tenant_id"]):
        audit(nu, "workspace.switch", body.workspace)
    return {"user": nu}


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
            "problems": problems, "job": jobs.running_mine()}


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


class LayerImportIn(BaseModel):
    files: dict[str, str]
    certified: bool = True


@app.get("/api/v1/semantic/export")
def semantic_export(u: dict = Depends(ADMIN)):
    """The workspace's semantic layer as YAML files (for review in git, or to move it to another workspace)."""
    from semantic import repo
    audit(u, "semantic.export")
    return {"files": repo.export_texts()}


@app.post("/api/v1/semantic/import")
def semantic_import(body: LayerImportIn, u: dict = Depends(ADMIN)):
    """Import YAML layer files. certified=true: the definitions were reviewed elsewhere (dbt, git) and keep their
    status; otherwise new metrics arrive as drafts."""
    from semantic import repo
    allowed = {"model.yaml", "metrics.yaml", "enrichment.yaml", "verified_queries.yaml", "instructions.md"}
    if not body.files or not set(body.files) <= allowed:
        raise HTTPException(400, f"Send files named {', '.join(sorted(allowed))}.")
    try:
        n = repo.import_texts(body.files, source="import", by=u["email"], certified=body.certified)
    except Exception as e:                                       # noqa: BLE001 - validation errors are shown
        raise HTTPException(400, f"Not imported: {str(e)[:400]}")
    audit(u, "semantic.import", ", ".join(sorted(body.files)), n)
    return {"imported": n}


# ------------------------------------------------------------------ connectors (sources kept up to date on their own)

class ConnectorIn(BaseModel):
    kind: str
    name: str = ""
    config: dict = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)
    groups: list[str] | None = None
    every_minutes: int = 360
    sync_now: bool = True


class ConnectorPatch(BaseModel):
    config: dict | None = None
    secrets: dict[str, str] | None = None
    groups: list[str] | None = None
    every_minutes: int | None = None
    status: str | None = None


@app.get("/api/v1/connectors")
def list_connectors(u: dict = Depends(ANY)):
    return {"connectors": connectors.list_all(), "kinds": connectors.catalog()}


@app.post("/api/v1/connectors")
def add_connector(body: ConnectorIn, u: dict = Depends(ADMIN)):
    try:
        c = connectors.create(body.kind, body.name, body.config, body.secrets, body.groups, body.every_minutes, u["email"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit(u, "connector.add", c["name"], {"kind": c["kind"], "config": c["config"], "groups": c["groups"]})
    job = connectors.queue_sync(c["id"], u["email"]) if body.sync_now else None
    return {"connector": c, "job": job}


@app.patch("/api/v1/connectors/{cid}")
def change_connector(cid: str, body: ConnectorPatch, u: dict = Depends(ADMIN)):
    try:
        c = connectors.update(cid, body.config, body.secrets, body.groups, body.every_minutes, body.status)
    except KeyError:
        raise HTTPException(404, "No such connector.")
    except ValueError as e:
        raise HTTPException(400, str(e))
    changed = [k for k, v in body.model_dump().items() if v is not None and k != "secrets"]
    audit(u, "connector.update", c["name"], {"changed": changed + (["secrets"] if body.secrets else [])})
    return {"connector": c}


@app.delete("/api/v1/connectors/{cid}")
def remove_connector(cid: str, u: dict = Depends(ADMIN)):
    try:
        out = connectors.delete(cid, u["email"])
    except KeyError:
        raise HTTPException(404, "No such connector.")
    audit(u, "connector.delete", cid, out)
    return out


@app.post("/api/v1/connectors/{cid}/test")
def test_connector(cid: str, u: dict = Depends(ADMIN)):
    try:
        return connectors.test(cid)
    except KeyError:
        raise HTTPException(404, "No such connector.")
    except Exception as e:                                   # noqa: BLE001 - shown to the admin
        return {"ok": False, "detail": f"{type(e).__name__}: {str(e)[:300]}"}


@app.post("/api/v1/connectors/{cid}/sync")
def sync_connector(cid: str, u: dict = Depends(ADMIN)):
    try:
        job = connectors.queue_sync(cid, u["email"])
    except KeyError:
        raise HTTPException(404, "No such connector.")
    audit(u, "connector.sync", cid)
    return {"job": job}


@app.post("/hooks/{kind}/{cid}", include_in_schema=False)
async def connector_webhook(kind: str, cid: str, request: Request):
    """Webhooks from sources (GitHub push / pull request / issues): signed with the connector's webhook secret."""
    body = await request.body()
    if len(body) > 5 * 2 ** 20:
        return JSONResponse({"detail": "too large"}, 413)
    status, msg = await run_in_threadpool(connectors.webhook, kind, cid, body,
                                          request.headers.get("x-hub-signature-256", ""),
                                          request.headers.get("x-github-event", ""))
    return JSONResponse({"detail": msg}, status)


# ------------------------------------------------------------------ knowledge-graph ontology (per workspace)

class OntologyIn(BaseModel):
    yaml: str
    certified: bool = True


@app.get("/api/v1/ontology")
def get_ontology(u: dict = Depends(ANY)):
    """The approved ontology (schema.yaml format) and every definition with its status and source."""
    from graph import ontology
    return {"ontology": ontology.export_schema(),
            "definitions": [{k: r[k] for k in ("kind", "name", "definition", "status", "source", "note", "version",
                                               "updated_by", "updated_at")} for r in ontology.rows()]}


@app.get("/api/v1/ontology/export.yaml")
def export_ontology(u: dict = Depends(ADMIN)):
    import yaml
    from graph import ontology
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(yaml.safe_dump(ontology.export_schema(), sort_keys=False, allow_unicode=True),
                             media_type="application/x-yaml")


def _ontology_changed(before: str, by: str):
    """An approved change re-extracts the graph (only passages extracted with the old ontology)."""
    from graph import ontology
    if ontology.fingerprint() != before and engine.graph_ready():
        jobs.enqueue("graph", "Building the knowledge graph", None, by, {"reason": "ontology changed"})


@app.put("/api/v1/ontology")
def import_ontology(body: OntologyIn, u: dict = Depends(ADMIN)):
    """Import an ontology in the graph_layer/schema.yaml format (adds and updates; nothing is removed)."""
    import yaml
    from graph import ontology
    try:
        data = yaml.safe_load(body.yaml) or {}
        assert isinstance(data, dict)
        before = ontology.fingerprint()
        n = ontology.import_schema(data, source="import", by=u["email"], certified=body.certified)
    except Exception as e:                                       # noqa: BLE001 - shown to the admin
        raise HTTPException(400, f"Not imported: {str(e)[:300]}")
    audit(u, "ontology.import", "", n)
    _ontology_changed(before, u["email"])
    return {"imported": n}


class DefinitionStatusIn(BaseModel):
    status: str


@app.post("/api/v1/ontology/{kind}/{name}/status")
def set_ontology_status(kind: str, name: str, body: DefinitionStatusIn, u: dict = Depends(ADMIN)):
    """Approve, send back to draft, or retire an entity type, relation type or rule (e.g. a proposed type)."""
    from graph import ontology
    if kind not in ("entity_type", "relation_type", "rule") or body.status not in ("draft", "approved", "needs_review",
                                                                                  "retired"):
        raise HTTPException(400, "Unknown kind or status.")
    before = ontology.fingerprint()
    try:
        r = ontology.set_status(kind, name, body.status, u["email"])
    except KeyError:
        raise HTTPException(404, "No such definition.")
    audit(u, "ontology.status", f"{kind} {name}", {"status": body.status})
    _ontology_changed(before, u["email"])
    return {"definition": r}


@app.put("/api/v1/semantic/{kind}/{name}/status")
def set_definition_status(kind: str, name: str, body: DefinitionStatusIn, u: dict = Depends(ADMIN)):
    """Approve, send back to draft, or retire any definition (e.g. a suggested synonym)."""
    from semantic import repo
    if kind not in repo.KINDS or body.status not in repo.STATUSES:
        raise HTTPException(400, "Unknown kind or status.")
    try:
        r = repo.set_status(kind, name, body.status, u["email"])
    except KeyError:
        raise HTTPException(404, "No such definition.")
    audit(u, "semantic.status", f"{kind} {name}", {"status": body.status})
    return {"definition": r}


@app.get("/api/v1/semantic/drafts")
def semantic_drafts(u: dict = Depends(ANY)):
    """Everything waiting for a person: drafts and definitions a check took out (needs_review)."""
    from semantic import repo
    return {"items": [{k: r[k] for k in ("kind", "name", "status", "source", "note", "definition", "updated_at")}
                      for kind in repo.KINDS for r in repo.rows(kind, ("draft", "needs_review"))]}


@app.get("/api/v1/semantic/history")
def semantic_history(kind: str, name: str, u: dict = Depends(ANY)):
    from semantic import repo
    if kind not in repo.KINDS:
        raise HTTPException(400, "Unknown kind.")
    return {"history": repo.history(kind, name), "current": repo.get(kind, name)}


@app.get("/api/v1/graph")
def graph(q: str = "", u: dict = Depends(ANY)):
    groups = None if u["role"] in ("owner", "admin") else u["groups"]          # viewers: only their groups
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
    return {"jobs": jobs.list_jobs(), "running": jobs.running_mine()}


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
    scope: str = "agent"
    expires: str | None = None
    allowed_ips: list[str] = Field(default_factory=list)


class AgentPatch(BaseModel):
    groups: list[str] | None = None
    capabilities: list[str] | None = None
    expires: str | None = None
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
                                     body.scope, body.expires, body.allowed_ips)
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit(u, "agents.create", a["name"], {"groups": a["groups"], "capabilities": a["capabilities"],
                                           "scope": a["scope"]})
    return {"agent": a, "token": token}


@app.patch("/api/v1/agents/{aid}")
def update_agent(aid: str, body: AgentPatch, u: dict = Depends(ADMIN)):
    try:
        groups = engine.clean_groups(body.groups) if body.groups is not None else None
        a = core.update_agent(aid, groups, body.capabilities, body.expires, body.allowed_ips)
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


# ------------------------------------------------------------------ an app's end users (delegate keys)

class ExternalUserIn(BaseModel):
    groups: list[str]
    name: str = ""
    disabled: bool = False


@app.get("/api/v1/external-users")
def external_users(u: dict = Depends(ADMIN)):
    from . import delegate
    return {"users": delegate.list_users()}


@app.put("/api/v1/external-users/{user_id}")
def put_external_user(user_id: str, body: ExternalUserIn, u: dict = Depends(ADMIN)):
    """The groups an app's end user may see when the app calls with its delegate key on their behalf."""
    from . import delegate
    try:
        r = delegate.put_user(user_id, body.groups, body.name, body.disabled)
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit(u, "external_user.put", user_id, {"groups": r["groups"], "disabled": r["disabled"]})
    return {"user": r}


@app.delete("/api/v1/external-users/{user_id}")
def delete_external_user(user_id: str, u: dict = Depends(ADMIN)):
    from . import delegate
    if not delegate.delete_user(user_id):
        raise HTTPException(404, "No such user.")
    audit(u, "external_user.delete", user_id)
    return {"ok": True}


class PrivacyIn(BaseModel):
    event_retention_days: int = 90
    redact_questions: bool = False


@app.put("/api/v1/settings/privacy")
def set_privacy(body: PrivacyIn, u: dict = Depends(OWNER)):
    """How long the activity log is kept, and whether agents' questions are stored as text or only as a hash."""
    if not 1 <= body.event_retention_days <= 3650:
        raise HTTPException(400, "Keep events between 1 and 3650 days.")
    core.set_setting("event_retention_days", body.event_retention_days)
    core.set_setting("redact_questions", body.redact_questions)
    audit(u, "settings.privacy", "", body.model_dump())
    return body.model_dump()


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
    with core.system():
        return core.db().execute("SELECT * FROM upload_links WHERE token_hash = ? AND expires > ?",
                                 (core.token_hash(token), core.now())).fetchone()


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
    with core.tenant_scope(r["tenant_id"]):
        return await _upload_into(token, r, files)


async def _upload_into(token: str, r, files: list[UploadFile]):
    import html as H
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
                     "note": "Answering uses no AI model: Context Layer finds the evidence (passages, numbers, graph "
                     "links) and the agent that connects over MCP writes the answer." + (
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
    core.rename_tenant(u["tenant_id"], body.organisation)
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
