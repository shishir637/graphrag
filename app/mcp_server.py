"""Context Layer for agents: an MCP server at /mcp (Streamable HTTP, stateless, JSON responses).

Every request carries "Authorization: Bearer <token>": an agent key (clk_..., made under Agents) or a token from
signing in to Context Layer inside Claude or ChatGPT (cla_..., see app/oauth.py). The token decides:
  groups        which documents (and graph facts from them) the caller can see
  capabilities  documents / data (metrics) / graph / upload
Signed-in users act as themselves (their groups; owners and admins see everything and may upload).
Tools refuse clearly when a capability is missing. Every call is logged as an agent event."""
import base64
import contextvars
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import core, engine, jobs
from . import db as D

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITES = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
MAX_INLINE_MB = 15
MAX_FILE_MB = 200
UPLOAD_LINK_MIN = 30

INSTRUCTIONS = """Context Layer gives you this organisation's knowledge as evidence with sources: passages from its
documents, business numbers computed from reviewed metric definitions (never guessed), and a knowledge graph of
how things connect. It does not write answers: you do, from the evidence it returns. Cite passage ids.

- `ask`: start here. It picks documents, metrics or the graph and returns passages (with highlighted sentences),
  graph facts or metric results. If the evidence does not contain the answer, say so.
- `search_documents`: more passages, or passages from specific files.
- `describe_data` then `query_metrics`: for numbers. Fill in metric and dimension NAMES from describe_data
  (the server writes the SQL). You may pass `question` instead and let the server plan it.
- `find_connections` and `explore_entity`: how people, places, organisations and things are linked, with the
  sentence behind every link.
- `fetch_passage`: the full text of a passage id before you quote it.
- Adding files (needs upload permission): when the user shares a file in the chat, call `add_document`
  (ChatGPT passes the attached file itself; otherwise pass its text, or base64 for small binary files).
  For large or binary files you cannot pass, call `create_upload_link` and give the user the link.
  Context Layer then builds its search index, knowledge graph and metric drafts in the background:
  call `build_status` until it says ready, then answer from the new files with `ask`.
You only see what your key allows; if something is refused, say so rather than guessing."""

# who is calling (set by KeyGate for the request): decides which tools are listed
CALLER: contextvars.ContextVar[dict | None] = contextvars.ContextVar("cl_caller", default=None)
UPLOAD_TOOLS = {"add_document", "create_upload_link"}
ADMIN_TOOLS = {"approve_metric"}          # setting the workspace up: admin-scope keys, signed-in owners/admins


def allowed_tools(caller: dict | None) -> set[str] | None:
    """Tool names this caller may see (None: all, e.g. outside a request)."""
    if caller is None:
        return None
    hide = set()
    if "upload" not in caller.get("capabilities", []):
        hide |= UPLOAD_TOOLS
    if caller.get("scope") != "admin":
        hide |= ADMIN_TOOLS
    return {t for t in TOOLS if t not in hide}


class ScopedServer(MCPServer):
    """Lists only the tools the caller's key may use, so a question-only key never sees write tools."""

    async def list_tools(self):
        tools = await super().list_tools()
        allow = allowed_tools(CALLER.get())
        return tools if allow is None else [t for t in tools if t.name in allow]


mcp = ScopedServer(name="context-layer", title="Context Layer", instructions=INSTRUCTIONS, version=core.VERSION)


def _bearer(headers) -> str | None:
    h = (headers or {}).get("authorization") or ""
    return h[7:].strip() if h[:7].lower() == "bearer " else None


def resolve(token: str | None, resource: str | None = None) -> dict | None:
    """The caller behind a token, with their effective groups."""
    a = core.principal_for_token(token, resource)
    if a and a.get("all_groups"):
        a["groups"] = sorted(set(engine.all_groups()) | set(a["groups"]))
    return a


def _agent(ctx: Context) -> dict:
    a = resolve(_bearer(ctx.headers))
    if a is None:
        raise ToolError("Invalid, expired or revoked sign-in. Reconnect Context Layer.")
    if a["kind"] == "user":
        core.touch_token(_bearer(ctx.headers))
    return a


def _need(agent: dict, cap: str):
    if cap not in agent["capabilities"]:
        raise ToolError(f"This key may not use {cap}. Ask your Context Layer admin to allow it.")


def _wait():
    """Questions no longer wait for builds: indexes are updated in place, so the last good version answers."""


def run(agent: dict, tool: str, args: dict, fn, wait: bool = True) -> dict:
    t = core.timer()
    try:
        if wait:
            _wait()
        out = fn()
        core.touch_agent(agent["id"])
        summary = out.get("route") or (f"{len(out['passages'])} passages" if "passages" in out else
                                       f"{out['row_count']} rows" if "row_count" in out else "ok")
        core.log_event("agent", agent["name"], f"tool.{tool}", args.get("question") or args.get("query") or
                       args.get("passage_id") or "", {"args": args, "summary": summary, "agent_id": agent["id"]},
                       True, t())
        return out
    except ToolError as e:
        core.log_event("agent", agent["name"], f"tool.{tool}", "", {"args": args, "error": str(e),
                                                                     "agent_id": agent["id"]}, False, t())
        raise
    except (PermissionError, ValueError, RuntimeError) as e:
        core.log_event("agent", agent["name"], f"tool.{tool}", "", {"args": args, "error": str(e),
                                                                     "agent_id": agent["id"]}, False, t())
        raise ToolError(str(e)) from e


# ------------------------------------------------------------------ tool logic (also used by the Playground)

def tool_ask(agent: dict, question: str, mode: str = "auto") -> dict:
    if mode not in ("auto", "documents", "metrics", "graph", "overview"):
        raise ToolError("mode must be auto, documents, metrics, graph or overview")
    return engine.ask(question, agent["groups"], agent["capabilities"], mode)


def tool_search(agent: dict, query: str, top_k: int = 5, files: list[str] | None = None) -> dict:
    _need(agent, "documents")
    r = engine.search(query, agent["groups"], top_k, files, use_graph="graph" in agent["capabilities"])
    return {"query": query, "evidence": r["evidence"], "passages": r["passages"], "entities_found": r["entities"],
            "guidance": engine.GUIDANCE}


def tool_describe_data(agent: dict) -> dict:
    _need(agent, "data")
    return engine.describe_data()


def tool_metrics(agent: dict, metrics: list[str] | None = None, dimensions: list[str] | None = None,
                 filters: list[dict] | None = None, period: str | None = None, start_date: str | None = None,
                 end_date: str | None = None, order_by: list[str] | None = None, limit: int | None = None,
                 question: str | None = None) -> dict:
    _need(agent, "data")
    if metrics:
        req = {"metrics": metrics, "dimensions": dimensions or [], "filters": filters or [],
               "order_by": order_by or [], "limit": limit, "period": period,
               "start_date": start_date, "end_date": end_date}
        return engine.metric_query({k: v for k, v in req.items() if v not in (None, [], "")})
    if not question:
        raise ToolError("Give metric names (see describe_data) or a question.")
    return engine.metric_query(question=question)


def tool_list_metrics(agent: dict) -> dict:
    _need(agent, "data")
    d = engine.describe_data()
    return {"metrics": [{k: m[k] for k in ("name", "label", "description", "format")} for m in d["metrics"]],
            "dimensions": [{k: x[k] for k in ("name", "label", "type") if k in x} for x in d["dimensions"]]}


def tool_connections(agent: dict, question: str) -> dict:
    _need(agent, "graph")
    from graph import search as GS
    from graph.access import visible
    if not engine.graph_ready():
        raise ToolError("The knowledge graph has not been built yet.")
    r = GS.local(question, agent["groups"], visible)
    return {"entities": r["entities"], "bridges": r["bridges"], "relations": r["relations"],
            "passage_ids": r["passage_ids"][:10],
            "guidance": "Links come from sentences in the documents (evidence); fetch_passage reads a passage in full."}


def tool_entity(agent: dict, name: str) -> dict:
    _need(agent, "graph")
    try:
        e = engine.explore_entity(name, agent["groups"])
    except RuntimeError as ex:
        raise ToolError(str(ex)) from ex
    return {"found": True, **e} if e else {"found": False, "name": name, "note": "No such entity visible to your key."}


def tool_list_documents(agent: dict) -> dict:
    _need(agent, "documents")
    return {"documents": engine.visible_documents(agent["groups"]), "your_groups": agent["groups"]}


def tool_fetch(agent: dict, passage_id: str) -> dict:
    if not {"documents", "graph"} & set(agent["capabilities"]):
        raise ToolError("This key may not read documents.")
    p = engine.passage(passage_id.strip(), agent["groups"])
    return {"found": True, **p} if p else {"found": False, "id": passage_id,
                                           "note": "No passage with that id is available to your key."}


# ------------------------------------------------------------------ adding files

def _upload_groups(agent: dict, groups: list[str] | None) -> list[str]:
    """Uploads may only be shared with groups the caller belongs to (owners and admins: any group)."""
    want = engine.clean_groups(groups or []) if groups else []
    if not want:
        want = ["public"] if "public" in agent["groups"] or agent.get("all_groups") else agent["groups"][:1]
    if not agent.get("all_groups") and not set(want) <= set(agent["groups"]):
        raise ToolError(f"You can only share uploads with your own groups: {', '.join(agent['groups'])}.")
    return want


def download(url: str, max_mb: int = MAX_FILE_MB) -> bytes:
    """Fetch a file the chat app gave us (e.g. ChatGPT's download_url): https and public addresses only."""
    import httpx
    from .oauth import public_host
    for _ in range(4):
        u = urlparse(url)
        if u.scheme != "https" or not u.hostname or not public_host(u.hostname):
            raise ToolError("Files can only be fetched from public https addresses.")
        with httpx.Client(timeout=60.0, follow_redirects=False) as c, c.stream("GET", url) as r:
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                url = str(r.url.join(r.headers["location"]))
                continue
            if r.status_code != 200:
                raise ToolError(f"Could not fetch the file (HTTP {r.status_code}). Attach it again.")
            buf = bytearray()
            for chunk in r.iter_bytes():
                buf += chunk
                if len(buf) > max_mb * 2 ** 20:
                    raise ToolError(f"The file is larger than {max_mb} MB.")
            return bytes(buf)
    raise ToolError("Too many redirects while fetching the file.")


def tool_add_document(agent: dict, filename: str | None = None, text: str | None = None,
                      content_base64: str | None = None, file: dict | None = None, kind: str = "auto",
                      groups: list[str] | None = None) -> dict:
    _need(agent, "upload")
    g = _upload_groups(agent, groups)
    given = [x for x in (text, content_base64, file) if x]
    if len(given) != 1:
        raise ToolError("Give exactly one of: file (an attached file), text, or content_base64.")
    if file:
        if not isinstance(file, dict) or not file.get("download_url"):
            raise ToolError("This chat app did not pass the file. Use create_upload_link instead.")
        filename = filename or file.get("file_name") or file.get("name")
        data = download(str(file["download_url"]))
    elif text:
        data = text.encode("utf-8")
        if len(data) > MAX_INLINE_MB * 2 ** 20:
            raise ToolError(f"The text is larger than {MAX_INLINE_MB} MB. Use create_upload_link instead.")
        stem, dot, ext = (filename or "note.md").rpartition(".")
        if not dot or "." + ext.lower() not in engine.TEXT_EXTS:      # e.g. report.pdf given as its text
            filename = f"{stem or ext or 'note'}.md"
    else:
        try:
            data = base64.b64decode(content_base64, validate=True)
        except ValueError as e:
            raise ToolError("content_base64 is not valid base64.") from e
        if len(data) > MAX_INLINE_MB * 2 ** 20:
            raise ToolError(f"The file is larger than {MAX_INLINE_MB} MB. Use create_upload_link instead.")
    if not filename:
        raise ToolError("Give the file a name, e.g. report.pdf.")
    try:
        saved = engine.ingest_upload(filename, data, g, kind)
    except ValueError as e:
        raise ToolError(str(e)) from e
    job = engine.queue_rebuild([saved], agent["name"])
    return {"saved": saved, "bytes": len(data), "job_id": job["id"], "status": job["status"],
            "next": "Context Layer is building the search index, graph and metric drafts for this file. "
                    "Call build_status with this job_id until it is ready (usually 1-5 minutes)."}


def tool_upload_link(agent: dict, groups: list[str] | None = None, base: str = "") -> dict:
    _need(agent, "upload")
    g = _upload_groups(agent, groups)
    tok = core.new_token("clu_")
    exp = (datetime.now(timezone.utc) + timedelta(minutes=UPLOAD_LINK_MIN)).isoformat(timespec="seconds")
    core.db().execute("INSERT INTO upload_links (token_hash, tenant_id, principal, groups, created, expires) "
                      "VALUES (?, ?, ?, ?, ?, ?)",
                      (core.token_hash(tok), agent["tenant_id"], agent["name"], json.dumps(g), core.now(), exp))
    return {"upload_url": f"{base}/upload/{tok}", "expires_in_minutes": UPLOAD_LINK_MIN, "groups": g,
            "next": "Give this link to the user. After they upload, the page shows a job id: "
                    "call build_status (no arguments shows your latest upload)."}


def tool_build_status(agent: dict, job_id: str | None = None) -> dict:
    from . import jobs as J
    mine = [j for j in J.list_jobs(50) if j["kind"] == "rebuild" and (j["by"] == agent["name"] or agent.get("all_groups"))]
    if job_id:
        j = J.get(job_id.strip())
        if j is None or j["kind"] != "rebuild":
            raise ToolError("No upload job with that id. Call build_status without a job_id to see recent ones.")
    elif mine:
        j = mine[0]
    else:
        return {"status": "none", "note": "No uploads yet."}
    return engine.rebuild_report(j, J.info(j["id"]))


def tool_approve_metric(agent: dict, name: str) -> dict:
    if agent.get("scope") != "admin":
        raise ToolError("Only an admin key or a signed-in Context Layer owner or admin can approve metrics.")
    from semantic.auto_model import set_status
    try:
        r = set_status(name.strip(), "approved", by=agent.get("email") or agent["name"])
    except KeyError as e:
        raise ToolError(f"No metric named {name}. See build_status or the Metrics page for drafts.") from e
    except ValueError as e:
        raise ToolError(str(e)) from e
    who = agent.get("email") or agent["name"]
    core.log_event("user" if agent.get("kind") == "user" else "agent", who, "metrics.status", name,
                   {"status": "approved", "via": agent.get("client") or "admin key"})
    return r | {"next": "Approved. query_metrics can use it now."}


TOOLS = {"ask": tool_ask, "search_documents": tool_search, "describe_data": tool_describe_data,
         "query_metrics": tool_metrics, "list_metrics": tool_list_metrics, "find_connections": tool_connections,
         "explore_entity": tool_entity, "list_documents": tool_list_documents, "fetch_passage": tool_fetch,
         "add_document": tool_add_document, "create_upload_link": tool_upload_link, "build_status": tool_build_status,
         "approve_metric": tool_approve_metric}


# ------------------------------------------------------------------ MCP registrations

@mcp.tool(name="ask", annotations=READ_ONLY, structured_output=True)
def _ask(question: str, ctx: Context, mode: str = "auto") -> dict[str, Any]:
    """Gather the evidence for a question from the organisation's documents, metrics or knowledge graph.
    Returns passages (id, file, section, text, highlighted sentences), graph facts, or metric results.
    You write the answer from this evidence and cite passage ids.
    mode: auto (default), documents, metrics, graph (how things connect) or overview (big picture)."""
    a = _agent(ctx)
    return run(a, "ask", {"question": question, "mode": mode}, lambda: tool_ask(a, question, mode))


@mcp.tool(name="search_documents", annotations=READ_ONLY, structured_output=True)
def _search(query: str, ctx: Context, top_k: int = 5, files: list[str] | None = None) -> dict[str, Any]:
    """Find the most relevant passages in the documents (meaning + keywords + knowledge graph, re-ranked).
    Returns id, file, page, section, text, highlighted sentences and score. top_k: 1-10.
    files: optional list of file names to search within."""
    a = _agent(ctx)
    return run(a, "search_documents", {"query": query, "top_k": top_k, "files": files},
               lambda: tool_search(a, query, top_k, files))


@mcp.tool(name="describe_data", annotations=READ_ONLY, structured_output=True)
def _describe(ctx: Context) -> dict[str, Any]:
    """The approved business metrics, the dimensions they can be split or filtered by (with known values),
    synonyms, business rules (fiscal year, what 'revenue' means) and example requests. Read it before
    query_metrics."""
    a = _agent(ctx)
    return run(a, "describe_data", {}, lambda: tool_describe_data(a))


@mcp.tool(name="query_metrics", annotations=READ_ONLY, structured_output=True)
def _metrics(ctx: Context, metrics: list[str] | None = None, dimensions: list[str] | None = None,
             filters: list[dict] | None = None, period: str | None = None, start_date: str | None = None,
             end_date: str | None = None, order_by: list[str] | None = None, limit: int | None = None,
             question: str | None = None) -> dict[str, Any]:
    """Compute business numbers from the reviewed metric definitions (the server writes the SQL; never ad hoc).
    metrics: metric names from describe_data, e.g. ["net_revenue"]. dimensions: e.g. ["regions.region_name",
    "orders.order_date__month"]. filters: [{"dimension": "customers.city", "op": "=", "values": ["Mumbai"]}].
    period: words like "last quarter", "FY2025-26", "2025" (or start_date/end_date, YYYY-MM-DD, inclusive).
    order_by: ["-net_revenue"] for descending; limit: top N. Or pass only `question` in plain English.
    Returns rows, the SQL, assumptions and corrected filter values."""
    a = _agent(ctx)
    args = {"metrics": metrics, "dimensions": dimensions, "filters": filters, "period": period,
            "start_date": start_date, "end_date": end_date, "order_by": order_by, "limit": limit, "question": question}
    return run(a, "query_metrics", {k: v for k, v in args.items() if v},
               lambda: tool_metrics(a, metrics, dimensions, filters, period, start_date, end_date, order_by, limit,
                                    question))


@mcp.tool(name="list_metrics", annotations=READ_ONLY, structured_output=True)
def _list_metrics(ctx: Context) -> dict[str, Any]:
    """List the approved business metrics and the dimensions they can be broken down by (short form of describe_data)."""
    a = _agent(ctx)
    return run(a, "list_metrics", {}, lambda: tool_list_metrics(a))


@mcp.tool(name="find_connections", annotations=READ_ONLY, structured_output=True)
def _connections(question: str, ctx: Context) -> dict[str, Any]:
    """Knowledge-graph evidence for a question that links things: the entities it names, the entities that
    connect them, and the links between them with the sentence behind each link and its passage ids."""
    a = _agent(ctx)
    return run(a, "find_connections", {"question": question}, lambda: tool_connections(a, question))


@mcp.tool(name="explore_entity", annotations=READ_ONLY, structured_output=True)
def _entity(name: str, ctx: Context) -> dict[str, Any]:
    """One entity of the knowledge graph (person, place, organisation, product...) with its links, the evidence
    sentence for each link, and the passages that mention it."""
    a = _agent(ctx)
    return run(a, "explore_entity", {"name": name}, lambda: tool_entity(a, name))


@mcp.tool(name="list_documents", annotations=READ_ONLY, structured_output=True)
def _list_documents(ctx: Context) -> dict[str, Any]:
    """List the documents this key can read."""
    a = _agent(ctx)
    return run(a, "list_documents", {}, lambda: tool_list_documents(a))


@mcp.tool(name="fetch_passage", annotations=READ_ONLY, structured_output=True)
def _fetch(passage_id: str, ctx: Context) -> dict[str, Any]:
    """Read the exact, full text of a passage by its id (from ask or search_documents) before quoting it."""
    a = _agent(ctx)
    return run(a, "fetch_passage", {"passage_id": passage_id}, lambda: tool_fetch(a, passage_id))


@mcp.tool(name="add_document", annotations=WRITES, structured_output=True,
          meta={"openai/fileParams": ["file"],
                "openai/toolInvocation/invoking": "Adding the file to Context Layer",
                "openai/toolInvocation/invoked": "Added; building in the background"})
def _add_document(ctx: Context, filename: str | None = None, file: dict[str, Any] | None = None,
                  text: str | None = None, content_base64: str | None = None, kind: str = "auto",
                  groups: list[str] | None = None) -> dict[str, Any]:
    """Add a file the user shared in the chat to Context Layer. It becomes searchable passages and knowledge-graph
    facts (documents) or a data table with draft metrics (CSV / Excel / Parquet), built in the background.
    Pass exactly one of: file (the attached file; ChatGPT fills this in), text (the file's full text, e.g. what
    you read from an attached PDF or Word file - keep its headings), or content_base64 (small binary files).
    filename: e.g. "pricing-policy.pdf". kind: auto (default), document or table.
    groups: who may see it (default public, or your own group). Returns a job_id for build_status."""
    a = _agent(ctx)
    args = {"filename": filename, "kind": kind, "groups": groups,
            "source": "file" if file else "text" if text else "base64"}
    return run(a, "add_document", args, lambda: tool_add_document(a, filename, text, content_base64, file, kind, groups),
               wait=False)


@mcp.tool(name="create_upload_link", annotations=WRITES, structured_output=True)
def _upload_link(ctx: Context, groups: list[str] | None = None) -> dict[str, Any]:
    """A one-time page (valid 30 minutes) where the user can drop large or binary files (PDF, Word, Excel...)
    straight into Context Layer. Use it when you cannot pass the file with add_document. Give the user the link."""
    a = _agent(ctx)
    from .public import base_url
    scope = getattr(getattr(ctx.request_context, "request", None), "scope", None) or {}
    return run(a, "create_upload_link", {"groups": groups}, lambda: tool_upload_link(a, groups, base_url(scope)),
               wait=False)


@mcp.tool(name="build_status", annotations=READ_ONLY, structured_output=True)
def _build_status(ctx: Context, job_id: str | None = None) -> dict[str, Any]:
    """Progress of the background build after an upload (queued, running, done or failed), and once done what
    the files became: passages, knowledge-graph size and draft metrics waiting for approval.
    job_id: from add_document or the upload page; leave out for your latest upload."""
    a = _agent(ctx)
    return run(a, "build_status", {"job_id": job_id}, lambda: tool_build_status(a, job_id), wait=False)


@mcp.tool(name="approve_metric", annotations=WRITES, structured_output=True)
def _approve(ctx: Context, name: str) -> dict[str, Any]:
    """Approve a draft metric (from build_status) so agents may use it. Only for signed-in owners and admins:
    confirm with the user first, and show them the definition (label, expression) you are approving."""
    a = _agent(ctx)
    return run(a, "approve_metric", {"name": name}, lambda: tool_approve_metric(a, name), wait=False)


class KeyGate:
    """Refuse /mcp requests without a valid token (401) before they reach MCP. The 401 tells MCP clients
    (Claude, ChatGPT) where to sign in, so users can connect with just the URL."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            from . import oauth
            from .public import base_url
            tok = _bearer({k.decode().lower(): v.decode() for k, v in scope.get("headers", [])})
            caller = core.principal_for_token(tok, base_url(scope) + "/mcp")
            if caller is None:
                body = json.dumps({"error": "invalid_token", "error_description":
                                   "Sign in to Context Layer from your AI app, or send 'Authorization: Bearer <key>' "
                                   "(keys are made under Agents in Context Layer)."}).encode()
                www = oauth.www_authenticate(scope, bool(tok)) if oauth.enabled() else b'Bearer error="invalid_token"'
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"), (b"www-authenticate", www),
                                        (b"access-control-expose-headers", b"WWW-Authenticate")]})
                await send({"type": "http.response.body", "body": body})
                return
            t1, t2 = D.TENANT.set(caller["tenant_id"]), CALLER.set(caller)
            try:
                await self.app(scope, receive, send)
            finally:
                D.TENANT.reset(t1)
                CALLER.reset(t2)
            return
        await self.app(scope, receive, send)


def transport_security():
    """DNS-rebinding guard: localhost always, plus the names this installation is served under
    (CL_PUBLIC_HOSTS=contextlayer.acme.com,10.0.0.5). Without them, agents calling the public name get 421."""
    import os
    from mcp.server.transport_security import TransportSecuritySettings
    hosts = ["127.0.0.1", "localhost", "[::1]"] + [h.strip() for h in os.environ.get("CL_PUBLIC_HOSTS", "").split(",") if h.strip()]
    if any("*" in h for h in hosts):
        # wildcard names (e.g. *.trycloudflare.com): the app's own host check (app/public.py) enforces them;
        # every MCP request also needs a valid agent key
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[x for h in hosts for x in (h, f"{h}:*")],
        allowed_origins=[f"{scheme}://{h}{port}" for h in hosts for scheme in ("http", "https") for port in ("", ":*")])


def http_app():
    return KeyGate(mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True,
                                           transport_security=transport_security()))
