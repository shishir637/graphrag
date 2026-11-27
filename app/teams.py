"""Teams as a hierarchy, with team admins and a layer per team.

  tree         teams have a parent (company -> department -> team); the directory keeps it (teams.parent)
  admins       company admins (role owner or admin) manage every team; a team admin manages their team and every
               team under it, never a parent or a sibling team
  team layer   each team has its own layer of definitions and files, "team:<slug>". Its members read it, and so
               do members of the teams under it; people and teams the team admin adds to the team link read it too
  answers      company layer -> team layers (top team first) -> layers shared with the person -> their own layer
  link         /mcp/teams/<slug> (also /mcp/layers/<slug>): the company link with the team layer added, for anyone
               allowed to read it
"""
import re

from . import core, directory

SCHEMA = """
CREATE TABLE IF NOT EXISTS team_admins (team TEXT NOT NULL, email TEXT NOT NULL, added_by TEXT, added TEXT NOT NULL,
  PRIMARY KEY (team, email));
"""
_ready: str | None = None


def db():
    directory.db()
    return core.db_with(SCHEMA)


def slug(team: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", team.lower()).strip("-")[:60] or "team"


def layer_id(team: str) -> str:
    return f"team:{slug(team)}"


def team_for_layer(lid: str) -> str | None:
    if not lid.startswith("team:"):
        return None
    s = lid[5:]
    return next((t for t in directory.team_names() if slug(t) == s), None)


def _parents() -> dict[str, str | None]:
    return {r["name"]: r["parent"] for r in directory.db().execute("SELECT name, parent FROM teams")}


def ancestors(team: str) -> list[str]:
    """The team's parents, nearest first (stops at a loop)."""
    par, out, cur = _parents(), [], team
    while par.get(cur) and par[cur] not in out and par[cur] != team:
        cur = par[cur]
        out.append(cur)
    return out


def descendants(team: str) -> list[str]:
    par = _parents()
    out, todo = [], [team]
    while todo:
        t = todo.pop()
        for k, p in par.items():
            if p == t and k not in out and k != team:
                out.append(k)
                todo.append(k)
    return sorted(out)


def check_parent(team: str, parent: str | None):
    if parent and (parent == team or parent in descendants(team)):
        raise ValueError(f"{parent} is under {team}: a team cannot sit under its own sub-team.")


# ------------------------------------------------------------------ admins

def is_company_admin(agent: dict) -> bool:
    return agent.get("kind") == "user" and agent.get("role") in ("owner", "admin")


def managed_teams_direct(email: str) -> list[str]:
    """Teams this person was made admin of (not counting teams under them)."""
    return [r["team"] for r in db().execute("SELECT team FROM team_admins WHERE email = ?", ((email or "").lower(),))]


def admins_of(team: str) -> list[str]:
    return [r["email"] for r in db().execute("SELECT email FROM team_admins WHERE team = ? ORDER BY email", (team,))]


def admin_teams(email: str) -> list[str]:
    """Teams this person administers directly."""
    return [r["team"] for r in db().execute("SELECT team FROM team_admins WHERE email = ?", ((email or "").lower(),))]


def manages(agent: dict, team: str) -> bool:
    if is_company_admin(agent):
        return True
    if agent.get("kind") != "user":
        return False
    mine = set(admin_teams(agent.get("email", "")))
    return bool(mine & ({team} | set(ancestors(team))))


def managed_teams(agent: dict) -> list[str]:
    if is_company_admin(agent):
        return directory.team_names()
    out = set()
    for t in admin_teams(agent.get("email", "")):
        out |= {t} | set(descendants(t))
    return sorted(x for x in out if x in set(directory.team_names()))


def set_admin(agent: dict, team: str, email: str, on: bool = True):
    """Company admins appoint admins of any team; a team admin appoints admins of the teams UNDER theirs."""
    email = (email or "").strip().lower()
    if team not in directory.team_names():
        raise ValueError(f"There is no team called '{team}'.")
    allowed = is_company_admin(agent) or any(manages(agent, p) for p in ancestors(team))
    if not allowed:
        raise PermissionError("Only a company admin, or an admin of a team above it, can change this team's admins.")
    if on:
        if not directory.db().execute("SELECT 1 FROM people WHERE email = ?", (email,)).fetchone() and \
                not core.db().execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
            raise ValueError(f"{email} is not in the directory. Add them first.")
        db().execute("INSERT INTO team_admins VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                     (team, email, agent.get("email"), core.now()))
    else:
        db().execute("DELETE FROM team_admins WHERE team = ? AND email = ?", (team, email))


# ------------------------------------------------------------------ membership and layers

def member_teams(email: str) -> list[str]:
    return directory.teams_of(email)


def reading_teams(agent: dict) -> list[str]:
    """Teams whose layer this person reads by membership: their teams and the teams above them, top first."""
    if agent.get("kind") != "user":
        return []
    out = []
    for t in member_teams(agent.get("email", "")):
        for x in list(reversed(ancestors(t))) + [t]:
            if x not in out:
                out.append(x)
    return out


def can_read_team(agent: dict, team: str) -> bool:
    if agent.get("kind") != "user":
        return False
    if team in reading_teams(agent) or manages(agent, team):
        return True
    from . import sharing
    return sharing.shared_to(agent, layer_id(team))


def ensure_layer(team: str) -> str:
    from semantic import defstore
    lid = layer_id(team)
    defstore.ensure_layer(lid, "team", f"{team} team layer", kind="team")
    return lid


def summary(team: str, agent: dict | None = None, base: str = "") -> dict:
    t = next((x for x in directory.list_teams() if x["name"] == team), None)
    if not t:
        raise ValueError(f"There is no team called '{team}'.")
    from . import sharing
    lid = layer_id(team)
    out = {**t, "slug": slug(team), "layer": lid, "admins": admins_of(team), "parents": ancestors(team),
           "children": [x["name"] for x in directory.list_teams() if x.get("parent") == team],
           "link": f"{base}/mcp/teams/{sharing.slug_for(lid)}" if base else None,
           "link_extra": sharing.share_targets(lid)}
    if agent is not None:
        out["you_manage"] = manages(agent, team)
    return out


def tree() -> list[dict]:
    """Teams as a list in tree order with depth, for the admin page."""
    rows = directory.list_teams()
    kids: dict = {}
    for r in rows:
        kids.setdefault(r.get("parent") or None, []).append(r)
    names = {r["name"] for r in rows}
    out = []

    def walk(parent, depth):
        for r in sorted(kids.get(parent, []), key=lambda x: x["name"]):
            out.append({**r, "depth": depth, "admins": admins_of(r["name"]), "slug": slug(r["name"])})
            walk(r["name"], depth + 1)
    walk(None, 0)
    for r in rows:                                       # parents that are not teams themselves
        if r.get("parent") and r["parent"] not in names and r["name"] not in {o["name"] for o in out}:
            out.append({**r, "depth": 0, "admins": admins_of(r["name"]), "slug": slug(r["name"])})
    return out
