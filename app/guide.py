"""The getting-started guide shown when someone first signs in to a workspace.

Steps depend on the person's role and are ticked from the workspace's real state (not from clicks), so the guide
shows what is actually left. Its state (dismissed) is kept per person and workspace (memberships.guide).
"""
import json

from . import core


def _state(u: dict) -> dict:
    r = core.db().execute("SELECT guide FROM memberships WHERE user_id = ? AND tenant_id = ?",
                          (u["id"], u["tenant_id"])).fetchone()
    try:
        return json.loads(r["guide"]) if r else {}
    except ValueError:
        return {}


def _save(u: dict, st: dict):
    core.db().execute("UPDATE memberships SET guide = ? WHERE user_id = ? AND tenant_id = ?",
                      (json.dumps(st), u["id"], u["tenant_id"]))


def dismiss(u: dict) -> dict:
    st = _state(u) | {"dismissed": True, "dismissed_at": core.now()}
    _save(u, st)
    return st


def reset(u: dict) -> dict:
    _save(u, {})
    return {}


def facts(u: dict) -> dict:
    """What has really happened in this workspace (and, for questions, by this person)."""
    from . import engine, jobs, oauth
    t = core.current_tenant()
    org = core.get_setting("organisation", "") or ""
    st = engine.status()
    n_conn = core.db().execute("SELECT count(*) FROM connectors WHERE tenant_id = ?", (t,)).fetchone()[0]
    drafts = core.db().execute("SELECT count(*) FROM sl_definitions WHERE tenant_id = ? AND status IN ('draft', 'needs_review')",
                               (t,)).fetchone()[0]
    drafts += core.db().execute("SELECT count(*) FROM onto_definitions WHERE tenant_id = ? AND status = 'draft'",
                                (t,)).fetchone()[0]
    drafts += core.db().execute("SELECT count(*) FROM facts WHERE tenant_id = ? AND status = 'needs_review'", (t,)).fetchone()[0]
    drafts += core.db().execute("SELECT count(*) FROM fact_links WHERE tenant_id = ? AND status = 'needs_review'",
                                (t,)).fetchone()[0]
    keys = [a for a in core.list_agents() if not a["revoked"]]
    apps = oauth.grants(None)
    asked_any = core.db().execute("SELECT 1 FROM events WHERE tenant_id = ? AND action IN ('playground.ask', 'tool.ask', "
                                  "'tool.search_documents', 'tool.query_metrics') LIMIT 1", (t,)).fetchone()
    asked_me = core.db().execute("SELECT 1 FROM events WHERE tenant_id = ? AND ((action = 'playground.ask' AND actor = ?) OR "
                                 "(action LIKE 'tool.%' AND actor LIKE ?)) LIMIT 1",
                                 (t, u["email"], f"{u['name']} via %")).fetchone()
    built = st["documents"]["ready"] > 0 or st["data"]["ready"] or st["graph"]["ready"]
    return {"named": bool(org.strip()), "source": bool(n_conn or st["documents"]["count"] or st["data"]["tables"]),
            "built": bool(built), "building": jobs.running_mine() is not None, "drafts": drafts,
            "connected": bool(keys or apps), "my_app": any(g["user_id"] == u["id"] for g in apps),
            "asked": bool(asked_any), "asked_me": bool(asked_me), "status": st}


def mcp_url(base: str) -> str:
    from .public import public_mcp_url
    try:
        return public_mcp_url() or base.rstrip("/") + "/mcp"
    except Exception:                                          # noqa: BLE001
        return base.rstrip("/") + "/mcp"


def connect_snippets(url: str) -> dict:
    return {
        "claude_web": f"Claude: Settings → Connectors → Add custom connector → paste {url} → sign in with your "
                      "Context Layer account and choose Allow.",
        "chatgpt": f"ChatGPT: Settings → Apps & connectors → Create (developer mode) → paste {url}, authentication "
                   "OAuth → sign in and allow.",
        "claude_code": f'claude mcp add --transport http context-layer {url} --header "Authorization: Bearer <key>"',
        "cursor": json.dumps({"mcpServers": {"context-layer": {"url": url, "headers": {"Authorization": "Bearer <key>"}}}},
                             indent=2),
        "scopes": {"agent": "asks questions (and adds files if allowed)",
                   "admin": "also sets the workspace up: sources, approvals",
                   "delegate": "an app's backend acting for its own users, each seeing only their groups"},
    }


def steps(u: dict, base: str = "") -> dict:
    f = facts(u)
    url = mcp_url(base or "http://localhost:8080")
    admin = u["role"] in ("owner", "admin")
    if admin:
        items = [
            {"id": "name", "title": "Name your workspace", "done": f["named"], "to": "/settings",
             "body": "The name people see when they sign in and when Claude or ChatGPT asks them to allow access."},
            {"id": "source", "title": "Connect a source", "done": f["source"], "to": "/sources/documents",
             "body": "Connect GitHub or a Postgres database, or upload files. No data yet? Load the sample data.",
             "sample": not f["source"]},
            {"id": "build", "title": "Let it build", "done": f["built"] and not f["building"], "to": "/",
             "body": "Context Layer reads the sources, builds the search index and knowledge graph and drafts metrics. "
                     "Questions work while it builds.", "running": f["building"],
             "progress": {"passages": f["status"]["documents"]["passages"], "entities": f["status"]["graph"]["entities"],
                          "metrics": f["status"]["data"]["metrics"]}},
            {"id": "review", "title": "Review the inbox", "done": f["built"] and f["drafts"] == 0, "to": "/knowledge/inbox",
             "body": "New metrics, proposed graph types and facts Context Layer was unsure about wait for your approval "
                     "before agents use them.",
             "count": f["drafts"]},
            {"id": "connect", "title": "Connect an AI app", "done": f["connected"], "to": "/agents",
             "body": "Give Claude, ChatGPT, Cursor or your own app the MCP link. People sign in as themselves; apps and "
                     "scripts use a key.", "mcp_url": url, "snippets": connect_snippets(url)},
            {"id": "ask", "title": "Ask a test question", "done": f["asked"], "to": "/playground",
             "body": "Ask in the Playground and look at the evidence an agent would get: passages, numbers, links."},
        ]
    else:
        items = [
            {"id": "see", "title": "What you can see", "done": True, "to": "/sources/documents", "info": True,
             "body": "You see documents and numbers shared with your groups: " + ", ".join(u["groups"]) + "."},
            {"id": "connect", "title": "Connect Claude or ChatGPT", "done": f["my_app"], "to": "/agents",
             "body": "Add the MCP link as a connector and sign in with your own account; it sees what you can see.",
             "mcp_url": url, "snippets": {k: v for k, v in connect_snippets(url).items() if k in ("claude_web", "chatgpt")}},
            {"id": "ask", "title": "Ask your first question", "done": f["asked_me"], "to": "/playground",
             "body": "Try the Playground, or ask in Claude or ChatGPT once connected."},
        ]
    st = _state(u)
    left = [s for s in items if not s["done"]]
    return {"role": u["role"], "dismissed": bool(st.get("dismissed")), "show": not st.get("dismissed") and bool(left),
            "done": len(items) - len(left), "total": len(items), "steps": items}


def load_sample(by: str) -> dict:
    """'Try sample data': the shipped sales tables and their semantic layer, into an empty workspace."""
    from semantic import repo
    if not repo.is_empty():
        raise ValueError("This workspace already has definitions; sample data is only for an empty workspace.")
    from .seed import seed_first_workspace
    return seed_first_workspace(by, force=True)
