"""People and teams directory, kept by admins (Access page) and read by Claude through MCP (find_person, list_teams).

  people   name, work email, title, team, manager, location, active / left, whether they have an account
  teams    name, lead, parent team, description; teams ARE access groups (document access and sharing use them)

Someone marked as left: their account is disabled (their apps are signed out) and shares to them stop.
Invites: a one-time link (7 days) where an invited person sets their password; until an email service is set up,
the link is shown to whoever invited them, to pass on.
Stored in the app database (var/app.db), next to the accounts.
"""
import csv
import io
import json
import re
from datetime import datetime, timedelta, timezone

from . import core

SCHEMA = """
CREATE TABLE IF NOT EXISTS teams (name TEXT PRIMARY KEY, lead_email TEXT, parent TEXT, description TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS people (email TEXT PRIMARY KEY, name TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
  team TEXT, manager_email TEXT, location TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active',
  updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS invites (token_hash TEXT PRIMARY KEY, email TEXT NOT NULL, invited_by TEXT, created TEXT NOT NULL,
  expires TEXT NOT NULL, used TEXT);
"""
_ready: str | None = None
FIELDS = ("email", "name", "title", "team", "manager_email", "location", "status")


def db():
    return core.db_with(SCHEMA)


def _clean_team(name: str) -> str:
    n = " ".join((name or "").split())
    if not n or len(n) > 60 or not re.match(r"^[\w][\w &./-]*$", n):
        raise ValueError(f"'{name}' is not a usable team name (letters, digits, spaces, & . / -; up to 60).")
    return n


# ------------------------------------------------------------------ teams

def team_names() -> list[str]:
    return [r["name"] for r in db().execute("SELECT name FROM teams ORDER BY name")]


def list_teams() -> list[dict]:
    rows = [dict(r) for r in db().execute("SELECT * FROM teams ORDER BY name")]
    counts = {r["team"]: r["n"] for r in db().execute(
        "SELECT team, COUNT(*) AS n FROM people WHERE status = 'active' GROUP BY team")}
    for r in rows:
        r["members"] = counts.get(r["name"], 0)
    return rows


def save_team(name: str, lead_email: str | None = None, parent: str | None = None, description: str = "") -> dict:
    name = _clean_team(name)
    if parent:
        parent = _clean_team(parent)
        if parent == name:
            raise ValueError("A team cannot be its own parent.")
    db().execute("INSERT INTO teams VALUES (?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET lead_email = excluded.lead_email, "
                 "parent = excluded.parent, description = excluded.description",
                 (name, (lead_email or "").lower() or None, parent or None, description or ""))
    return {"name": name}


def delete_team(name: str):
    if db().execute("SELECT 1 FROM people WHERE team = ? AND status = 'active'", (name,)).fetchone():
        raise ValueError("Move the team's people to another team first.")
    db().execute("DELETE FROM teams WHERE name = ?", (name,))


# ------------------------------------------------------------------ people

def list_people(query: str = "", team: str | None = None, include_left: bool = False) -> list[dict]:
    sql, args = "SELECT * FROM people WHERE 1 = 1", []
    if not include_left:
        sql += " AND status = 'active'"
    if team:
        sql += " AND team = ?"
        args.append(team)
    rows = [dict(r) for r in db().execute(sql + " ORDER BY name", args)]
    q = query.strip().lower()
    if q:
        rows = [r for r in rows if q in r["name"].lower() or q in r["email"] or q in (r["title"] or "").lower()
                or q in (r["team"] or "").lower()]
    accounts = {r["email"] for r in core.db().execute("SELECT email FROM users WHERE disabled = 0")}
    for r in rows:
        r["has_account"] = r["email"] in accounts
    return rows


def save_person(p: dict, by: str = "") -> dict:
    email = (p.get("email") or "").strip().lower()
    if "@" not in email:
        raise ValueError("Each person needs a work email.")
    name = " ".join((p.get("name") or "").split())
    if not name:
        raise ValueError(f"{email}: give a name.")
    team = _clean_team(p["team"]) if p.get("team") else None
    if team and team not in team_names():
        save_team(team)
    status = (p.get("status") or "active").strip().lower()
    if status not in ("active", "left"):
        raise ValueError("status is active or left")
    old = db().execute("SELECT status FROM people WHERE email = ?", (email,)).fetchone()
    db().execute("INSERT INTO people VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(email) DO UPDATE SET name = excluded.name, "
                 "title = excluded.title, team = excluded.team, manager_email = excluded.manager_email, "
                 "location = excluded.location, status = excluded.status, updated = excluded.updated",
                 (email, name, (p.get("title") or "").strip(), team, (p.get("manager_email") or "").strip().lower() or None,
                  (p.get("location") or "").strip(), status, core.now()))
    if status == "left" and (old is None or old["status"] != "left"):
        _left(email, by)
    return {"email": email, "name": name, "team": team, "status": status}


def _left(email: str, by: str):
    """Disable their account (signs out their apps) and stop shares to them."""
    u = core.db().execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if u:
        try:
            core.update_user(u["id"], disabled=True)
        except ValueError:
            pass
    try:
        from semantic import defstore
        if defstore.enabled():
            from .sharing import _db
            _db().q("UPDATE shares SET revoked = ? WHERE kind = 'person' AND target = ? AND revoked IS NULL",
                    (core.now(), email))
    except Exception:                                      # noqa: BLE001 - sharing store optional
        pass
    core.log_event("user", by or "admin", "directory.left", email)


def import_csv(text: str, by: str = "") -> dict:
    """Columns (any order, header row): name, email, title, team, manager_email (or manager), location, status."""
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    if not rows:
        raise ValueError("The file has no rows.")
    norm = lambda k: re.sub(r"[^a-z]+", "_", (k or "").strip().lower()).strip("_")
    done, errors = 0, []
    for i, raw in enumerate(rows, 2):
        r = {norm(k): (v or "").strip() for k, v in raw.items()}
        r.setdefault("manager_email", r.get("manager", ""))
        r["email"] = r.get("email") or r.get("work_email", "")
        try:
            save_person(r, by)
            done += 1
        except ValueError as e:
            errors.append(f"row {i}: {e}")
    core.log_event("user", by or "admin", "directory.import", f"{done} people", {"errors": errors[:20]})
    return {"imported": done, "errors": errors[:50]}


def teams_of(email: str) -> list[str]:
    r = db().execute("SELECT team FROM people WHERE email = ? AND status = 'active'", ((email or "").lower(),)).fetchone()
    return [r["team"]] if r and r["team"] else []


def find_person(query: str) -> dict:
    """For Claude: who someone is (name, title, team, manager, location). Nothing else from HR."""
    hits = list_people(query)[:10]
    managers = {p["email"]: p["name"] for p in list_people()}
    leads = {t["name"]: t["lead_email"] for t in list_teams()}
    return {"people": [{"name": p["name"], "email": p["email"], "title": p["title"], "team": p["team"],
                        "manager": managers.get(p["manager_email"] or "", p["manager_email"]), "location": p["location"],
                        "leads_team": p["team"] if leads.get(p["team"]) == p["email"] else None,
                        "has_account": p["has_account"]} for p in hits],
            "more": max(0, len(list_people(query)) - 10)}


def teams_for_claude() -> dict:
    people = {p["email"]: p["name"] for p in list_people()}
    return {"teams": [{"name": t["name"], "lead": people.get(t["lead_email"] or "", t["lead_email"]),
                       "parent": t["parent"], "members": t["members"], "description": t["description"]}
                      for t in list_teams()]}


# ------------------------------------------------------------------ invites

def invite(email: str, by: str = "", base: str = "", role: str = "viewer") -> dict:
    email = email.strip().lower()
    if "@" not in email:
        raise ValueError("Give a work email.")
    if core.db().execute("SELECT 1 FROM users WHERE email = ? AND disabled = 0", (email,)).fetchone():
        return {"email": email, "note": "Already has an account."}
    tok = core.new_token("cli_")
    exp = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(timespec="seconds")
    db().execute("INSERT INTO invites VALUES (?, ?, ?, ?, ?, NULL)", (core.token_hash(tok), email, by, core.now(), exp))
    core.set_setting(f"invite_role:{email}", role)
    core.log_event("user", by or "admin", "directory.invite", email)
    return {"email": email, "invite_link": f"{base}/invite/{tok}", "valid_days": 7,
            "note": "No email service is set up: pass this link on. It works once."}


def invite_for(token: str) -> dict | None:
    r = db().execute("SELECT * FROM invites WHERE token_hash = ? AND used IS NULL AND expires > ?",
                     (core.token_hash(token), core.now())).fetchone()
    return dict(r) if r else None


def accept_invite(token: str, name: str, password: str) -> dict:
    inv = invite_for(token)
    if inv is None:
        raise ValueError("This invite link has expired or was already used.")
    p = db().execute("SELECT name FROM people WHERE email = ?", (inv["email"],)).fetchone()
    role = core.get_setting(f"invite_role:{inv['email']}", "viewer") or "viewer"
    u = core.create_user(inv["email"], name or (p["name"] if p else inv["email"].split("@")[0]), password, role)
    db().execute("UPDATE invites SET used = ? WHERE token_hash = ?", (core.now(), core.token_hash(token)))
    return u
