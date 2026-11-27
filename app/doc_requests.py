"""Moving a personal file into a team's data set or the company (Everyone), from chat.

A file someone added from Claude lands in their own layer ("layer:user:<id>"): only they see it. They can ask Claude
to share it with their team or the whole company:
  - company admins (and, for a team, that team's admins) do it directly after a yes from the person;
  - for everyone else it becomes a request that the right admins approve on the Approvals page:
      company (Everyone)  -> company admins
      a team's data set   -> admins of that team or a team above it, and company admins
Approving moves the file into the data set and prepares it again, so Claude finds it for those people.
"""
import secrets
import time

from . import core, engine, teams

SCHEMA = """CREATE TABLE IF NOT EXISTS doc_requests (id INTEGER PRIMARY KEY, file TEXT NOT NULL, dataset TEXT NOT NULL,
  team TEXT, asked_by TEXT NOT NULL, app TEXT NOT NULL DEFAULT '', created TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'waiting',
  decided_by TEXT, decided TEXT, reason TEXT NOT NULL DEFAULT '')"""
_ready: str | None = None
_holds: dict[str, dict] = {}          # confirm code -> the move waiting for the person's yes (10 minutes)


def db():
    global _ready
    c = core.db()
    if _ready != str(core.DB_PATH):
        c.execute(SCHEMA)
        _ready = str(core.DB_PATH)
    return c


class Refused(Exception):
    pass


def _target(agent: dict, to: str) -> tuple[str, str | None, str]:
    """(data set, team or None, label) for 'company', 'team' or 'team:<name>'."""
    from . import access, directory
    to = (to or "company").strip()
    if to.lower() in ("company", "everyone", "public"):
        return access.PUBLIC, None, "everyone in the company"
    if to.lower() in ("team", "my team"):
        mine = directory.teams_of(agent.get("email", ""))
        if not mine:
            raise Refused("You are not in a team. Ask an admin to add you, or share it with the company instead.")
        name = mine[0]
    elif to.lower().startswith("team:"):
        want = to[5:].strip().lower()
        name = next((t for t in directory.team_names() if t.lower() == want), None)
        if not name:
            raise Refused(f"There is no team called {to[5:].strip()}.")
    else:
        raise Refused("Say where to: company, team, or team:<team name>.")
    return access.team_set(name), name, f"the {name} team"


def _may_direct(agent: dict, team: str | None) -> bool:
    if teams.is_company_admin(agent):
        return True
    return bool(team) and teams.manages(agent, team)


def _own_file(agent: dict, name: str) -> dict:
    mine = f"layer:user:{agent.get('user_id')}"
    f = next((d for d in engine.documents() if d["name"] == name.strip()), None)
    if f is None or (mine not in f["groups"] and not teams.is_company_admin(agent)):
        raise Refused(f"There is no file '{name}' among the files you added. list_documents shows your files.")
    return f


def share(agent: dict, name: str, to: str = "company", confirm: str | None = None) -> dict:
    if confirm:
        h = _holds.pop(confirm, None)
        if not h or h["who"] != agent.get("email") or h["until"] < time.time():
            raise Refused("That confirm code has expired. Ask again.")
        return _apply(agent, h) if h["direct"] else _request(agent, h)
    f = _own_file(agent, name)
    ds, team, label = _target(agent, to)
    if ds in f["groups"]:
        raise Refused(f"{f['name']} is already visible to {label}.")
    direct = _may_direct(agent, team)
    code = secrets.token_hex(4)
    _holds[code] = {"file": f["name"], "dataset": ds, "team": team, "label": label, "direct": direct,
                    "who": agent.get("email"), "app": agent.get("client") or "", "until": time.time() + 600}
    q = (f"Share your file {f['name']} with {label}? Claude will then use it in their answers too."
         if direct else
         f"Ask to share your file {f['name']} with {label}? It goes to "
         f"{'the company admins' if team is None else f'the admins of {team}'} for approval; until they approve, "
         "only you see it.")
    return {"saved": False, "confirm_code": code, "ask_the_person": q,
            "next": "Ask the person this question in your own words. Only if they clearly say yes, call this tool "
                    "again with confirm set to the confirm_code."}


def _apply(agent: dict, h: dict) -> dict:
    groups = [g for g in (engine.access_map().get(h["file"]) or []) if not g.startswith("layer:")] + [h["dataset"]]
    engine.set_document_groups(h["file"], sorted(set(groups)))
    job = engine.queue_rebuild([{"file": h["file"], "kind": "document"}], agent.get("email") or "admin")
    return {"saved": True, "file": h["file"], "now_visible_to": h["label"], "job": job["id"],
            "note": "Done. Context Layer prepares the file again; build_status shows when it is ready for them."}


def _request(agent: dict, h: dict) -> dict:
    if db().execute("SELECT 1 FROM doc_requests WHERE file = ? AND dataset = ? AND status = 'waiting'",
                    (h["file"], h["dataset"])).fetchone():
        raise Refused("That request is already waiting for approval.")
    cur = db().execute("INSERT INTO doc_requests (file, dataset, team, asked_by, app, created) VALUES (?, ?, ?, ?, ?, ?)",
                       (h["file"], h["dataset"], h["team"], h["who"], h["app"], core.now()))
    return {"saved": False, "requested": True, "request_id": f"file:{cur.lastrowid}", "file": h["file"],
            "to": h["label"], "note": f"Sent to {'the company admins' if h['team'] is None else 'the admins of ' + h['team']} "
                                      "for approval. Nothing changes until they approve; my_changes shows the answer."}


def _can_decide(agent: dict, r) -> bool:
    return teams.is_company_admin(agent) or (bool(r["team"]) and teams.manages(agent, r["team"]))


def waiting_for(agent: dict) -> list[dict]:
    out = []
    for r in db().execute("SELECT * FROM doc_requests WHERE status = 'waiting' ORDER BY id"):
        if _can_decide(agent, r):
            out.append({"id": f"file:{r['id']}", "type": "company file" if r["team"] is None else "team file",
                        "where": "everyone" if r["team"] is None else f"the {r['team']} team", "kind": "file",
                        "key": r["file"], "summary": r["file"], "asked_by": r["asked_by"], "asked": r["created"],
                        "app": r["app"], "can_decide": True})
    return out


def history(agent: dict, status: str, limit: int = 100) -> list[dict]:
    return [{"id": f"file:{r['id']}", "type": "company file" if r["team"] is None else "team file",
             "where": "everyone" if r["team"] is None else f"the {r['team']} team", "kind": "file", "key": r["file"],
             "summary": r["file"], "asked_by": r["asked_by"], "asked": r["created"], "decided_by": r["decided_by"],
             "decided": r["decided"], "reason": r["reason"]}
            for r in db().execute("SELECT * FROM doc_requests WHERE status = ? ORDER BY id DESC LIMIT ?", (status, limit))
            if _can_decide(agent, r)]


def decide(agent: dict, rid: int, approve: bool, reason: str = "") -> dict:
    r = db().execute("SELECT * FROM doc_requests WHERE id = ?", (rid,)).fetchone()
    if r is None or r["status"] != "waiting":
        raise KeyError("That request is not waiting any more.")
    if not _can_decide(agent, r):
        raise PermissionError("You do not manage where this file would go.")
    out = {"item": f"file {r['file']}", "approved": approve}
    if approve:
        if r["file"] not in {d["name"] for d in engine.documents()}:
            approve = False
            reason = reason or "The file was deleted before approval."
            out["approved"] = False
        else:
            label = "everyone in the company" if r["team"] is None else f"the {r['team']} team"
            out.update(_apply(agent, {"file": r["file"], "dataset": r["dataset"], "label": label}))
    db().execute("UPDATE doc_requests SET status = ?, decided_by = ?, decided = ?, reason = ? WHERE id = ?",
                 ("approved" if approve else "declined", agent.get("email", ""), core.now(), reason[:300], rid))
    return out


def mine(agent: dict, limit: int = 20) -> list[dict]:
    return [{"request": f"file:{r['id']}", "file": r["file"], "to": "everyone" if r["team"] is None else r["team"],
             "status": r["status"], "asked": r["created"], "decided_by": r["decided_by"], "reason": r["reason"]}
            for r in db().execute("SELECT * FROM doc_requests WHERE asked_by = ? ORDER BY id DESC LIMIT ?",
                                  (agent.get("email", ""), limit))]
