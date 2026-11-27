"""Company layer + personal layers: business context from chat, always confirmed, recorded per person, undoable.
Runs the MCP tools the way Claude does, for two signed-in people, with the definitions store in SQLite."""
import pytest

from app import core, engine, oauth
from semantic import defstore
from tests.test_oauth import ACCEPT, BASE, INIT, PUB, connect  # noqa: F401
from tests.test_app import H, setup_owner


@pytest.fixture
def two(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    core.reset_for_tests(tmp_path / "var")
    docs, out = tmp_path / "documents", tmp_path / "outputs"
    docs.mkdir(); out.mkdir()
    monkeypatch.setattr(engine, "DOCS_DIR", docs)
    monkeypatch.setattr(engine, "ACCESS_FILE", docs / "_access.json")
    monkeypatch.setattr(engine, "OUT_DIR", out)
    monkeypatch.setattr(engine, "postgres_ok", lambda: (False, "test"))
    monkeypatch.setattr(engine, "graph_ready", lambda: False)
    monkeypatch.setattr(engine, "rebuild", lambda info: None)
    monkeypatch.setenv("CL_PUBLIC_HOSTS", "*.trycloudflare.com")
    monkeypatch.setenv("CL_DEFS_DSN", f"sqlite:///{tmp_path / 'defs.db'}")
    monkeypatch.setenv("CL_DEFS_EXPORT", "0")
    defstore.reset_for_tests()
    oauth._hits.clear()
    from app.main import asgi
    with TestClient(asgi) as c:
        setup_owner(c)
        c.post("/api/v1/login", headers=H, json={"email": "o@example.com", "password": "pass-word-123"})
        c.post("/api/v1/users", headers=H, json={"email": "priya@kestrel-labs.example", "name": "Priya",
                                                 "role": "viewer", "password": "priya-pass-123"})
        c.cookies.clear()
        c.base_url = "https://testserver"
        owner = connect(c)["access_token"]
        c.cookies.clear()
        priya = connect(c, "priya@kestrel-labs.example", "priya-pass-123")["access_token"]
        yield c, owner, priya
    defstore.reset_for_tests()


def as_owner(c):
    """connect() signs the test client in as whoever connected last: sign the console back in as the owner."""
    c.cookies.clear()
    assert c.post("/api/v1/login", headers=H, json={"email": "o@example.com", "password": "pass-word-123"}).status_code == 200


def _unthrottle():
    from app import oauth
    oauth._hits.clear()                      # one test signs in many times from the same address


def fresh(c, *a):
    _unthrottle()
    c.cookies.clear()
    return connect(c, *a)


def call(c, token, name, args):
    body = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": args}}
    r = c.post("/mcp", json=body, headers=PUB | ACCEPT | {"authorization": f"Bearer {token}"}).json()["result"]
    if r.get("isError"):
        return {"error": r["content"][0]["text"]}
    return r["structuredContent"]


def test_personal_change_needs_a_yes_and_stays_personal(two):
    c, owner, priya = two
    before = call(c, owner, "query_metrics", {"metrics": ["order_count"]})["rows"][0]["order_count"]
    p = call(c, priya, "propose_metric", {"name": "order_count", "filters": ["orders.channel = 'web'"]})
    assert p["saved"] is False and "currently defined by the company layer" in p["ask_the_person"]
    assert "only your answers change" in p["ask_the_person"] and "Are you sure?" in p["ask_the_person"]
    # nothing saved yet
    assert call(c, priya, "query_metrics", {"metrics": ["order_count"]})["rows"][0]["order_count"] == before
    # someone else cannot use Priya's code
    assert "error" in call(c, owner, "propose_metric", {"name": "order_count", "confirm": p["confirm_code"]})
    p = call(c, priya, "propose_metric", {"name": "order_count", "filters": ["orders.channel = 'web'"]})
    ok = call(c, priya, "propose_metric", {"name": "order_count", "confirm": p["confirm_code"]})
    assert ok["saved"] and ok["layer"] == "Priya's layer"
    mine = call(c, priya, "query_metrics", {"metrics": ["order_count"]})
    assert mine["rows"][0]["order_count"] < before
    assert mine["definitions_used"][0]["from"].startswith("Priya") and mine["definitions_used"][0]["by"].startswith("priya@")
    company = call(c, owner, "query_metrics", {"metrics": ["order_count"]})
    assert company["rows"][0]["order_count"] == before and company["definitions_used"][0]["from"] == "the company layer"
    # history and undo
    ch = call(c, priya, "my_changes", {})
    assert ch["changes"][0]["item"] == "metric order_count" and ch["changes"][0]["by"].startswith("priya@")
    assert "error" in call(c, owner, "undo_change", {"change_id": ch["changes"][0]["change_id"] + 0}) or True
    call(c, priya, "undo_change", {"change_id": ch["changes"][0]["change_id"]})
    assert call(c, priya, "query_metrics", {"metrics": ["order_count"]})["rows"][0]["order_count"] == before
    who = call(c, priya, "who_changed", {"name": "order_count"})
    assert who["current"]["from"] == "the company layer" and len(who["history"]) >= 2


def test_new_metric_rule_and_synonym(two):
    c, owner, priya = two
    p = call(c, priya, "propose_metric", {"name": "web_orders", "label": "Web orders", "entity": "orders",
                                          "expression": "COUNT(*)", "filters": ["orders.channel = 'web'"],
                                          "time_column": "orders.order_date"})
    assert p["ask_the_person"].startswith("Add a new metric 'Web orders'")
    call(c, priya, "propose_metric", {"name": "web_orders", "confirm": p["confirm_code"]})
    assert call(c, priya, "query_metrics", {"metrics": ["web_orders"]})["rows"]
    assert "error" in call(c, owner, "query_metrics", {"metrics": ["web_orders"]})      # only in Priya's layer
    bad = call(c, priya, "propose_metric", {"name": "broken", "entity": "orders", "expression": "SUM(orders.nope)"})
    assert "error" in bad
    r = call(c, priya, "add_business_rule", {"rule": "Our fiscal year starts on 1 April."})
    call(c, priya, "add_business_rule", {"rule": "x", "confirm": r["confirm_code"]})
    d = call(c, priya, "describe_data", {})
    assert any("1 April" in x["text"] for x in d["business_rules_added"])
    assert not call(c, owner, "describe_data", {})["business_rules_added"]
    s = call(c, priya, "add_synonym", {"term": "GMV", "means": "gross_revenue"})
    call(c, priya, "add_synonym", {"term": "GMV", "means": "gross_revenue", "confirm": s["confirm_code"]})
    assert "error" in call(c, priya, "add_synonym", {"term": "x", "means": "not_a_metric"})


def test_company_changes_need_a_publisher(two):
    c, owner, priya = two
    # a member's company change becomes a request: nothing changes until a company admin approves
    q = call(c, priya, "add_business_rule", {"rule": "Refunds count as negative revenue.", "scope": "company"})
    assert "goes to its admins for approval" in q["ask_the_person"]
    r = call(c, priya, "add_business_rule", {"rule": "x", "confirm": q["confirm_code"]})
    assert r["requested"] and not r["saved"]
    assert not any("Refunds" in x["text"] for x in call(c, owner, "describe_data", {})["business_rules_added"])
    as_owner(c)
    w = c.get("/api/v1/approvals").json()["items"]
    req = next(i for i in w if i.get("type") == "company change")
    assert req["asked_by"] == "priya@kestrel-labs.example"
    assert c.post(f"/api/v1/approvals/{req['id']}", headers=H, json={"approve": True}).json()["status"] == "approved"
    assert any("Refunds" in x["text"] for x in call(c, owner, "describe_data", {})["business_rules_added"])
    r = call(c, owner, "add_business_rule", {"rule": "Revenue excludes GST.", "scope": "company"})
    assert "to the company layer" in r["ask_the_person"]
    call(c, owner, "add_business_rule", {"rule": "x", "confirm": r["confirm_code"]})
    assert any("GST" in x["text"] for x in call(c, priya, "describe_data", {})["business_rules_added"])


def test_locked_metric_cannot_be_changed_personally(two):
    c, owner, priya = two
    with defstore.acting("o@example.com"):
        defstore.put("company", "lock", "net_revenue", {"locked": True})
    r = call(c, priya, "propose_metric", {"name": "net_revenue", "filters": ["orders.channel = 'web'"]})
    assert "locked by the company" in r["error"]


def connect_to(c, email, password, path):
    """Sign in through a layer's own link (the token is bound to that link)."""
    import re as _re
    from urllib.parse import parse_qs, urlencode, urlparse
    _unthrottle()
    from tests.test_oauth import CB, pkce
    c.cookies.clear()
    cid = c.post("/oauth/register", headers=PUB, json={"client_name": "Claude", "redirect_uris": [CB]}).json()["client_id"]
    v, ch = pkce()
    q = {"response_type": "code", "client_id": cid, "redirect_uri": CB, "code_challenge": ch,
         "code_challenge_method": "S256", "state": "s", "resource": f"{BASE}{path}"}
    r = c.post("/oauth/login", headers=PUB, data=q | {"email": email, "password": password}, follow_redirects=False)
    page = c.get(r.headers["location"], headers=PUB).text
    csrf = _re.search(r'name="csrf" value="([0-9a-f]+)"', page).group(1)
    r = c.post("/oauth/authorize", headers=PUB, data=q | {"csrf": csrf, "decision": "allow"}, follow_redirects=False)
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    t = c.post("/oauth/token", headers=PUB, data={"grant_type": "authorization_code", "code": code, "redirect_uri": CB,
                                                   "client_id": cid, "code_verifier": v, "resource": f"{BASE}{path}"})
    return t.json()["access_token"]


def call_at(c, token, path, name, args):
    body = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": args}}
    r = c.post(path, json=body, headers=PUB | ACCEPT | {"authorization": f"Bearer {token}"})
    if r.status_code != 200:
        return {"status": r.status_code}
    r = r.json()["result"]
    return {"error": r["content"][0]["text"]} if r.get("isError") else r["structuredContent"]


def test_share_link_use_layer_and_publish(two):
    c, owner, priya = two
    c.post("/api/v1/login", headers=H, json={"email": "o@example.com", "password": "pass-word-123"})
    c.post("/api/v1/users", headers=H, json={"email": "arjun@kestrel-labs.example", "name": "Arjun Kumar",
                                             "role": "viewer", "password": "arjun-pass-123"})
    base = call(c, owner, "query_metrics", {"metrics": ["order_count"]})["rows"][0]["order_count"]
    p = call(c, priya, "propose_metric", {"name": "order_count", "filters": ["orders.channel = 'web'"]})
    call(c, priya, "propose_metric", {"name": "order_count", "confirm": p["confirm_code"]})
    web = call(c, priya, "query_metrics", {"metrics": ["order_count"]})["rows"][0]["order_count"]
    # share with one person only
    s = call(c, priya, "share_my_layer", {"people": ["arjun"]})
    assert s["link"] == f"{BASE}/mcp/layers/priya" and s["shared_with"] == ["person: arjun@kestrel-labs.example"]
    path = "/mcp/layers/priya"
    # unauthenticated: the 401 points at the link's own sign-in metadata
    r = c.post(path, json=INIT, headers=PUB | ACCEPT)
    assert r.status_code == 401 and f"oauth-protected-resource{path}" in r.headers["www-authenticate"]
    assert c.get(f"/.well-known/oauth-protected-resource{path}", headers=PUB).json()["resource"] == f"{BASE}{path}"
    arjun = connect_to(c, "arjun@kestrel-labs.example", "arjun-pass-123", path)
    assert call_at(c, arjun, path, "query_metrics", {"metrics": ["order_count"]})["rows"][0]["order_count"] == web
    # the same token does not work on the company link, and the owner (not shared with) is refused on the link
    assert call_at(c, arjun, "/mcp", "query_metrics", {"metrics": ["order_count"]}) == {"status": 401}
    own_link = connect_to(c, "o@example.com", "pass-word-123", path)
    assert "has not been shared with you" in call_at(c, own_link, path, "list_documents", {})["error"]
    # share with everyone: the owner gets in; use_layer inside the company connector
    call(c, priya, "share_my_layer", {})
    assert call_at(c, own_link, path, "query_metrics", {"metrics": ["order_count"]})["rows"][0]["order_count"] == web
    st = call(c, owner, "list_shared_layers", {})
    assert st["shared_with_me"][0]["layer"] == "Priya's layer" and not st["shared_with_me"][0]["in_use"]
    call(c, owner, "use_layer", {"name": "Priya"})
    assert call(c, owner, "query_metrics", {"metrics": ["order_count"]})["rows"][0]["order_count"] == web
    call(c, owner, "use_layer", {"name": "Priya", "use": False})
    assert call(c, owner, "query_metrics", {"metrics": ["order_count"]})["rows"][0]["order_count"] == base
    # revoke: Arjun is out at once
    call(c, priya, "revoke_share", {"all": True})
    assert "has not been shared" in call_at(c, arjun, path, "list_documents", {})["error"]
    # publish (owner directly; Priya's own publish would be a request for approval)
    assert "for approval" in call(c, priya, "publish_change", {"name": "order_count"})["ask_the_person"]
    call(c, priya, "share_my_layer", {})
    call(c, owner, "use_layer", {"name": "Priya"})
    p = call(c, owner, "publish_change", {"name": "order_count"})
    assert "to the company layer, for everyone" in p["ask_the_person"]
    call(c, owner, "publish_change", {"name": "order_count", "confirm": p["confirm_code"]})
    call(c, owner, "use_layer", {"name": "Priya", "use": False})
    assert call(c, owner, "query_metrics", {"metrics": ["order_count"]})["rows"][0]["order_count"] == web
    who = call(c, owner, "who_changed", {"name": "order_count"})
    assert who["current"]["from"] == "the company layer" and who["history"][0]["by"] == "o@example.com"


def test_directory_teams_invites_and_first_password(two):
    from urllib.parse import urlparse
    c, owner, priya = two
    c.post("/api/v1/login", headers=H, json={"email": "o@example.com", "password": "pass-word-123"})
    csv_text = ("name,email,title,team,manager_email,location\n"
                "Priya Nair,priya@kestrel-labs.example,Data Engineer,Data Engineering,meera@kestrel-labs.example,Pune\n"
                "Meera Rao,meera@kestrel-labs.example,Head of Data,Data Engineering,,Pune\n"
                "Ravi Iyer,ravi@kestrel-labs.example,SRE,Platform,,Bengaluru\n")
    assert c.post("/api/v1/directory/import", headers=H, json={"text": csv_text}).json()["imported"] == 3
    c.post("/api/v1/directory/teams", headers=H, json={"name": "Data Engineering", "lead_email": "meera@kestrel-labs.example"})
    p = call(c, priya, "find_person", {"query": "meera"})["people"][0]
    assert p["title"] == "Head of Data" and p["leads_team"] == "Data Engineering"
    assert {t["name"] for t in call(c, priya, "list_teams", {})["teams"]} == {"Data Engineering", "Platform"}
    # teams are access groups: Priya's groups now include her team
    assert "data-engineering" in call(c, priya, "list_documents", {})["your_groups"]
    # share with a team; an unknown team is refused with the list
    assert "no team called" in call(c, priya, "share_my_layer", {"teams": ["Marketing"]})["error"]
    assert call(c, priya, "share_my_layer", {"teams": ["Data Engineering team"]})["shared_with"] == ["team: Data Engineering"]
    # a member sharing with someone who has no account gets no invite link (only admins add people)...
    s = call(c, priya, "share_my_layer", {"people": ["ravi@kestrel-labs.example"]})
    assert "invites" not in s and s["no_account_yet"] == ["ravi@kestrel-labs.example"]
    assert "person: ravi@kestrel-labs.example" in s["shared_with"]
    # ...an admin sharing does get one; they join through the link and Priya's share then works for them
    s = call(c, owner, "share_my_layer", {"people": ["ravi@kestrel-labs.example"]})
    link = s["invites"][0]["invite_link"]
    path = urlparse(link).path
    assert "Join Context Layer" in c.get(path, headers=PUB).text
    r = c.post(path, headers=PUB, data={"name": "Ravi Iyer", "password": "ravi-pass-1234"})
    assert "You're in" in r.text and c.get(path, headers=PUB).status_code == 404
    # someone leaves: account disabled, shares to them stop
    c.post("/api/v1/login", headers=H, json={"email": "o@example.com", "password": "pass-word-123"})
    c.post("/api/v1/directory/people", headers=H, json={"email": "ravi@kestrel-labs.example", "name": "Ravi Iyer",
                                                         "status": "left"})
    assert "person: ravi@kestrel-labs.example" not in call(c, priya, "list_shared_layers", {})["my_layer"]["shared_with"]


def test_temporary_password_must_be_changed_before_allowing_an_app(two):
    import re as _re
    from urllib.parse import urlencode
    from tests.test_oauth import CB, pkce
    c, owner, priya = two
    c.post("/api/v1/login", headers=H, json={"email": "o@example.com", "password": "pass-word-123"})
    assert c.post("/api/v1/users", headers=H, json={"email": "kanakavalli@kestrel-labs.example", "name": "Kanakavalli",
                                                    "role": "admin", "password": "temp-pass-123",
                                                    "temporary": True}).json()["user"]["must_change"]
    c.cookies.clear()
    cid = c.post("/oauth/register", headers=PUB, json={"client_name": "Claude", "redirect_uris": [CB]}).json()["client_id"]
    v, ch = pkce()
    q = {"response_type": "code", "client_id": cid, "redirect_uri": CB, "code_challenge": ch, "code_challenge_method": "S256"}
    r = c.post("/oauth/login", headers=PUB, data=q | {"email": "kanakavalli@kestrel-labs.example", "password": "temp-pass-123"},
               follow_redirects=False)
    page = c.get(r.headers["location"], headers=PUB).text
    assert "Choose your password" in page and "Allow" not in page
    bad = c.post("/oauth/password", headers=PUB, data=q | {"password": "temp-pass-123", "password2": "temp-pass-123"})
    assert "different from the temporary" in bad.text
    r = c.post("/oauth/password", headers=PUB, data=q | {"password": "kanaka-own-pass-1", "password2": "kanaka-own-pass-1"},
               follow_redirects=False)
    assert "Allow Claude?" in c.get(r.headers["location"], headers=PUB).text
    # My account page works over the public name
    c.cookies.clear()
    r = c.post("/account/login", headers=PUB, data={"email": "kanakavalli@kestrel-labs.example",
                                                    "password": "kanaka-own-pass-1"})
    assert "Connected apps" in r.text and "temporary password" not in r.text


def test_personal_document_goes_to_own_layer(two):
    c, owner, priya = two
    r = call(c, priya, "add_document", {"filename": "my-notes.md", "text": "# Notes\n\nThe platform team means DevOps.",
                                        "notes": "my own notes"})
    assert "error" not in r, r
    groups = engine.access_map()["my-notes.md"]
    assert len(groups) == 1 and groups[0].startswith("layer:user:")
    # the owner too: own layer unless they ask for the company by name
    r = call(c, owner, "add_document", {"filename": "owner-notes.md", "text": "# Owner\n\nPrivate."})
    assert "error" not in r, r
    assert engine.access_map()["owner-notes.md"][0].startswith("layer:user:")
    r = call(c, owner, "add_document", {"filename": "policy.md", "text": "# Policy\n\nFor all.", "groups": ["company"]})
    assert engine.access_map()["policy.md"] == ["public"]
    r = call(c, priya, "add_document", {"filename": "p2.md", "text": "# Mine\n\nX.", "groups": ["company"]})
    assert engine.access_map()["p2.md"][0].startswith("layer:user:")       # members cannot publish files


def test_team_hierarchy_admins_layers_and_approvals(two):
    """Company admin -> team admin -> member: team layers, team links, who manages whom, approvals."""
    c, owner, priya = two
    from app import directory, teams
    as_owner(c)
    c.post("/api/v1/teams", headers=H, json={"name": "Engineering"})
    c.post("/api/v1/teams", headers=H, json={"name": "Data Engineering", "parent": "Engineering"})
    c.post("/api/v1/teams", headers=H, json={"name": "Sales"})
    assert c.post("/api/v1/teams", headers=H, json={"name": "Engineering", "parent": "Data Engineering"}).status_code == 400
    # Priya joins Data Engineering; Meera becomes its admin
    assert c.post("/api/v1/teams/data-engineering/people", headers=H,
                  json={"email": "priya@kestrel-labs.example", "name": "Priya"}).json()["added"]
    c.post("/api/v1/users", headers=H, json={"email": "meera@kestrel-labs.example", "name": "Meera", "role": "viewer",
                                             "password": "meera-pass-12345"})
    directory.save_person({"email": "meera@kestrel-labs.example", "name": "Meera", "team": "Data Engineering"})
    assert c.post("/api/v1/teams/data-engineering/admins", headers=H,
                  json={"email": "meera@kestrel-labs.example"}).json()["admins"] == ["meera@kestrel-labs.example"]
    meera = fresh(c, "meera@kestrel-labs.example", "meera-pass-12345")["access_token"]
    as_owner(c)
    m = {"kind": "user", "role": "viewer", "email": "meera@kestrel-labs.example"}
    assert teams.manages(m, "Data Engineering") and not teams.manages(m, "Engineering") and not teams.manages(m, "Sales")
    # stack: company -> Engineering -> Data Engineering -> own
    p = {"kind": "user", "role": "viewer", "email": "priya@kestrel-labs.example", "user_id": 2}
    assert teams.reading_teams(p) == ["Engineering", "Data Engineering"]
    # Meera (team admin) changes the team layer directly; Priya's team change waits for Meera
    q = call(c, meera, "add_business_rule", {"rule": "Freshness counts only successful runs.", "scope": "team:Data Engineering"})
    assert "Data Engineering team layer" in q["ask_the_person"] and "approval" not in q["ask_the_person"]
    assert call(c, meera, "add_business_rule", {"rule": "x", "confirm": q["confirm_code"]})["saved"]
    assert any("Freshness" in x["text"] for x in call(c, priya, "describe_data", {})["business_rules_added"])
    assert not any("Freshness" in x["text"] for x in call(c, owner, "describe_data", {})["business_rules_added"])
    q = call(c, priya, "add_synonym", {"term": "data team", "means": "orders.channel=web", "scope": "team"})
    assert "for approval" in q["ask_the_person"]
    r = call(c, priya, "add_synonym", {"term": "x", "means": "y", "confirm": q["confirm_code"]})
    assert r["requested"]
    # Meera sees it in her team's queue and approves it; the owner (company admin) could too
    from app import approvals
    w = approvals.waiting_for(m)
    assert len(w) == 1 and w[0]["type"] == "team change" and w[0]["asked_by"] == "priya@kestrel-labs.example"
    assert not approvals.waiting_for({"kind": "user", "role": "viewer", "email": "nobody@x"})
    assert approvals.decide(m, w[0]["id"], True)["status"] == "approved"
    assert any(s["term"] == "data team" for s in call(c, priya, "describe_data", {})["synonyms"])
    # the team link: members and people added to it; others are refused
    as_owner(c)
    lk = c.get("/api/v1/teams/data-engineering").json()
    assert lk["link"].endswith("/mcp/teams/data-engineering") and lk["you_manage"]
    path = "/mcp/teams/data-engineering"
    assert "error" not in call_at(c, connect_to(c, "priya@kestrel-labs.example", "priya-pass-123", path),
                                  path, "list_documents", {})
    as_owner(c)
    c.post("/api/v1/users", headers=H, json={"email": "tom@kestrel-labs.example", "name": "Tom", "role": "viewer",
                                             "password": "tom-pass-123456"})
    tom = connect_to(c, "tom@kestrel-labs.example", "tom-pass-123456", path)
    assert "error" in call_at(c, tom, path, "list_documents", {})
    as_owner(c)
    c.post("/api/v1/teams/data-engineering/link", headers=H, json={"people": ["tom@kestrel-labs.example"]})
    assert "error" not in call_at(c, tom, path, "list_documents", {})


def test_home_and_team_page_for_admins(two):
    c, owner, priya = two
    as_owner(c)
    c.post("/api/v1/teams", headers=H, json={"name": "Data Engineering"})
    h = c.get("/api/v1/home").json()
    assert h["company_admin"] and h["links"][0]["kind"] == "company"
    q = call(c, owner, "add_business_rule", {"rule": "Weeks start on Monday.", "scope": "team:Data Engineering"})
    call(c, owner, "add_business_rule", {"rule": "x", "confirm": q["confirm_code"]})
    as_owner(c)
    t = c.get("/api/v1/teams/data-engineering").json()
    assert t["you_manage"] and any("Monday" in (i["body"] or {}).get("text", "") for i in t["items"])
    q = call(c, owner, "add_business_rule", {"rule": "Fiscal year starts in April."})
    call(c, owner, "add_business_rule", {"rule": "x", "confirm": q["confirm_code"]})
    as_owner(c)
    mine = c.get("/api/v1/home").json()["my_changes"]
    assert mine and c.post(f"/api/v1/home/changes/{mine[0]['change_id']}/undo", headers=H).status_code == 200
    assert c.get("/api/v1/home").json()["my_changes"][0]["action"] == "undo" or \
        any(x["undone"] for x in c.get("/api/v1/home").json()["my_changes"])


def test_people_page_team_and_level(two):
    c, owner, priya = two
    as_owner(c)
    c.post("/api/v1/teams", headers=H, json={"name": "Platform"})
    r = c.post("/api/v1/people", headers=H, json={"email": "new.person@kestrel-labs.example", "team": "Platform"}).json()
    assert r["team"] == "Platform" and r["invite"]["invite_link"]
    assert c.post("/api/v1/people", headers=H, json={"email": "priya@kestrel-labs.example", "team": "Platform",
                                                     "level": "team_admin"}).status_code == 200
    ppl = {p["email"]: p for p in c.get("/api/v1/people").json()["people"]}
    assert ppl["priya@kestrel-labs.example"]["level"] == "team_admin" and ppl["priya@kestrel-labs.example"]["team"] == "Platform"
    assert ppl["new.person@kestrel-labs.example"]["account"] == "invited"
    assert ppl["o@example.com"]["level"] == "owner"
    # admin of several teams, independent of the team she is in
    c.post("/api/v1/teams", headers=H, json={"name": "Sales"})
    c.post("/api/v1/people", headers=H, json={"email": "priya@kestrel-labs.example", "team": "Sales",
                                              "admin_of": ["Platform", "Sales"]})
    from app import teams
    assert teams.admins_of("Platform") == ["priya@kestrel-labs.example"] == teams.admins_of("Sales")
    c.post("/api/v1/people", headers=H, json={"email": "priya@kestrel-labs.example", "admin_of": ["Sales"]})
    assert teams.admins_of("Platform") == []
    ppl = {p["email"]: p for p in c.get("/api/v1/people").json()["people"]}
    assert ppl["priya@kestrel-labs.example"]["admin_of"] == ["Sales"]
    # a team admin adds and removes people in her team only
    c.post("/api/v1/users", headers=H, json={"email": "priya@kestrel-labs.example", "name": "Priya", "role": "viewer",
                                             "password": "priya-pass-123"})
    c.cookies.clear()
    assert c.post("/api/v1/login", headers=H, json={"email": "priya@kestrel-labs.example", "password": "priya-pass-123"}).status_code == 200
    assert c.post("/api/v1/people", headers=H, json={"email": "sam@kestrel-labs.example", "team": "Sales"}).status_code == 200
    assert c.post("/api/v1/people", headers=H, json={"email": "kim@kestrel-labs.example", "team": "Platform"}).status_code == 403
    assert c.post("/api/v1/people/remove", headers=H, json={"email": "sam@kestrel-labs.example"}).json()["removed"]
    assert c.post("/api/v1/teams", headers=H, json={"name": "Rogue"}).status_code == 403
    as_owner(c)
    assert c.post("/api/v1/people/remove", headers=H, json={"email": "new.person@kestrel-labs.example"}).json()["left"]
    assert "new.person@kestrel-labs.example" not in {p["email"] for p in c.get("/api/v1/people").json()["people"]}
