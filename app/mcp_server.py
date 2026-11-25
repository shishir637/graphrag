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
    return {"query": query, "passages": r["passages"], "entities_found": r["entities"], "guidance": engine.GUIDANCE}


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


TOOLS = {"ask": tool_ask, "search_documents": tool_search, "describe_data": tool_describe_data,
         "query_metrics": tool_metrics, "list_metrics": tool_list_metrics, "find_connections": tool_connections,
         "explore_entity": tool_entity, "list_documents": tool_list_documents, "fetch_passage": tool_fetch}


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
