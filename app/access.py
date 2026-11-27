"""Who can see which data, and which connectors they can use (v0.8).

  data sets    a label on files ("finance", "hr-policies"). Built in: "public" (Everyone) and one per team (the team's
               slug). Files can carry several. Personal files ("layer:<id>") stay private to their owner and the
               people they share with; this module leaves them alone.
  connectors   what Claude/ChatGPT may use for the person: documents, data (metrics and tables), graph, upload.
  roles        named bundles of data sets and connectors ("Finance reader"). The built-in role "Everyone" applies
               to every account; admins edit it to change the baseline.
  two ways     team access: a team's grants (its own data set + data sets, connectors and roles given to the team)
               apply to its members and to members of the teams under it.
               unique access: roles, data sets and connectors given to one person.
  effective    Everyone + every team they read + their roles + their unique grants (a union). Company admins see all.
"""
import json

from . import core, directory, teams

CONNECTORS = {"documents": "Documents", "data": "Metrics and tables", "graph": "Knowledge graph",
              "upload": "Add files from chat"}
EVERYONE = "Everyone"
PUBLIC = "public"

SCHEMA = """
CREATE TABLE IF NOT EXISTS ac_datasets (name TEXT PRIMARY KEY, description TEXT NOT NULL DEFAULT '', created TEXT,
  by_user TEXT);
CREATE TABLE IF NOT EXISTS ac_roles (name TEXT PRIMARY KEY, description TEXT NOT NULL DEFAULT '',
  datasets TEXT NOT NULL DEFAULT '[]', connectors TEXT NOT NULL DEFAULT '[]', created TEXT, by_user TEXT);
CREATE TABLE IF NOT EXISTS ac_team (team TEXT PRIMARY KEY, datasets TEXT NOT NULL DEFAULT '[]',
  connectors TEXT NOT NULL DEFAULT '[]', roles TEXT NOT NULL DEFAULT '[]', updated TEXT, by_user TEXT);
CREATE TABLE IF NOT EXISTS ac_user (email TEXT PRIMARY KEY, datasets TEXT NOT NULL DEFAULT '[]',
  connectors TEXT NOT NULL DEFAULT '[]', roles TEXT NOT NULL DEFAULT '[]', updated TEXT, by_user TEXT);
"""
_ready: str | None = None


def db():
    global _ready
    c = core.db()
    if _ready != str(core.DB_PATH):
        c.executescript(SCHEMA)
        if not c.execute("SELECT 1 FROM ac_roles WHERE name = ?", (EVERYONE,)).fetchone():
            c.execute("INSERT INTO ac_roles VALUES (?, ?, ?, ?, ?, ?)",
                      (EVERYONE, "Every account gets this.", json.dumps([PUBLIC]), json.dumps(list(CONNECTORS)),
                       core.now(), "system"))
        _ready = str(core.DB_PATH)
    return c


def _j(s) -> list:
    try:
        return list(json.loads(s or "[]"))
    except ValueError:
        return []


def team_set(team: str) -> str:
    """A team's own data set name (a valid group name)."""
    return teams.slug(team)[:40].strip("-") or "team"


# ------------------------------------------------------------------ data sets

def _pretty(n: str) -> str:
    return n.replace("-", " ").replace("_", " ").capitalize()


def datasets() -> list[dict]:
    """Every data set: built in (Everyone, one per team), created ones, and labels found on files."""
    from . import engine
    out: dict[str, dict] = {PUBLIC: {"name": PUBLIC, "label": "Everyone", "kind": "everyone", "description":
                                     "Every account can see these files."}}
    for t in directory.team_names():
        s = team_set(t)
        out.setdefault(s, {"name": s, "label": t, "kind": "team", "team": t, "description": f"Files for {t}."})
    for r in db().execute("SELECT * FROM ac_datasets ORDER BY name"):
        out.setdefault(r["name"], {"name": r["name"], "label": _pretty(r["name"]), "kind": "custom", "description": r["description"]})
    try:
        for g in engine.all_groups():
            if not g.startswith("layer:"):
                out.setdefault(g, {"name": g, "label": _pretty(g), "kind": "custom", "description": ""})
    except Exception:                                      # noqa: BLE001 - no documents yet
        pass
    return list(out.values())


def add_dataset(name: str, description: str, by: str) -> dict:
    from .engine import clean_groups
    n = clean_groups([name.strip().lower().replace(" ", "-")])
    if len(n) != 1:
        raise ValueError("Give the data set a name.")
    if any(d["name"] == n[0] for d in datasets()):
        raise ValueError(f"There is already a data set called {n[0]}.")
    db().execute("INSERT INTO ac_datasets VALUES (?, ?, ?, ?)", (n[0], description.strip()[:200], core.now(), by))
    return {"name": n[0]}


def delete_dataset(name: str):
    from . import engine
    if any(name in d["groups"] for d in engine.documents()):
        raise ValueError("Move its files to another data set first.")
    c = db()
    c.execute("DELETE FROM ac_datasets WHERE name = ?", (name,))
    for tbl, key in (("ac_roles", "name"), ("ac_team", "team"), ("ac_user", "email")):
        for r in c.execute(f"SELECT {key} AS k, datasets FROM {tbl}").fetchall():
            ds = _j(r["datasets"])
            if name in ds:
                c.execute(f"UPDATE {tbl} SET datasets = ? WHERE {key} = ?", (json.dumps([d for d in ds if d != name]), r["k"]))


# ------------------------------------------------------------------ roles

def roles() -> list[dict]:
    return [{"name": r["name"], "description": r["description"], "datasets": _j(r["datasets"]),
             "connectors": _j(r["connectors"]), "builtin": r["name"] == EVERYONE}
            for r in db().execute("SELECT * FROM ac_roles ORDER BY name = ? DESC, name", (EVERYONE,))]


def _check(ds: list[str], cons: list[str]) -> tuple[list[str], list[str]]:
    known = {d["name"] for d in datasets()}
    bad = [d for d in ds if d not in known]
    if bad:
        raise ValueError(f"Unknown data set: {', '.join(bad)}")
    badc = [c for c in cons if c not in CONNECTORS]
    if badc:
        raise ValueError(f"Unknown connector: {', '.join(badc)}")
    return sorted(set(ds)), [c for c in CONNECTORS if c in cons]


def save_role(name: str, description: str, ds: list[str], cons: list[str], by: str, old: str | None = None) -> dict:
    name = " ".join((name or "").split())[:60]
    if not name:
        raise ValueError("Give the role a name.")
    ds, cons = _check(ds, cons)
    c = db()
    if old and old != name:
        if old == EVERYONE:
            raise ValueError("The Everyone role keeps its name.")
        if c.execute("SELECT 1 FROM ac_roles WHERE name = ?", (name,)).fetchone():
            raise ValueError(f"There is already a role called {name}.")
        c.execute("UPDATE ac_roles SET name = ? WHERE name = ?", (name, old))
        for tbl, key in (("ac_team", "team"), ("ac_user", "email")):
            for r in c.execute(f"SELECT {key} AS k, roles FROM {tbl}").fetchall():
                rs = _j(r["roles"])
                if old in rs:
                    c.execute(f"UPDATE {tbl} SET roles = ? WHERE {key} = ?",
                              (json.dumps([name if x == old else x for x in rs]), r["k"]))
    elif not old and c.execute("SELECT 1 FROM ac_roles WHERE name = ?", (name,)).fetchone():
        raise ValueError(f"There is already a role called {name}.")
    c.execute("INSERT INTO ac_roles VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET "
              "description = excluded.description, datasets = excluded.datasets, connectors = excluded.connectors",
              (name, description.strip()[:200], json.dumps(ds), json.dumps(cons), core.now(), by))
    return next(r for r in roles() if r["name"] == name)


def delete_role(name: str):
    if name == EVERYONE:
        raise ValueError("The Everyone role cannot be deleted; edit it instead.")
    c = db()
    c.execute("DELETE FROM ac_roles WHERE name = ?", (name,))
    for tbl, key in (("ac_team", "team"), ("ac_user", "email")):
        for r in c.execute(f"SELECT {key} AS k, roles FROM {tbl}").fetchall():
            rs = _j(r["roles"])
            if name in rs:
                c.execute(f"UPDATE {tbl} SET roles = ? WHERE {key} = ?", (json.dumps([x for x in rs if x != name]), r["k"]))


# ------------------------------------------------------------------ grants to a team or a person

def _grant(tbl: str, key: str, val: str) -> dict:
    r = db().execute(f"SELECT * FROM {tbl} WHERE {key} = ?", (val,)).fetchone()
    return {"datasets": _j(r["datasets"]) if r else [], "connectors": _j(r["connectors"]) if r else [],
            "roles": _j(r["roles"]) if r else []}


def team_grant(team: str) -> dict:
    return _grant("ac_team", "team", team)


def user_grant(email: str) -> dict:
    return _grant("ac_user", "email", (email or "").lower())


def _save(tbl: str, key: str, val: str, ds: list[str], cons: list[str], rs: list[str], by: str):
    ds, cons = _check(ds, cons)
    known = {r["name"] for r in roles()}
    bad = [r for r in rs if r not in known]
    if bad:
        raise ValueError(f"Unknown role: {', '.join(bad)}")
    db().execute(f"INSERT INTO {tbl} ({key}, datasets, connectors, roles, updated, by_user) VALUES (?, ?, ?, ?, ?, ?) "
                 f"ON CONFLICT({key}) DO UPDATE SET datasets = excluded.datasets, connectors = excluded.connectors, "
                 "roles = excluded.roles, updated = excluded.updated, by_user = excluded.by_user",
                 (val, json.dumps(ds), json.dumps(cons), json.dumps(sorted(set(rs) - {EVERYONE})), core.now(), by))


def set_team_grant(team: str, ds: list[str], cons: list[str], rs: list[str], by: str):
    if team not in directory.team_names():
        raise ValueError(f"There is no team called {team}.")
    _save("ac_team", "team", team, ds, cons, rs, by)


def set_user_grant(email: str, ds: list[str], cons: list[str], rs: list[str], by: str):
    _save("ac_user", "email", email.strip().lower(), ds, cons, rs, by)


# ------------------------------------------------------------------ what someone ends up with

def effective(email: str, role: str | None = None) -> dict:
    """Data sets and connectors a person gets, and why: {"datasets": {name: [reasons]}, "connectors": {..}}."""
    email = (email or "").lower()
    rmap = {r["name"]: r for r in roles()}
    ds: dict[str, list[str]] = {}
    cs: dict[str, list[str]] = {}

    def add(d: list[str], c: list[str], why: str):
        for target, items in ((ds, d), (cs, c)):
            for x in items:
                reasons = target.setdefault(x, [])
                if why not in reasons:
                    reasons.append(why)

    if role in ("owner", "admin"):
        add([d["name"] for d in datasets()], list(CONNECTORS), "company admin")
    ev = rmap.get(EVERYONE)
    if ev:
        add(ev["datasets"], ev["connectors"], "Everyone")
    seen = []
    for t in directory.teams_of(email):
        for x in list(reversed(teams.ancestors(t))) + [t]:
            if x in seen:
                continue
            seen.append(x)
            g = team_grant(x)
            why = f"team {x}"
            add([team_set(x)] + g["datasets"], g["connectors"], why)
            for rn in g["roles"]:
                if rn in rmap:
                    add(rmap[rn]["datasets"], rmap[rn]["connectors"], f"team {x} (role {rn})")
    u = user_grant(email)
    for rn in u["roles"]:
        if rn in rmap:
            add(rmap[rn]["datasets"], rmap[rn]["connectors"], f"role {rn}")
    add(u["datasets"], u["connectors"], "given directly")
    try:                                                  # groups set on the account before v0.8
        r = core.db().execute("SELECT groups FROM users WHERE email = ?", (email,)).fetchone()
        add([g for g in _j(r["groups"]) if g != PUBLIC] if r else [], [], "account")
    except Exception:                                      # noqa: BLE001
        pass
    return {"datasets": ds, "connectors": cs, "teams": seen, "roles": u["roles"]}


def for_agent(email: str, role: str | None) -> tuple[list[str], list[str]]:
    """(groups, capabilities) for the MCP agent of a signed-in person."""
    e = effective(email, role)
    return sorted(e["datasets"]), [c for c in CONNECTORS if c in e["connectors"]]


def who_can_see(dataset: str) -> dict:
    """Teams, roles and people that reach a data set (company admins always do)."""
    team_list = [t for t in directory.team_names() if dataset == team_set(t) or dataset in team_grant(t)["datasets"]
                 or any(dataset in r["datasets"] for r in roles() if r["name"] in team_grant(t)["roles"])]
    under = set(team_list)
    for t in directory.team_names():
        if any(a in team_list for a in teams.ancestors(t)):
            under.add(t)
    role_list = [r["name"] for r in roles() if dataset in r["datasets"]]
    people = [r["email"] for r in db().execute("SELECT email, datasets, roles FROM ac_user")
              if dataset in _j(r["datasets"]) or any(x in role_list for x in _j(r["roles"]))]
    return {"everyone": EVERYONE in role_list, "teams": sorted(under), "roles": role_list, "people": people}


def manageable(agent: dict) -> dict:
    """What this admin may hand out: everything for company admins; a team admin only what their teams have."""
    if teams.is_company_admin(agent):
        return {"datasets": [d["name"] for d in datasets()], "connectors": list(CONNECTORS),
                "roles": [r["name"] for r in roles() if not r["builtin"]], "all": True}
    ds, cs = set(), set()
    rmap = {r["name"]: r for r in roles()}
    for t in teams.managed_teams(agent):
        g = team_grant(t)
        ds |= {team_set(t), *g["datasets"]}
        cs |= set(g["connectors"])
        for rn in g["roles"]:
            if rn in rmap:
                ds |= set(rmap[rn]["datasets"])
                cs |= set(rmap[rn]["connectors"])
    ev = rmap.get(EVERYONE)
    if ev:
        ds |= set(ev["datasets"])
        cs |= set(ev["connectors"])
    fit = [r["name"] for r in roles() if not r["builtin"] and set(r["datasets"]) <= ds and set(r["connectors"]) <= cs]
    return {"datasets": sorted(ds), "connectors": [c for c in CONNECTORS if c in cs], "roles": fit, "all": False}
