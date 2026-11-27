"""Sharing a person's layer, from chat.

  share_my_layer   who may use it: everyone in the company (default), named people, or teams (access groups).
                   Returns the layer's own MCP link: /mcp/layers/<name>. The link alone gives nothing: the person
                   signs in to Context Layer, and the share list decides whether they get in.
  use_layer        inside the company connector, answer from a layer shared with you (no new link needed)
  revoke_share     stop sharing with someone, a team, everyone, or all
  publish_change   (publishers and admins) make an item of a personal layer the company definition; recorded,
                   with the person who made it, and the old company version kept in history
Shares and choices live in the definitions store (Postgres), next to the layers.
"""
import re

from semantic import defstore

from . import core
from .context_tools import COMPANY, Refused, can_publish, ensure_own, layer_label, own_layer, who


def _tables(x):
    serial = "INTEGER PRIMARY KEY AUTOINCREMENT" if x.sqlite else "BIGSERIAL PRIMARY KEY"
    x.q(f"CREATE TABLE IF NOT EXISTS shares (id {serial}, layer_id TEXT NOT NULL, kind TEXT NOT NULL, target TEXT NOT NULL,"
        " created_by TEXT, created TEXT NOT NULL, revoked TEXT)")
    x.q("CREATE TABLE IF NOT EXISTS layer_use (user_id INTEGER NOT NULL, layer_id TEXT NOT NULL, since TEXT NOT NULL,"
        " PRIMARY KEY (user_id, layer_id))")
    x.q("CREATE TABLE IF NOT EXISTS layer_slugs (slug TEXT PRIMARY KEY, layer_id TEXT NOT NULL UNIQUE)")


def _db():
    x = defstore.db()
    _tables(x)
    return x


# ------------------------------------------------------------------ links

def slug_for(layer_id: str) -> str:
    x = _db()
    r = x.one("SELECT slug FROM layer_slugs WHERE layer_id = ?", (layer_id,))
    if r:
        return r["slug"]
    base = layer_id[5:] if layer_id.startswith("team:") else \
        re.sub(r"[^a-z0-9]+", "-", layer_label(layer_id).lower().replace("'s layer", "")).strip("-") or "layer"
    slug, n = base, 1
    while x.one("SELECT 1 AS y FROM layer_slugs WHERE slug = ?", (slug,)):
        n += 1
        slug = f"{base}-{n}"
    x.q("INSERT INTO layer_slugs VALUES (?, ?)", (slug, layer_id))
    return slug


def layer_for_slug(slug: str) -> str | None:
    r = _db().one("SELECT layer_id FROM layer_slugs WHERE slug = ?", (slug,))
    return r["layer_id"] if r else None


def link(base: str, layer_id: str) -> str:
    return f"{base}/mcp/layers/{slug_for(layer_id)}"


# ------------------------------------------------------------------ who may use a layer

def _owner_of(layer_id: str) -> int | None:
    return int(layer_id.split(":", 1)[1]) if layer_id.startswith("user:") else None


def can_use(agent: dict, layer_id: str) -> bool:
    if layer_id == COMPANY or layer_id == own_layer(agent):
        return True
    if agent.get("kind") != "user":
        return False
    if layer_id.startswith("team:"):
        from . import teams
        t = teams.team_for_layer(layer_id)
        return bool(t) and teams.can_read_team(agent, t)
    return shared_to(agent, layer_id)


def shared_to(agent: dict, layer_id: str) -> bool:
    """On the layer's share list: everyone, the person, or one of their teams (or a team above theirs)."""
    if agent.get("kind") != "user":
        return False
    groups = set(agent.get("base_groups") or agent.get("groups") or [])
    try:
        from . import teams
        for t in list(groups):
            groups |= set(teams.ancestors(t))
    except Exception:                                      # noqa: BLE001 - no directory
        pass
    for s in _db().all("SELECT kind, target FROM shares WHERE layer_id = ? AND revoked IS NULL", (layer_id,)):
        if s["kind"] == "everyone" or (s["kind"] == "person" and s["target"] == agent.get("email", "").lower()) \
                or (s["kind"] == "team" and s["target"] in groups):
            return True
    return False


def shared_with_me(agent: dict) -> list[str]:
    ids = {r["layer_id"] for r in _db().all("SELECT DISTINCT layer_id FROM shares WHERE revoked IS NULL")}
    return sorted(l for l in ids if l != own_layer(agent) and can_use(agent, l))


def layers_in_use(agent: dict) -> list[str]:
    if agent.get("kind") != "user" or not defstore.enabled():
        return []
    rows = _db().all("SELECT layer_id FROM layer_use WHERE user_id = ? ORDER BY since", (agent["user_id"],))
    return [r["layer_id"] for r in rows if can_use(agent, r["layer_id"])]


# ------------------------------------------------------------------ people and teams by name

def _people(names: list[str]) -> tuple[list[str], list[str]]:
    """Names or emails -> (emails with an account, emails without one yet). Names are looked up among accounts and
    in the company directory; unclear names are refused with the choices."""
    accounts = {r["email"].lower(): r["name"] for r in core.db().execute("SELECT email, name FROM users WHERE disabled = 0")}
    people = dict(accounts)
    try:
        from . import directory
        for p in directory.list_people():
            people.setdefault(p["email"].lower(), p["name"])
    except Exception:                                      # noqa: BLE001 - directory not set up yet
        pass
    found, unknown = [], []
    for raw in names:
        n = " ".join(raw.strip().lower().split())
        if not n:
            continue
        if n in people or "@" in n:
            (found if n in accounts else unknown).append(n)
            continue
        hits = [e for e, name in people.items() if n == name.lower() or n in name.lower().split()
                or e.split("@")[0] == n or (len(n) > 3 and name.lower().startswith(n))]
        if len(hits) > 1:
            raise Refused(f"More than one person matches '{raw}': " + ", ".join(f"{people[e]} ({e})" for e in hits[:10])
                          + ". Ask which one, then use their email.")
        if not hits:
            raise Refused(f"No one called '{raw}' is in the company directory or has a Context Layer account. "
                          "Ask for their work email.")
        (found if hits[0] in accounts else unknown).append(hits[0])
    return found, unknown


def _teams(names: list[str]) -> list[str]:
    from . import engine
    known = {g.lower(): g for g in engine.all_groups() if not g.startswith("layer:")}
    try:
        from . import directory
        known |= {t.lower(): t for t in directory.team_names()}
    except Exception:                                      # noqa: BLE001 - directory not set up yet
        pass
    words = {}                                             # everyday words for teams ("data team", "SRE")
    try:
        from semantic.yamlio import load_enrichment
        for syn in load_enrichment().synonyms:
            ref, _, val = syn.target.partition("=")
            if ref == "teams.team_name" and val.lower() in known:
                words[syn.term.lower()] = known[val.lower()]
    except Exception:                                      # noqa: BLE001 - no definitions yet
        pass
    out = []
    for raw in names:
        k = raw.strip().lower().removesuffix(" team").strip()
        hit = known.get(k) or words.get(raw.strip().lower()) or words.get(k) or \
            next((v for kk, v in known.items() if kk.replace("-", " ") == k), None)
        if not hit:
            raise Refused(f"There is no team called '{raw}'. Teams: {', '.join(sorted(known.values())) or 'none yet'}.")
        out.append(hit)
    return out


# ------------------------------------------------------------------ tools

def share_my_layer(agent: dict, people: list[str] | None = None, teams: list[str] | None = None,
                   everyone: bool | None = None, base: str = "") -> dict:
    layer_id = ensure_own(agent)
    x = _db()
    people, teams = people or [], teams or []
    if everyone is None:
        everyone = not people and not teams
    emails, invite = _people(people)
    team_names = _teams(teams)
    added = []
    targets = ([("everyone", "*")] if everyone else []) + [("person", e) for e in emails + invite] + \
              [("team", t) for t in team_names]
    for kind, target in targets:
        if not x.one("SELECT id FROM shares WHERE layer_id = ? AND kind = ? AND target = ? AND revoked IS NULL",
                     (layer_id, kind, target)):
            x.q("INSERT INTO shares (layer_id, kind, target, created_by, created) VALUES (?, ?, ?, ?, ?)",
                (layer_id, kind, target, who(agent), defstore.now()))
            added.append(f"{kind} {target}" if kind != "everyone" else "everyone in the company")
    core.log_event("user", who(agent), "layer.share", layer_id, {"added": added})
    out = {"link": link(base, layer_id), "layer": layer_label(layer_id), "shared_with": _list(layer_id),
           "next": "Send the link. The person adds it in Claude (Settings, Connectors, Add custom connector) or "
                   "ChatGPT, and signs in to Context Layer. People it is shared with can also say 'use "
                   f"{layer_label(layer_id)}' in their company connector."}
    if invite:
        if agent.get("role") in ("owner", "admin"):
            out["invites"] = [_invite(agent, e, base) for e in invite]
        else:                                  # only admins add people to Context Layer
            out["no_account_yet"] = invite
            out["note"] = ("These people have no Context Layer account yet. The share is kept: they get access once an "
                           "admin adds them (admin console, Directory, Invite). Tell the person to ask an admin.")
    return out


def _invite(agent: dict, email: str, base: str) -> dict:
    try:
        from . import directory
        return directory.invite(email, by=who(agent), base=base)
    except (ImportError, AttributeError):
        return {"email": email, "note": "No account yet: ask an admin to add them."}


def add_shares(layer_id: str, by: str, people: list[str] | None = None, teams: list[str] | None = None,
               everyone: bool = False) -> list[str]:
    """Add people (names or emails) and teams to a layer's share list; returns what was added."""
    x = _db()
    emails, unknown = _people(people or [])
    targets = ([("everyone", "*")] if everyone else []) + [("person", e) for e in emails + unknown] + \
              [("team", t) for t in _teams(teams or [])]
    added = []
    for kind, target in targets:
        if not x.one("SELECT id FROM shares WHERE layer_id = ? AND kind = ? AND target = ? AND revoked IS NULL",
                     (layer_id, kind, target)):
            x.q("INSERT INTO shares (layer_id, kind, target, created_by, created) VALUES (?, ?, ?, ?, ?)",
                (layer_id, kind, target, by, defstore.now()))
            added.append(f"{kind}: {target}" if kind != "everyone" else "everyone in the company")
    return added


def remove_share(layer_id: str, kind: str, target: str) -> int:
    x = _db()
    rows = x.all("SELECT id FROM shares WHERE layer_id = ? AND kind = ? AND target = ? AND revoked IS NULL",
                 (layer_id, kind, target))
    for r in rows:
        x.q("UPDATE shares SET revoked = ? WHERE id = ?", (defstore.now(), r["id"]))
    return len(rows)


def share_targets(layer_id: str) -> list[dict]:
    return [{"kind": s["kind"], "target": s["target"]} for s in
            _db().all("SELECT kind, target FROM shares WHERE layer_id = ? AND revoked IS NULL ORDER BY id", (layer_id,))]


def _list(layer_id: str) -> list[str]:
    out = []
    for s in _db().all("SELECT kind, target FROM shares WHERE layer_id = ? AND revoked IS NULL ORDER BY id", (layer_id,)):
        out.append("everyone in the company" if s["kind"] == "everyone" else f"{s['kind']}: {s['target']}")
    return out


def sharing_status(agent: dict, base: str = "") -> dict:
    own = own_layer(agent)
    mine = None
    if own and defstore.layer(own):
        mine = {"layer": layer_label(own), "link": link(base, own), "shared_with": _list(own) or ["no one yet"]}
    using = layers_in_use(agent)
    return {"my_layer": mine,
            "shared_with_me": [{"layer": layer_label(l), "in_use": l in using, "link": link(base, l)}
                               for l in shared_with_me(agent)],
            "answers_use": [layer_label(l) for l in [COMPANY] + using + ([own] if own and defstore.layer(own) else [])]}


def use_layer(agent: dict, name: str, use: bool = True) -> dict:
    if agent.get("kind") != "user":
        raise Refused("Only people signed in to Context Layer can choose layers.")
    choices = {layer_label(l).lower(): l for l in shared_with_me(agent)}
    k = name.strip().lower()
    hit = choices.get(k) or next((l for lab, l in choices.items() if k in lab or k == lab.split("'")[0]), None)
    if not hit:
        raise Refused("No layer by that name is shared with you. Shared with you: "
                      + (", ".join(layer_label(l) for l in choices.values()) or "none"))
    x = _db()
    if use:
        if not x.one("SELECT 1 AS y FROM layer_use WHERE user_id = ? AND layer_id = ?", (agent["user_id"], hit)):
            x.q("INSERT INTO layer_use VALUES (?, ?, ?)", (agent["user_id"], hit, defstore.now()))
    else:
        x.q("DELETE FROM layer_use WHERE user_id = ? AND layer_id = ?", (agent["user_id"], hit))
    return {"layer": layer_label(hit), "in_use": use,
            "note": f"Your answers now use {layer_label(hit)} on top of the company layer; your own changes still come first."
            if use else f"Your answers no longer use {layer_label(hit)}."}


def revoke_share(agent: dict, people: list[str] | None = None, teams: list[str] | None = None,
                 everyone: bool = False, all: bool = False) -> dict:
    layer_id = own_layer(agent)
    if not layer_id or not defstore.layer(layer_id):
        raise Refused("You have not shared a layer.")
    x = _db()
    conds = []
    if all:
        conds.append(("1 = 1", ()))
    if everyone:
        conds.append(("kind = 'everyone'", ()))
    for e in (_people(people or [])[0] + [p.lower() for p in (people or []) if "@" in p]):
        conds.append(("kind = 'person' AND target = ?", (e,)))
    for t in _teams(teams or []):
        conds.append(("kind = 'team' AND target = ?", (t,)))
    if not conds:
        raise Refused("Say who to stop sharing with: people, teams, everyone, or all.")
    n = 0
    for cond, args in conds:
        n += x.q(f"UPDATE shares SET revoked = ? WHERE layer_id = ? AND revoked IS NULL AND {cond}",
                 (defstore.now(), layer_id, *args)).rowcount or 0
    core.log_event("user", who(agent), "layer.revoke", layer_id, {"removed": n})
    return {"removed": n, "shared_with": _list(layer_id) or ["no one"]}


def publish_change(agent: dict, name: str, kind: str = "metric", confirm: str | None = None,
                   to: str = "company") -> dict:
    """Move a personal metric or rule into the company layer, or into a team's layer (to='team:<name>').
    Who may not do that directly sends it to that layer's admins for approval."""
    from .context_tools import _confirm, _hold, _target, stack_for
    if kind in ("file", "document"):
        from . import doc_requests
        try:
            return doc_requests.share(agent, name, to or "company", confirm)
        except doc_requests.Refused as e:
            raise Refused(str(e)) from e
    if confirm:
        return _confirm(agent, confirm)
    target = _target(agent, to or "company")
    real = target[8:] if target.startswith("request:") else target
    own = own_layer(agent)
    stack = tuple(dict.fromkeys(list(stack_for(agent)) + layers_in_use(agent) + ([own] if own else [])))
    src = defstore.source_of(kind, name.strip(), stack)
    if not src or src["layer"] in (COMPANY, real):
        raise Refused(f"There is no personal {kind} '{name}' to publish in the layers you use.")
    cur = defstore.source_of(kind, name.strip(), (real,))
    q = (f"Publish {layer_label(src['layer'])}'s {kind} '{name}' to {layer_label(real)}"
         + (", for everyone? " if real == COMPANY else ", for everyone in the team? ")
         + (f"It replaces the current version (last changed by {cur['by']}, {str(cur['at'])[:10]}); "
            "the old one stays in history." if cur else "It is new there."))
    return _hold(agent, target, kind, name.strip(), src["body"], q)
