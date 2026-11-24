"""Context Layer for agents: an MCP server at /mcp (Streamable HTTP, stateless, JSON responses).

Every request carries "Authorization: Bearer <agent key>". The key decides:
  groups        which documents (and graph facts from them) the agent can see
  capabilities  documents / data (metrics) / graph
Tools refuse clearly when a capability is missing. Every call is logged as an agent event."""
import json
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import core, engine, jobs

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)

INSTRUCTIONS = """Context Layer gives you this organisation's knowledge with sources: its documents, its business
metrics (numbers computed from reviewed definitions, never guessed) and a knowledge graph of how things connect.
Use `ask` for most questions: it picks documents, metrics or the graph by itself and returns the answer with
sources. Use `search_documents` to read raw passages, `query_metrics` for numbers, `list_metrics` to see which
numbers exist, and `fetch_passage` to read the exact text behind a source id before quoting it.
You only see what your key allows; if something is refused, say so rather than guessing."""

mcp = MCPServer(name="context-layer", title="Context Layer", instructions=INSTRUCTIONS, version=core.VERSION)


def _agent(ctx: Context) -> dict:
    hdr = (ctx.headers or {}).get("authorization") or ""
    a = core.agent_for_token(hdr[7:].strip() if hdr[:7].lower() == "bearer " else None)
    if a is None:
        raise ToolError("Invalid or revoked key.")
    return a


def _need(agent: dict, cap: str):
    if cap not in agent["capabilities"]:
        raise ToolError(f"This key may not use {cap}. Ask your Context Layer admin to allow it.")


def _wait():
    if not jobs.LOCK.acquire(timeout=180):
        raise ToolError("Context Layer is rebuilding its data. Try again in a few minutes.")
    jobs.LOCK.release()


def run(agent: dict, tool: str, args: dict, fn) -> dict:
    t = core.timer()
    try:
        _wait()
        out = fn()
        core.touch_agent(agent["id"])
        summary = out.get("route") or (f"{len(out['sources'])} sources" if "sources" in out else
                                       f"{len(out['passages'])} passages" if "passages" in out else "ok")
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
    if mode not in ("auto", "documents", "metrics", "graph"):
        raise ToolError("mode must be auto, documents, metrics or graph")
    m = {"auto": "auto", "documents": "vector", "metrics": "data", "graph": "local"}[mode]
    return engine.ask(question, agent["groups"], agent["capabilities"], m)


def tool_search(agent: dict, query: str, top_k: int = 5, files: list[str] | None = None) -> dict:
    _need(agent, "documents")
    return {"query": query, "passages": engine.search(query, agent["groups"], top_k, files)}


def tool_metrics(agent: dict, question: str) -> dict:
    _need(agent, "data")
    return engine.ask(question, None, ["data"], "data")


def tool_list_metrics(agent: dict) -> dict:
    _need(agent, "data")
    m = engine.metrics()
    return {"metrics": [{k: x[k] for k in ("name", "label", "description", "format")} for x in m["metrics"]
                        if x.get("status", "approved") == "approved"],
            "dimensions": m["dimensions"]}


def tool_list_documents(agent: dict) -> dict:
    _need(agent, "documents")
    return {"documents": engine.visible_documents(agent["groups"]), "your_groups": agent["groups"]}


def tool_fetch(agent: dict, passage_id: str) -> dict:
    if not {"documents", "graph"} & set(agent["capabilities"]):
        raise ToolError("This key may not read documents.")
    p = engine.passage(passage_id.strip(), agent["groups"])
    return {"found": True, **p} if p else {"found": False, "id": passage_id,
                                           "note": "No passage with that id is available to your key."}


TOOLS = {"ask": tool_ask, "search_documents": tool_search, "query_metrics": tool_metrics,
         "list_metrics": tool_list_metrics, "list_documents": tool_list_documents, "fetch_passage": tool_fetch}


# ------------------------------------------------------------------ MCP registrations

@mcp.tool(name="ask", annotations=READ_ONLY, structured_output=True)
def _ask(question: str, ctx: Context, mode: str = "auto") -> dict[str, Any]:
    """Answer a question from the organisation's documents, metrics or knowledge graph, with sources.
    mode: auto (default, picks the right source), documents, metrics, or graph (questions that connect things)."""
    a = _agent(ctx)
    return run(a, "ask", {"question": question, "mode": mode}, lambda: tool_ask(a, question, mode))


@mcp.tool(name="search_documents", annotations=READ_ONLY, structured_output=True)
def _search(query: str, ctx: Context, top_k: int = 5, files: list[str] | None = None) -> dict[str, Any]:
    """Find the most relevant passages in the documents (meaning + keywords, re-ranked). Returns id, file,
    page, section, text and score. top_k: 1-10. files: optional list of file names to search within."""
    a = _agent(ctx)
    return run(a, "search_documents", {"query": query, "top_k": top_k, "files": files},
               lambda: tool_search(a, query, top_k, files))


@mcp.tool(name="query_metrics", annotations=READ_ONLY, structured_output=True)
def _metrics(question: str, ctx: Context) -> dict[str, Any]:
    """Compute a business number from the data tables using the reviewed metric definitions (never ad-hoc SQL),
    e.g. 'net revenue by region last quarter'. Returns the answer, rows, the request and any assumptions."""
    a = _agent(ctx)
    return run(a, "query_metrics", {"question": question}, lambda: tool_metrics(a, question))


@mcp.tool(name="list_metrics", annotations=READ_ONLY, structured_output=True)
def _list_metrics(ctx: Context) -> dict[str, Any]:
    """List the approved business metrics and the dimensions they can be broken down by."""
    a = _agent(ctx)
    return run(a, "list_metrics", {}, lambda: tool_list_metrics(a))


@mcp.tool(name="list_documents", annotations=READ_ONLY, structured_output=True)
def _list_documents(ctx: Context) -> dict[str, Any]:
    """List the documents this key can read."""
    a = _agent(ctx)
    return run(a, "list_documents", {}, lambda: tool_list_documents(a))


@mcp.tool(name="fetch_passage", annotations=READ_ONLY, structured_output=True)
def _fetch(passage_id: str, ctx: Context) -> dict[str, Any]:
    """Read the exact, full text of a source passage by its id (from ask or search_documents) before quoting it."""
    a = _agent(ctx)
    return run(a, "fetch_passage", {"passage_id": passage_id}, lambda: tool_fetch(a, passage_id))


class KeyGate:
    """Refuse /mcp requests without a valid agent key (401) before they reach MCP."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            h = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}.get("authorization", "")
            if core.agent_for_token(h[7:].strip() if h[:7].lower() == "bearer " else None) is None:
                body = json.dumps({"error": "invalid_token", "error_description":
                                   "Send 'Authorization: Bearer <key>'. Keys are created under Agents in Context Layer."}).encode()
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"www-authenticate", b'Bearer error="invalid_token"')]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def transport_security():
    """DNS-rebinding guard: localhost always, plus the names this installation is served under
    (CL_PUBLIC_HOSTS=contextlayer.acme.com,10.0.0.5). Without them, agents calling the public name get 421."""
    import os
    from mcp.server.transport_security import TransportSecuritySettings
    hosts = ["127.0.0.1", "localhost", "[::1]"] + [h.strip() for h in os.environ.get("CL_PUBLIC_HOSTS", "").split(",") if h.strip()]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[x for h in hosts for x in (h, f"{h}:*")],
        allowed_origins=[f"{scheme}://{h}{port}" for h in hosts for scheme in ("http", "https") for port in ("", ":*")])


def http_app():
    return KeyGate(mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True,
                                           transport_security=transport_security()))
