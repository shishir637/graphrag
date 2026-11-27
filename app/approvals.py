"""The approval queue: changes people asked for in a shared layer, plus drafts made by Context Layer itself.

  team change      someone in a team (not its admin) changed the team layer from chat -> the team's admins
                   (and admins of the teams above it, and company admins) approve or decline
  company change   someone who is not a publisher changed the company layer -> company admins
  metric draft     Context Layer drafted a metric from an uploaded table -> company admins (Metrics page)
  graph types      the AI model proposed knowledge-graph types -> company admins (Knowledge graph page)
Personal changes never come here: they apply to the person at once and are only recorded.
"""
from semantic import defstore

from . import teams


def _label(lid: str) -> str:
    from .context_tools import layer_label
    return layer_label(lid)


def _layers_for(agent: dict, team: str | None = None) -> list[str]:
    if team:
        return [teams.layer_id(team)] if teams.manages(agent, team) else []
    out = [teams.layer_id(t) for t in teams.managed_teams(agent)]
    if teams.is_company_admin(agent):
        out.append(defstore.COMPANY)
    return out


def _effect(r: dict) -> str:
    if r["kind"] == "metric" and r["body"]:
        try:
            from .context_tools import _effect as eff, _sets
            stack = (defstore.COMPANY,) + ((r["layer_id"],) if r["layer_id"] != defstore.COMPANY else ())
            cur = defstore.source_of("metric", r["key"], stack)
            return eff(r["key"], _sets(stack) if cur else None, _sets(stack, override=r["body"]))
        except Exception:                                  # noqa: BLE001 - effect is a nicety
            return ""
    return ""


def waiting_for(agent: dict, team: str | None = None, limit: int = 100) -> list[dict]:
    out = []
    if defstore.enabled():
        for r in defstore.requests(_layers_for(agent, team), "waiting", None, limit):
            out.append({"id": r["id"], "type": "company change" if r["layer_id"] == defstore.COMPANY else "team change",
                        "layer": r["layer_id"], "where": _label(r["layer_id"]), "kind": r["kind"], "key": r["key"],
                        "summary": r["summary"], "body": r["body"], "asked_by": r["by_user"], "app": r["app"],
                        "asked": r["created"], "effect": _effect(r), "can_decide": True})
    try:
        from . import doc_requests
        out += [r for r in doc_requests.waiting_for(agent) if team is None or r["where"] == f"the {team} team"]
    except Exception:                                      # noqa: BLE001
        pass
    if team is None and teams.is_company_admin(agent):
        try:
            from semantic.auto_model import pending_drafts
            for d in pending_drafts()[:20]:
                out.append({"id": f"draft:{d['name']}", "type": "metric draft", "where": "the company layer",
                            "key": d["name"], "summary": d.get("description") or d.get("label") or d["name"],
                            "asked_by": "Context Layer", "review": "/knowledge/metrics", "can_decide": False})
        except Exception:                                  # noqa: BLE001 - no drafts module function
            pass
        try:
            from graph.ontology import _canon, approved, proposal
            p = proposal()
            have = {_canon(e.get("name")).lower() for e in ((approved() or {}).get("entity_types") or [])}
            new = sorted({_canon(e["name"]) for e in (p or {}).get("entity_types", [])} - {""}
                         , key=str.lower)
            new = [n for n in new if n.lower() not in have]
            if new:
                out.append({"id": "ontology", "type": "graph types", "where": "the knowledge graph",
                            "key": "New kinds of things in the graph",
                            "summary": ", ".join(new[:8]), "asked_by": f"AI model ({p.get('model')})",
                            "review": "/knowledge/graph", "can_decide": False})
        except Exception:                                  # noqa: BLE001
            pass
    return out[:limit]


def history(agent: dict, status: str, limit: int = 100) -> list[dict]:
    from . import doc_requests
    out = _def_history(agent, status, limit) + doc_requests.history(agent, status, limit)
    return sorted(out, key=lambda r: r.get("decided") or "", reverse=True)[:limit]


def _def_history(agent: dict, status: str, limit: int = 100) -> list[dict]:
    if not defstore.enabled():
        return []
    return [{"id": r["id"], "type": "company change" if r["layer_id"] == defstore.COMPANY else "team change",
             "where": _label(r["layer_id"]), "kind": r["kind"], "key": r["key"], "summary": r["summary"],
             "asked_by": r["by_user"], "asked": r["created"], "decided_by": r["decided_by"], "decided": r["decided"],
             "reason": r["reason"]} for r in defstore.requests(_layers_for(agent), status, None, limit)]


def decide(agent: dict, rid: int, approve: bool, reason: str = "") -> dict:
    r = next((x for x in defstore.requests(None, "waiting") if x["id"] == int(rid)), None)
    if r is None:
        raise KeyError("That request is not waiting any more.")
    if r["layer_id"] not in _layers_for(agent):
        raise PermissionError("You do not manage the layer this change is for.")
    if r["layer_id"].startswith("team:"):
        t = teams.team_for_layer(r["layer_id"])
        if t:
            teams.ensure_layer(t)
    return defstore.decide(int(rid), approve, agent.get("email", ""), reason)
