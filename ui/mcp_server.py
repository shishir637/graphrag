"""Context Layer as an MCP server (Streamable HTTP at /mcp), for any agent: Claude, Cursor, ChatGPT,
your own. Read-only tools; every call is limited to the access groups of the caller's key.

  list_documents   which documents this key can read
  search           hybrid search (meaning + keywords, re-ranked) -> passages with ids and sources
  ask              answer a question with GraphRAG (hybrid by default) -> answer, citations, passages
  find_entity      what the knowledge graph knows about a name: description, links, source passages
  fetch_passage    the exact text of a passage id returned by the other tools

Auth: every request needs "Authorization: Bearer <key>" (create keys in the console). Requests
without a valid key are refused before they reach MCP.
"""
import json
import threading
import time
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from rapidfuzz import fuzz

from rag.config import FINAL_TOP_K, OUT_DIR

from . import keys

LOCK: threading.Lock | None = None          # the UI's pipeline lock: tools wait while data is being rebuilt
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)

INSTRUCTIONS = """Context Layer: search and question answering over this organisation's documents and their
knowledge graph. Answers come with sources. Start with `ask` for questions; use `search` to see raw
passages, `find_entity` to explore a person, product, system or organisation and what it is linked
to, and `fetch_passage` to read the exact text behind a passage id before quoting it. You only see
documents your key's access groups allow."""


# ------------------------------------------------------------------ shared helpers

def _chunks() -> dict:
    p = OUT_DIR / "2_chunks.json"
    if not p.exists():
        raise ToolError("No documents are prepared yet. Add documents in the console and run 'Prepare data'.")
    return {c["chunk_id"]: c for c in json.loads(p.read_text(encoding="utf-8"))}


def _visible(c: dict, groups: list[str]) -> bool:
    return bool(set(c.get("access", [])) & set(groups))


def _passage(c: dict, score=None, max_chars: int = 2000) -> dict:
    out = {"id": c["chunk_id"], "source": c["source"], "pages": c.get("pages") or [],
           "section": " > ".join(c.get("headings") or []), "text": c["text"][:max_chars]}
    if score is not None:
        out["score"] = score
    return out


def _graph_built() -> bool:
    return (OUT_DIR / "g5_graph.json").exists()


class _Busy:
    def __enter__(self):
        if LOCK is not None and not LOCK.acquire(timeout=120):
            raise ToolError("Context Layer is rebuilding its data right now. Try again in a minute.")

    def __exit__(self, *exc):
        if LOCK is not None:
            LOCK.release()


# ------------------------------------------------------------------ tool logic (also used by the console)

def list_documents(key: dict) -> dict:
    docs = {}
    for c in _chunks().values():
        if _visible(c, key["groups"]):
            d = docs.setdefault(c["source"], {"name": c["source"], "passages": 0, "groups": c.get("access", [])})
            d["passages"] += 1
    return {"documents": sorted(docs.values(), key=lambda d: d["name"]), "your_groups": key["groups"],
            "knowledge_graph": "built" if _graph_built() else "not built"}


def search(key: dict, query: str, top_k: int = 5, files: list[str] | None = None) -> dict:
    query = query.strip()
    if not query:
        raise ToolError("query is empty")
    top_k = max(1, min(int(top_k), 10))
    _chunks()
    from rag.retrieve import retrieve
    with _Busy():
        ret = retrieve(query, key["groups"], files or None, save=False)
    hits = sorted(ret["fused"], key=lambda h: -h.get("rerank_score", 0))[:top_k]
    chunks = _chunks()
    return {"query": query, "passages": [_passage(chunks[h["chunk_id"]], h.get("rerank_score"))
                                         for h in hits if h["chunk_id"] in chunks],
            "how": "dense (BGE-M3) + keyword (BM25) search, fused with RRF, re-ranked by a cross-encoder"}


def ask(key: dict, question: str, mode: str = "auto") -> dict:
    question = question.strip()
    if not question:
        raise ToolError("question is empty")
    if mode not in ("auto", "hybrid", "local", "global"):
        raise ToolError("mode must be one of auto, hybrid, local, global")
    _chunks()
    from rag.generate import can_answer
    fallback = None
    with _Busy():
        r = None
        if _graph_built():
            from rag.graph.search import ask_graph
            try:
                r = ask_graph(question, mode, key["groups"], save=False, answer=can_answer())
                used = r["mode"]
            except Exception as e:                       # graph store down etc.: still answer from the text
                fallback = f"knowledge graph unavailable ({type(e).__name__}); answered from document search only"
        if r is None:
            from rag.retrieve import retrieve
            from rag.generate import generate
            ret = retrieve(question, key["groups"], save=False)
            gen = generate(question, ret["passages"])
            r = {**gen, "contexts": ret["passages"], "graph_trace": {}}
            used = "rag" + ("" if fallback else " (knowledge graph not built)")
    chunks = _chunks()
    passages = []
    for c in r["contexts"]:
        if c["chunk_id"] in chunks:
            passages.append(_passage(chunks[c["chunk_id"]], c.get("rerank_score"), 1500))
        else:                                   # graph facts or a community report
            passages.append({"id": c["chunk_id"], "source": c["source"], "text": c["text"][:1500]})
    out = {"question": question, "mode": used, "answer": r.get("answer"), "found": r.get("found"),
           "citations": r.get("citations", []), "passages": passages}
    linked = (r.get("graph_trace") or {}).get("local", {}).get("linked_entities")
    if linked:
        out["entities_in_question"] = [{"name": e["name"], "type": e["type"]} for e in linked]
    if fallback:
        out["warning"] = fallback
    if out["answer"] is None:
        out["note"] = ("No LLM is configured on this Context Layer, so there is no written answer. "
                       "Answer from the passages yourself and cite their ids.")
    return out


def find_entity(key: dict, name: str) -> dict:
    if not _graph_built():
        raise ToolError("The knowledge graph is not built yet. Use search instead.")
    from rag.graph import search as S
    from rag.graph.resolve import normalise
    try:
        S._load()
    except Exception as e:
        raise ToolError(f"The knowledge graph could not be loaded ({type(e).__name__}). Use search instead.")
    groups = key["groups"]
    visible = [e for e in S._Cache.graph["entities"] if any(S._can_see(ch, groups, None) for ch in e["chunk_ids"])]
    target = normalise(name)
    scored = sorted(((max(fuzz.ratio(target, normalise(n)) for n in [e["name"], *e["aliases"]]), e) for e in visible),
                    key=lambda x: -x[0])
    if not scored or scored[0][0] < 80:
        return {"found": False, "name": name,
                "closest": [e["name"] for s, e in scored[:5] if s >= 50],
                "note": "No entity with that name in the parts of the graph your key can see."}
    e = scored[0][1]
    names = {x["id"]: x["name"] for x in visible}         # only entities this key can see
    rels = []
    for r in S._Cache.graph["relations"]:
        if e["id"] not in (r["source"], r["target"]) or not any(S._can_see(ch, groups, None) for ch in r["chunk_ids"]):
            continue
        other = r["target"] if r["source"] == e["id"] else r["source"]
        if other not in names:
            continue
        rels.append({"direction": "out" if r["source"] == e["id"] else "in", "type": r["type"],
                     "entity": names.get(other, other),
                     "evidence": r["description"] if S._can_see(r["chunk_ids"][0], groups, None) else None,
                     "passages": [c for c in r["chunk_ids"] if S._can_see(c, groups, None)]})
    rels.sort(key=lambda x: (x["type"], x["entity"]))
    return {"found": True, "name": e["name"], "type": e["type"], "also_called": e["aliases"],
            "description": S._entity_text(e, groups, None), "relationships": rels[:50],
            "passages": [c for c in e["chunk_ids"] if S._can_see(c, groups, None)]}


def fetch_passage(key: dict, passage_id: str) -> dict:
    c = _chunks().get(passage_id.strip())
    if c is None or not _visible(c, key["groups"]):
        return {"found": False, "id": passage_id, "note": "No passage with that id is available to your key."}
    return {"found": True, **_passage(c, max_chars=20000)}


TOOLS = {"list_documents": list_documents, "search": search, "ask": ask, "find_entity": find_entity,
         "fetch_passage": fetch_passage}


def run_tool(key: dict, tool: str, args: dict) -> dict[str, Any]:
    """Run one tool as a key and log the call (used by MCP and by the console)."""
    t0 = time.perf_counter()
    try:
        out = TOOLS[tool](key, **args)
        n = lambda k, w: f"{len(out[k])} {w}{'' if len(out[k]) == 1 else 's'}"
        if tool in ("find_entity", "fetch_passage"):
            summary = ("found" + (f", {n('passages', 'passage')}" if tool == "find_entity" else "")) if out.get("found") \
                else "not found / not allowed"
        elif "documents" in out:
            summary = n("documents", "document")
        else:
            summary = n("passages", "passage") + ("" if out.get("answer") is None else ", answered")
        keys.record_call(key, tool, args, True, time.perf_counter() - t0, summary)
        return out
    except Exception as e:
        keys.record_call(key, tool, args, False, time.perf_counter() - t0, str(e)[:200])
        raise


# ------------------------------------------------------------------ MCP wiring

mcp = MCPServer(name="context-layer", title="Context Layer", instructions=INSTRUCTIONS, version="0.1.0")


def _key(ctx: Context) -> dict:
    k = keys.verify(keys.token_from_header((ctx.headers or {}).get("authorization")))
    if k is None:                                # the HTTP gate already checked; this is a second check
        raise ToolError("Invalid or revoked access key.")
    return k


@mcp.tool(name="list_documents", annotations=READ_ONLY, structured_output=True)
def list_documents_tool(ctx: Context) -> dict[str, Any]:
    """List the documents this key can read, with their access groups and passage counts."""
    return run_tool(_key(ctx), "list_documents", {})


@mcp.tool(name="search", annotations=READ_ONLY, structured_output=True)
def search_tool(query: str, ctx: Context, top_k: int = 5, files: list[str] | None = None) -> dict[str, Any]:
    """Search the documents by meaning and keywords. Returns the best passages (id, source, page,
    section, text, score). top_k: 1-10. files: optional list of document names to search within."""
    return run_tool(_key(ctx), "search", {"query": query, "top_k": top_k, "files": files})


@mcp.tool(name="ask", annotations=READ_ONLY, structured_output=True)
def ask_tool(question: str, ctx: Context, mode: str = "auto") -> dict[str, Any]:
    """Answer a question from the documents and their knowledge graph, with citations to passage ids.
    mode: auto (default: overview questions use community summaries, others use hybrid),
    hybrid (graph + text search), local (graph neighbourhood only), global (community summaries)."""
    return run_tool(_key(ctx), "ask", {"question": question, "mode": mode})


@mcp.tool(name="find_entity", annotations=READ_ONLY, structured_output=True)
def find_entity_tool(name: str, ctx: Context) -> dict[str, Any]:
    """Look up a person, organisation, product, system or concept in the knowledge graph: its
    description, other names, relationships to other entities, and the passages it appears in."""
    return run_tool(_key(ctx), "find_entity", {"name": name})


@mcp.tool(name="fetch_passage", annotations=READ_ONLY, structured_output=True)
def fetch_passage_tool(passage_id: str, ctx: Context) -> dict[str, Any]:
    """Read the exact, full text of a passage by the id returned from search, ask or find_entity.
    Use it before quoting."""
    return run_tool(_key(ctx), "fetch_passage", {"passage_id": passage_id})


class KeyGate:
    """ASGI middleware in front of /mcp: refuse requests without a valid key with 401."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            if keys.verify(keys.token_from_header(headers.get("authorization"))) is None:
                body = json.dumps({"error": "invalid_token",
                                   "error_description": "Send 'Authorization: Bearer <key>'. Create a key in the "
                                                        "Context Layer console."}).encode()
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"www-authenticate", b'Bearer error="invalid_token"')]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def http_app():
    """The MCP Starlette app (path /mcp), wrapped in the key gate."""
    return KeyGate(mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True))
