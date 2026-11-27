"""Business context from chat: metrics, business rules and synonyms that people add or change through Claude
or ChatGPT. Every change lands in the caller's own layer (or the company layer, for people allowed to publish),
is recorded against their account, and is saved only after they say yes:

  1. the tool is called without `confirm`  -> nothing is saved; it returns the question to ask the person
     (what exists now, who defined it, what changes, the effect on a number) and a confirm code
  2. the tool is called again with confirm=<code> after the person agreed -> saved
Context Layer enforces this itself: a change cannot be saved without the code from step 1, by the same person.
"""
import re
from datetime import date, timedelta

from semantic import defstore

COMPANY = defstore.COMPANY


class Refused(ValueError):
    pass


# ------------------------------------------------------------------ who is calling, which layers they read

def own_layer(agent: dict) -> str | None:
    return f"user:{agent['user_id']}" if agent.get("kind") == "user" else None


def stack_for(agent: dict) -> tuple[str, ...]:
    """Company first, then the layers of the caller's teams (top team first), then layers shared with the caller
    that they chose to use, then their own (wins)."""
    stack = [COMPANY]
    try:
        from app import teams
        stack += [teams.layer_id(t) for t in teams.reading_teams(agent)]
    except Exception:                                      # noqa: BLE001 - no directory yet
        pass
    stack += [l for l in agent.get("using", []) if l != COMPANY]
    own = own_layer(agent)
    if own:
        stack.append(own)
    return tuple(dict.fromkeys(stack))


def who(agent: dict) -> str:
    return agent.get("email") or agent.get("name") or "agent"


def can_publish(agent: dict) -> bool:
    return agent.get("kind") == "user" and agent.get("role") in ("owner", "admin", "publisher")


def ensure_own(agent: dict) -> str:
    own = own_layer(agent)
    if not own:
        raise Refused("Changes need a person signed in to Context Layer (an agent key has no layer of its own).")
    defstore.ensure_layer(own, who(agent), f"{agent.get('name', '').split(' via ')[0] or who(agent)}'s layer")
    return own


def layer_label(layer_id: str) -> str:
    if layer_id == COMPANY:
        return "the company layer"
    if layer_id.startswith("team:"):
        from app import teams
        t = teams.team_for_layer(layer_id)
        return f"the {t} team layer" if t else layer_id
    lay = defstore.layer(layer_id) or {}
    if lay.get("name") and lay["name"] != layer_id:
        return lay["name"]
    if layer_id.startswith("user:") and layer_id[5:].isdigit():
        from app import core
        r = core.db().execute("SELECT name, email FROM users WHERE id = ?", (int(layer_id[5:]),)).fetchone()
        if r:
            return f"{r['name'] or r['email']}'s layer"
    return layer_id


def _target(agent: dict, scope: str) -> str:
    """The layer a change goes to. A change someone may not make directly (a team they do not manage, the company
    without being a publisher) becomes a request for that layer's admins: 'request:<layer>'."""
    scope = (scope or "mine").strip()
    if scope in ("mine", "own", "my layer"):
        return ensure_own(agent)
    if scope == "company":
        if can_publish(agent):
            return COMPANY
        ensure_own(agent)
        return f"request:{COMPANY}"
    from app import directory, teams
    name = scope.split(":", 1)[1] if scope.startswith("team:") else (None if scope == "team" else scope)
    mine = teams.member_teams(agent.get("email", ""))
    if name is None:
        if len(mine) != 1:
            raise Refused("Say which team: scope 'team:<team name>'." + (f" Your teams: {', '.join(mine)}." if mine else ""))
        name = mine[0]
    team = next((t for t in directory.team_names() if t.lower() == name.lower() or teams.slug(t) == teams.slug(name)), None)
    if not team:
        raise Refused(f"There is no team called '{name}'. scope is 'mine' (default), 'team:<team name>' or 'company'.")
    if not (teams.manages(agent, team) or team in teams.reading_teams(agent)):
        raise Refused(f"You are not in {team}: you cannot change its layer.")
    lid = teams.ensure_layer(team)
    return lid if teams.manages(agent, team) else f"request:{lid}"


def _where(target: str, long: bool = False) -> str:
    real = target[8:] if target.startswith("request:") else target
    if real == COMPANY:
        return "the company layer, for everyone" if long else "the company layer"
    if real.startswith("team:"):
        return f"{layer_label(real)}, for everyone in the team" if long else layer_label(real)
    return "your own layer: only your answers change" if long else "your own layer"


def locked(name: str) -> bool:
    return defstore.source_of("lock", name, (COMPANY,)) is not None


# ------------------------------------------------------------------ numbers before and after a change

def _effect(metric_name: str, before_sets, after_sets) -> str:
    """The metric's value last full month (or overall) under the old and the new definition."""
    from semantic.compiler import compile_query
    from semantic.db import connect
    from semantic.schema import SemanticQuery

    def value(model, mets):
        m = mets.get(metric_name)
        q = {"metrics": [metric_name]}
        if m.time_column:
            first = date.today().replace(day=1)
            last_month_end = first - timedelta(days=1)
            q |= {"start_date": last_month_end.replace(day=1), "end_date": last_month_end}
        sql = compile_query(SemanticQuery.model_validate(q), model, mets).sql
        df = connect().execute(sql).df()
        return None if df.empty else df.iloc[0][metric_name]

    def fmt(v):
        if v is None:
            return "no value"
        try:
            v = float(v)
        except (TypeError, ValueError):
            return str(v)
        return f"{v:,.2f}".rstrip("0").rstrip(".") if abs(v) < 1000 else f"{v:,.0f}"

    try:
        old = value(*before_sets) if before_sets else None
        new = value(*after_sets)
    except Exception as e:                                 # noqa: BLE001 - only a preview
        return f"(could not compute a preview: {e})"
    when = "last month" if after_sets[1].get(metric_name).time_column else "overall"
    if before_sets is None:
        return f"Its value {when}: {fmt(new)}."
    return f"{when.capitalize()}: {fmt(old)} becomes {fmt(new)}."


def _sets(stack, override: dict | None = None, drop: str | None = None):
    from semantic.schema import MetricSet, SemanticModel
    with defstore.acting(defstore.ACTOR.get()[0], defstore.ACTOR.get()[1], stack=stack):
        model = defstore.compose("model.yaml")
        mets = defstore.compose("metrics.yaml")
    ms = [m for m in mets["metrics"] if m["name"] != drop and (not override or m["name"] != override["name"])]
    if override:
        ms.append(override)
    return SemanticModel.model_validate(model), MetricSet.model_validate(mets | {"metrics": ms})


# ------------------------------------------------------------------ metrics

def propose_metric(agent: dict, name: str, label: str | None = None, description: str | None = None,
                   expression: str | None = None, type: str | None = None, entity: str | None = None,
                   numerator: str | None = None, denominator: str | None = None, filters: list[str] | None = None,
                   time_column: str | None = None, format: str | None = None, scope: str = "mine",
                   confirm: str | None = None) -> dict:
    from semantic.compiler import CompileError
    from semantic.schema import Metric
    if confirm:
        return _confirm(agent, confirm)
    name = re.sub(r"[^a-z0-9_]+", "_", (name or "").strip().lower()).strip("_")
    if not name:
        raise Refused("Give the metric a name, e.g. repeat_customer_rate.")
    target = _target(agent, scope)
    stack = stack_for(agent)
    cur = defstore.source_of("metric", name, stack)
    if cur and locked(name) and target != COMPANY:
        raise Refused(f"'{name}' is locked by the company: only a publisher or admin can change it, in the company layer.")
    base = dict(cur["body"]) if cur else {}
    given = {k: v for k, v in {"label": label, "description": description, "expression": expression, "type": type,
                               "entity": entity, "numerator": numerator, "denominator": denominator,
                               "filters": filters, "time_column": time_column, "format": format}.items() if v is not None}
    if not given:
        raise Refused("Say what to change: the formula (expression), filters, label, description...")
    body = base | given | {"name": name}
    body.setdefault("type", "simple")
    body.setdefault("label", name.replace("_", " ").capitalize())
    body.setdefault("description", body["label"])
    if "entity" not in body:
        raise Refused("Say which table the metric is counted on (entity), e.g. 'orders'. describe_data lists them.")
    body["status"] = "approved"
    body["owner"] = who(agent)
    try:
        Metric.model_validate(body)
        before = _sets(stack) if cur else None
        after = _sets(stack, override=body)
        from semantic.compiler import compile_query
        from semantic.schema import SemanticQuery
        sql = compile_query(SemanticQuery(metrics=[name]), *after).sql
        from semantic.db import connect
        connect().execute(f"SELECT * FROM ({sql}) AS checked LIMIT 0")    # columns and syntax really work
    except CompileError as e:
        raise Refused(f"That definition does not work: {e}") from e
    except ValueError as e:
        raise Refused(f"That definition is not valid: {e}") from e
    except Exception as e:                                 # noqa: BLE001 - DuckDB: unknown column, bad SQL...
        raise Refused(f"That definition does not run: {str(e).splitlines()[0][:300]}") from e
    effect = _effect(name, before, after)
    where = _where(target, True)
    if cur:
        defined = f"{layer_label(cur['layer'])}" + (f" (last changed by {cur['by']}, {str(cur['at'])[:10]})" if cur.get('by') else "")
        what = _diff_words(cur["body"], body)
        q = (f"'{body['label']}' ({name}) is currently defined by {defined}. You are changing {what}, in {where}. "
             f"{effect} Are you sure?")
        if cur["layer"] not in (COMPANY, target):
            q += f" This overrides {layer_label(cur['layer'])} for you only."
    else:
        q = f"Add a new metric '{body['label']}' ({name}) to {where}? {effect}"
    return _hold(agent, target, "metric", name, body, q)


def _diff_words(old: dict, new: dict) -> str:
    parts = []
    for k in ("expression", "filters", "numerator", "denominator", "time_column", "entity", "label", "description",
              "format", "type"):
        if old.get(k) != new.get(k):
            parts.append(f"{k} from {old.get(k)!r} to {new.get(k)!r}")
    return "; ".join(parts) or "nothing"


# ------------------------------------------------------------------ business rules and synonyms

def add_business_rule(agent: dict, rule: str, name: str | None = None, scope: str = "mine",
                      confirm: str | None = None) -> dict:
    if confirm:
        return _confirm(agent, confirm)
    rule = " ".join((rule or "").split())
    if len(rule) < 8:
        raise Refused("Write the rule as a full sentence, e.g. 'Our fiscal year starts on 1 April.'")
    key = re.sub(r"[^a-z0-9]+", "-", (name or rule[:48]).lower()).strip("-")
    target = _target(agent, scope)
    cur = defstore.source_of("rule", key, stack_for(agent))
    where = _where(target)
    if cur:
        q = (f"The rule '{key}' currently says (from {layer_label(cur['layer'])}): \"{cur['body']['text']}\". "
             f"Change it to \"{rule}\" in {where}? Are you sure?")
    else:
        q = f"Add this business rule to {where}: \"{rule}\"?"
    return _hold(agent, target, "rule", key, {"text": rule}, q)


def add_synonym(agent: dict, term: str, means: str, scope: str = "mine", confirm: str | None = None) -> dict:
    if confirm:
        return _confirm(agent, confirm)
    term = " ".join((term or "").split())
    means = (means or "").strip()
    if not term or not means:
        raise Refused("Give the word and what it means: a metric name (gross_revenue), a dimension "
                      "(customers.city) or a value (customers.city=Mumbai).")
    stack = stack_for(agent)
    with defstore.acting(who(agent), "chat", stack=stack):
        mets = {m["name"] for m in defstore.compose("metrics.yaml")["metrics"]}
        dims = {f"{e['name']}.{d['name']}" for e in defstore.compose("model.yaml")["entities"] for d in e.get("dimensions", [])}
    if means not in mets and means.split("=")[0] not in dims:
        raise Refused(f"'{means}' is not a metric or dimension. Use describe_data to see the names.")
    target = _target(agent, scope)
    key = f"{term.lower()}>{means}"
    where = _where(target)
    return _hold(agent, target, "synonym", key, {"term": term, "target": means},
                 f"Add '{term}' as a word for {means}, in {where}?")


# ------------------------------------------------------------------ confirm, history, undo

def _hold(agent, layer_id, kind, key, body, question) -> dict:
    if layer_id.startswith("request:"):
        real = layer_id[8:]
        question = (question.replace("your own layer: only your answers change", layer_label(real))
                    .replace("in your own layer", f"in {layer_label(real)}").replace("the company layer, for everyone", layer_label(real))
                    + f" You cannot change {layer_label(real)} yourself, so this goes to its admins for approval;"
                      " nothing changes until they approve.")
    code = defstore.hold(layer_id, kind, key, body, who(agent), question)
    return {"saved": False, "confirm_code": code, "ask_the_person": question,
            "next": "Ask the person this question in your own words. Only if they clearly say yes, call this tool "
                    "again with confirm set to the confirm_code. If they say no, do nothing."}


def _confirm(agent: dict, code: str) -> dict:
    try:
        r = defstore.confirm(code, who(agent))
    except KeyError as e:
        raise Refused(str(e)) from e
    if r.get("requested"):
        r["layer"] = layer_label(r["layer"])
        r["note"] = (f"Sent to the admins of {r['layer']} for approval (request {r['request_id']}). Nothing changes "
                     "until they approve; my_changes shows whether it was approved.")
        return r
    if r.get("layer"):
        r["layer"] = layer_label(r["layer"])
    r["note"] = ("Saved. Answers that use it say which layer the definition came from."
                 + (" Metric questions may take a minute to pick it up." if r["item"].startswith("metric") else ""))
    return r


def my_changes(agent: dict) -> dict:
    own = own_layer(agent)
    try:
        from . import doc_requests
        files = doc_requests.mine(agent)
    except Exception:                                      # noqa: BLE001
        files = []
    if not own or not defstore.layer(own):
        return {"changes": [], "items": [], "file_requests": files,
                "note": "You have not changed any definitions yet."}
    return {"file_requests": files, "items": [i for i in defstore.items(own) if i["kind"] != "meta"],
            "changes": [_h(h) for h in defstore.history([own], limit=30)],
            "requests": [{"id": r["id"], "layer": layer_label(r["layer_id"]), "item": f"{r['kind']} {r['key']}",
                          "status": r["status"], "asked": r["created"], "decided_by": r["decided_by"],
                          "reason": r["reason"]} for r in defstore.requests(None, None, who(agent), 20)]}


def _h(h: dict) -> dict:
    return {"change_id": h["id"], "item": f"{h['kind']} {h['key']}", "action": h["action"], "by": h["by_user"],
            "app": h["app"], "at": h["at"], "layer": layer_label(h["layer_id"]), "undone": bool(h["undone_by"]),
            "before": h["before"], "after": h["after"]}


def undo_change(agent: dict, change_id: int) -> dict:
    h = next((x for x in defstore.history(limit=100000) if x["id"] == int(change_id)), None)
    if h is None:
        raise Refused("No change with that id. my_changes lists yours.")
    if h["layer_id"] != own_layer(agent) and not (h["layer_id"] == COMPANY and can_publish(agent)):
        raise Refused("You can only undo changes in your own layer (publishers and admins: also the company layer).")
    try:
        r = defstore.undo(int(change_id))
        if isinstance(r, dict) and r.get("layer"):
            r["layer"] = layer_label(r["layer"])
        return r
    except (KeyError, ValueError) as e:
        raise Refused(str(e)) from e


def who_changed(agent: dict, name: str) -> dict:
    """History of one metric, rule or synonym in the layers the caller reads."""
    stack = list(stack_for(agent))
    name = name.strip()
    out = []
    for kind in ("metric", "rule", "synonym"):
        for h in defstore.history(stack, kind=kind, limit=200):
            if h["key"] == name or (kind == "synonym" and h["key"].split(">")[0] == name.lower()):
                out.append(_h(h))
    cur = None
    for kind in ("metric", "rule"):
        s = defstore.source_of(kind, name, tuple(stack))
        if s:
            cur = {"kind": kind, "from": layer_label(s["layer"]), "by": s["by"], "at": s["at"], "definition": s["body"]}
    return {"name": name, "current": cur, "history": out[:50]}


def sources(names: list[str], agent_stack: tuple[str, ...]) -> list[dict]:
    """Which layer each metric used in an answer comes from."""
    out = []
    for n in names:
        s = defstore.source_of("metric", n, agent_stack)
        if s:
            out.append({"metric": n, "from": layer_label(s["layer"]), "by": s["by"], "at": str(s["at"])[:10]})
    return out


def rules(stack: tuple[str, ...]) -> list[dict]:
    rows = defstore._rows(stack, {"rule"})
    return [{"rule": r["key"], "text": __import__("json").loads(r["body"])["text"], "from": layer_label(r["layer_id"])}
            for r in rows.values() if not r["removed"] and r["body"]]
