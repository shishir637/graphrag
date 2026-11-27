"""v0.8 access: data sets, roles, team access and unique access, and what Claude gets from them."""
from tests.test_layers import H, as_owner, call, fresh, two  # noqa: F401 - fixture


def test_team_role_and_unique_access(two):
    c, owner, priya = two
    as_owner(c)
    c.post("/api/v1/teams", headers=H, json={"name": "Engineering"})
    c.post("/api/v1/teams", headers=H, json={"name": "Data Engineering", "parent": "Engineering"})
    c.post("/api/v1/teams", headers=H, json={"name": "Finance"})
    assert c.post("/api/v1/access/datasets", headers=H, json={"name": "board-papers"}).status_code == 200
    r = c.post("/api/v1/access/roles", headers=H, json={"name": "Finance reader", "datasets": ["finance"],
                                                        "connectors": ["data"]})
    assert r.status_code == 200, r.text
    # team access: Engineering gets board papers; Data Engineering is under it
    assert c.put("/api/v1/access/teams/engineering", headers=H,
                 json={"datasets": ["board-papers"], "connectors": [], "roles": []}).status_code == 200
    c.post("/api/v1/people", headers=H, json={"email": "priya@kestrel-labs.example", "team": "Data Engineering"})
    me = c.get("/api/v1/access/user", params={"email": "priya@kestrel-labs.example"}).json()["effective"]
    assert {"public", "engineering", "data-engineering", "board-papers"} <= set(me["datasets"])
    assert "finance" not in me["datasets"] and me["datasets"]["board-papers"] == ["team Engineering"]
    # unique access: a role for this one person
    c.put("/api/v1/access/user", headers=H, json={"email": "priya@kestrel-labs.example", "roles": ["Finance reader"]})
    me = c.get("/api/v1/access/user", params={"email": "priya@kestrel-labs.example"}).json()["effective"]
    assert me["datasets"]["finance"] == ["role Finance reader"]
    # Claude gets the same (the token is read fresh on each call)
    groups = call(c, priya, "list_documents", {})["your_groups"]
    assert {"finance", "board-papers", "data-engineering"} <= set(groups)
    # connectors: take documents away from Everyone; Priya keeps data through her role
    c.post("/api/v1/access/roles", headers=H, json={"name": "Everyone", "old": "Everyone", "datasets": ["public"],
                                                    "connectors": ["graph"]})
    from app import core
    tok = core.db().execute("SELECT 1").fetchone()
    assert tok
    ag_groups, caps = __import__("app.access", fromlist=["for_agent"]).for_agent("priya@kestrel-labs.example", "viewer")
    assert caps == ["data", "graph"]
    # who can see a data set
    ov = c.get("/api/v1/access").json()
    bp = next(d for d in ov["datasets"] if d["name"] == "board-papers")
    assert bp["who"]["teams"] == ["Data Engineering", "Engineering"]
    fin = next(d for d in ov["datasets"] if d["name"] == "finance")
    assert fin["who"]["roles"] == ["Finance reader"] and fin["who"]["people"] == ["priya@kestrel-labs.example"]
    # renaming a role carries its holders along; deleting removes it
    c.post("/api/v1/access/roles", headers=H, json={"name": "Finance viewer", "old": "Finance reader",
                                                    "datasets": ["finance"], "connectors": ["data"]})
    assert c.get("/api/v1/access/user", params={"email": "priya@kestrel-labs.example"}).json()["grant"]["roles"] == ["Finance viewer"]
    assert c.delete("/api/v1/access/roles/Everyone", headers=H).status_code == 400


def test_team_admin_limits_and_files(two):
    c, owner, priya = two
    as_owner(c)
    c.post("/api/v1/teams", headers=H, json={"name": "Sales"})
    c.post("/api/v1/teams", headers=H, json={"name": "Finance"})
    c.post("/api/v1/access/roles", headers=H, json={"name": "Finance reader", "datasets": ["finance"], "connectors": ["data"]})
    c.post("/api/v1/people", headers=H, json={"email": "priya@kestrel-labs.example", "team": "Sales", "admin_of": ["Sales"]})
    c.post("/api/v1/people", headers=H, json={"email": "sam@kestrel-labs.example", "team": "Sales"})
    c.post("/api/v1/people", headers=H, json={"email": "fay@kestrel-labs.example", "team": "Finance"})
    c.cookies.clear()
    assert c.post("/api/v1/login", headers=H, json={"email": "priya@kestrel-labs.example", "password": "priya-pass-123"}).status_code == 200
    ov = c.get("/api/v1/access").json()
    assert not ov["company_admin"] and ov["can_give"]["datasets"] == ["public", "sales"]
    assert "Finance reader" not in ov["can_give"]["roles"]
    # she gives Sam what Sales has, not more
    assert c.put("/api/v1/access/user", headers=H, json={"email": "sam@kestrel-labs.example", "datasets": ["sales"]}).status_code == 200
    assert c.put("/api/v1/access/user", headers=H, json={"email": "sam@kestrel-labs.example", "datasets": ["finance"]}).status_code == 403
    assert c.put("/api/v1/access/user", headers=H, json={"email": "fay@kestrel-labs.example", "datasets": []}).status_code == 403
    # what a company admin gave Sam stays when the team admin edits him
    from app import access
    access.set_user_grant("sam@kestrel-labs.example", ["sales"], [], ["Finance reader"], "owner")
    assert c.put("/api/v1/access/user", headers=H, json={"email": "sam@kestrel-labs.example", "datasets": []}).status_code == 200
    assert access.user_grant("sam@kestrel-labs.example")["roles"] == ["Finance reader"]
    assert c.put("/api/v1/access/teams/sales", headers=H, json={"datasets": ["finance"]}).status_code == 403
    # files: into her team's data set only
    up = c.post("/api/v1/data/files", headers=H, data={"datasets": "sales"}, files=[("files", ("plan.md", b"# Plan", "text/markdown"))])
    assert up.status_code == 200, up.text
    assert c.post("/api/v1/data/files", headers=H, data={"datasets": "public"},
                  files=[("files", ("x.md", b"x", "text/markdown"))]).status_code == 403
    d = c.get("/api/v1/data").json()
    assert [f["name"] for f in d["files"]] == ["plan.md"] and d["upload_to"] == ["sales"]
    assert c.delete("/api/v1/data/files/plan.md", headers=H).status_code == 200


def test_create_and_delete_teams_and_data_sets(two):
    c, owner, priya = two
    as_owner(c)
    assert c.post("/api/v1/teams", headers=H, json={"name": "Ops"}).status_code == 200
    assert c.post("/api/v1/teams", headers=H, json={"name": "Night Ops", "parent": "Ops"}).status_code == 200
    assert c.put("/api/v1/access/teams/night-ops", headers=H, json={"datasets": [], "connectors": ["graph"]}).status_code == 200
    assert c.delete("/api/v1/directory/teams/Ops", headers=H).status_code == 400          # has a team inside
    assert c.delete("/api/v1/directory/teams/Night Ops", headers=H).status_code == 200
    from app import access
    assert access.team_grant("Night Ops") == {"datasets": [], "connectors": [], "roles": []}
    assert c.delete("/api/v1/directory/teams/Ops", headers=H).status_code == 200
    r = c.post("/api/v1/access/datasets", headers=H, json={"name": "Board papers"})
    assert r.json()["name"] == "board-papers"
    assert c.delete("/api/v1/access/datasets/board-papers", headers=H).status_code == 200
    assert "board-papers" not in [d["name"] for d in c.get("/api/v1/access").json()["datasets"]]


def test_claude_is_told_when_to_use_the_tools(two):
    c, owner, priya = two
    as_owner(c)
    c.post("/api/v1/teams", headers=H, json={"name": "Data Engineering"})
    from app import mcp_server as M
    text = M.describe_org()
    org = __import__("app.core", fromlist=["get_setting"]).get_setting("organisation", "")
    assert "EVERY question about" in text and (org or "this organisation") in text and "Data Engineering" in text
    assert "FIRST" in M.mcp._tool_manager.get_tool("ask").description


def test_personal_rule_and_file_go_to_team_and_company_with_approval(two):
    c, owner, priya = two
    from app import engine
    as_owner(c)
    c.post("/api/v1/teams", headers=H, json={"name": "Sales"})
    c.post("/api/v1/people", headers=H, json={"email": "priya@kestrel-labs.example", "team": "Sales"})
    c.post("/api/v1/people", headers=H, json={"email": "sam@kestrel-labs.example", "team": "Sales", "admin_of": ["Sales"]})
    c.post("/api/v1/users", headers=H, json={"email": "sam@kestrel-labs.example", "name": "Sam", "role": "viewer",
                                             "password": "sam-password-123", "temporary": False})
    # Priya makes a personal rule, then asks for it in the team layer: a request
    q = call(c, priya, "add_business_rule", {"rule": "Deals count when signed."})
    call(c, priya, "add_business_rule", {"rule": "x", "confirm": q["confirm_code"]})
    rules = call(c, priya, "my_changes", {})["items"]
    key = next(i["key"] for i in rules if i["kind"] == "rule")
    q = call(c, priya, "publish_change", {"name": key, "kind": "rule", "to": "team"})
    assert "approval" in q["ask_the_person"], q
    r = call(c, priya, "publish_change", {"name": key, "kind": "rule", "to": "team", "confirm": q["confirm_code"]})
    assert r.get("requested"), r
    # a personal file, asked for the whole company: a request too
    call(c, priya, "add_document", {"filename": "deal-desk.md", "text": "# Deal desk\n\nHow we approve deals."})
    q = call(c, priya, "publish_change", {"name": "deal-desk.md", "kind": "file", "to": "company"})
    assert "approval" in q["ask_the_person"], q
    r = call(c, priya, "publish_change", {"name": "deal-desk.md", "kind": "file", "to": "company", "confirm": q["confirm_code"]})
    assert r["requested"] and r["request_id"].startswith("file:")
    assert call(c, priya, "my_changes", {})["file_requests"][0]["status"] == "waiting"
    # the team admin sees the team rule, not the company file; the company admin sees both
    c.cookies.clear()
    c.post("/api/v1/login", headers=H, json={"email": "sam@kestrel-labs.example", "password": "sam-password-123"})
    w = c.get("/api/v1/approvals").json()["items"]
    assert [x["type"] for x in w] == ["team change"]
    assert c.post(f"/api/v1/approvals/{w[0]['id']}", headers=H, json={"approve": True}).status_code == 200
    fid = r["request_id"].split(":")[1]
    assert c.post(f"/api/v1/approvals/file/{fid}", headers=H, json={"approve": True}).status_code == 403
    as_owner(c)
    w = c.get("/api/v1/approvals").json()["items"]
    assert any(x["type"] == "company file" for x in w)
    assert c.post(f"/api/v1/approvals/file/{fid}", headers=H, json={"approve": True}).status_code == 200
    assert engine.access_map()["deal-desk.md"] == ["public"]
    assert call(c, priya, "my_changes", {})["file_requests"][0]["status"] == "approved"
    hist = c.get("/api/v1/approvals?status=approved").json()["items"]
    assert {h["type"] for h in hist} >= {"team change", "company file"}
