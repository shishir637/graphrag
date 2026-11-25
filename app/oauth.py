"""Sign in to Context Layer from Claude, ChatGPT and other MCP clients (OAuth 2.1, as the MCP spec asks).

The AI comes from the user's own Claude or ChatGPT account. This only proves who the user is to Context Layer,
so their documents, metrics and graph follow their access groups.

  /.well-known/oauth-protected-resource[/mcp]   where to sign in for /mcp (RFC 9728)
  /.well-known/oauth-authorization-server       endpoints and features (RFC 8414)
  /oauth/register                               clients register themselves (RFC 7591), or
                                                identify by a metadata URL as client_id (Client ID Metadata Documents)
  /oauth/authorize                              sign-in page, then "Allow <app>?" (authorization code + PKCE S256)
  /oauth/token                                  code -> access token (1 h) + refresh token (30 days, rotated)
  /oauth/revoke                                 sign out an app (RFC 7009)
"""
import base64
import hashlib
import hmac
import html
import ipaddress
import json
import os
import secrets
import socket
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from . import core
from .public import base_url

router = APIRouter()
SCOPE = "contextlayer"
COOKIE = "cl_session"
CODE_TTL_S = 600
CIMD_TTL_S = 86400
LOOPBACK = {"localhost", "127.0.0.1", "[::1]", "::1"}

_hits: dict[str, deque] = defaultdict(deque)


def _limited(key: str, limit: int, window: int = 300) -> bool:
    q, t = _hits[key], time.time()
    while q and q[0] < t - window:
        q.popleft()
    if len(q) >= limit:
        return True
    q.append(t)
    return False


def _ip(request: Request) -> str:
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")


def _later(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _err(error: str, desc: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": error, "error_description": desc}, status,
                        headers={"Cache-Control": "no-store", **CORS})


CORS = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Authorization, Content-Type, MCP-Protocol-Version"}


def _secret() -> bytes:
    s = core.get_setting("oauth_hmac")
    if not s:
        s = secrets.token_hex(32)
        core.set_setting("oauth_hmac", s)
    return bytes.fromhex(s)


# ------------------------------------------------------------------ metadata

def resource_url(request: Request) -> str:
    return base_url(request.scope) + "/mcp"


def server_metadata(base: str) -> dict:
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "revocation_endpoint": f"{base}/oauth/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
        "revocation_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
        "scopes_supported": [SCOPE],
        "client_id_metadata_document_supported": True,
        "authorization_response_iss_parameter_supported": True,
    }


@router.options("/.well-known/{rest:path}", include_in_schema=False)
@router.options("/oauth/{rest:path}", include_in_schema=False)
def _preflight(rest: str):
    return Response(status_code=204, headers=CORS)


@router.get("/.well-known/oauth-protected-resource", include_in_schema=False)
@router.get("/.well-known/oauth-protected-resource/mcp", include_in_schema=False)
def protected_resource(request: Request):
    base = base_url(request.scope)
    return JSONResponse({"resource": f"{base}/mcp", "authorization_servers": [base], "scopes_supported": [SCOPE],
                         "bearer_methods_supported": ["header"], "resource_name": "Context Layer"}, headers=CORS)


@router.get("/.well-known/oauth-authorization-server", include_in_schema=False)
@router.get("/.well-known/oauth-authorization-server/mcp", include_in_schema=False)
@router.get("/.well-known/openid-configuration", include_in_schema=False)
def auth_server(request: Request):
    return JSONResponse(server_metadata(base_url(request.scope)), headers=CORS)


# ------------------------------------------------------------------ clients

def _redirect_ok(uri: str) -> bool:
    u = urlparse(uri)
    if u.fragment or not u.netloc:
        return False
    if u.scheme == "https":
        return True
    return u.scheme == "http" and (u.hostname or "") in LOOPBACK


def _same_redirect(registered: str, given: str) -> bool:
    if registered == given:
        return True
    a, b = urlparse(registered), urlparse(given)
    # loopback redirects may use any port (RFC 8252 7.3)
    return (a.hostname in LOOPBACK and a.hostname == b.hostname and a.scheme == b.scheme == "http"
            and a.path == b.path and a.query == b.query)


def public_host(host: str) -> bool:
    """Every address the name resolves to is on the public internet (no fetching from inside the network)."""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    return bool(infos) and all(ipaddress.ip_address(i[4][0].split("%")[0]).is_global for i in infos)


def _fetch_cimd(client_id: str) -> dict | None:
    """Client ID Metadata Document: the client_id is an https URL of a JSON file describing the client."""
    u = urlparse(client_id)
    if u.scheme != "https" or not u.hostname or u.path in ("", "/") or not public_host(u.hostname):
        return None
    try:
        import httpx
        with httpx.Client(timeout=5.0, follow_redirects=False) as c:
            r = c.get(client_id, headers={"Accept": "application/json"})
        if r.status_code != 200 or len(r.content) > 65536:
            return None
        doc = r.json()
    except Exception:                                      # noqa: BLE001 - treated as an unknown client
        return None
    uris = doc.get("redirect_uris")
    if doc.get("client_id") != client_id or not isinstance(uris, list) or not uris:
        return None
    uris = [x for x in uris if isinstance(x, str) and _redirect_ok(x)][:10]
    return {"name": str(doc.get("client_name") or u.hostname)[:80], "redirect_uris": uris} if uris else None


def get_client(client_id: str) -> dict | None:
    if not client_id or len(client_id) > 500:
        return None
    r = core.db().execute("SELECT * FROM oauth_clients WHERE client_id = ?", (client_id,)).fetchone()
    fresh = r is not None and (r["kind"] != "cimd" or
                               datetime.fromisoformat(r["created"]) > datetime.now(timezone.utc) - timedelta(seconds=CIMD_TTL_S))
    if not fresh and client_id.startswith("https://"):
        doc = _fetch_cimd(client_id)
        if doc is None:
            return None
        core.db().execute("INSERT OR REPLACE INTO oauth_clients VALUES (?, NULL, ?, ?, 'cimd', ?)",
                          (client_id, doc["name"], json.dumps(doc["redirect_uris"]), core.now()))
        r = core.db().execute("SELECT * FROM oauth_clients WHERE client_id = ?", (client_id,)).fetchone()
    if r is None:
        return None
    return {"client_id": r["client_id"], "name": r["name"], "redirect_uris": json.loads(r["redirect_uris"]),
            "secret_hash": r["secret_hash"], "kind": r["kind"]}


@router.post("/oauth/register", include_in_schema=False)
async def register(request: Request):
    if _limited("reg:" + _ip(request), 20, 3600):
        return _err("slow_down", "Too many registrations. Try again later.", 429)
    try:
        body = await request.json()
        assert isinstance(body, dict)
    except Exception:                                      # noqa: BLE001
        return _err("invalid_client_metadata", "Send client metadata as JSON.")
    uris = body.get("redirect_uris")
    if not isinstance(uris, list) or not uris or len(uris) > 10 or \
            not all(isinstance(x, str) and len(x) < 500 and _redirect_ok(x) for x in uris):
        return _err("invalid_redirect_uri", "redirect_uris must be https URLs (or http on localhost).")
    method = body.get("token_endpoint_auth_method") or "none"
    if method not in ("none", "client_secret_post", "client_secret_basic"):
        return _err("invalid_client_metadata", "Unsupported token_endpoint_auth_method.")
    name = " ".join(str(body.get("client_name") or urlparse(uris[0]).hostname or "MCP client").split())[:80]
    cid = "clc_" + secrets.token_urlsafe(16)
    secret = new_secret = None
    if method != "none":
        new_secret = "clcs_" + secrets.token_urlsafe(28)
        secret = core.token_hash(new_secret)
    core.db().execute("INSERT INTO oauth_clients VALUES (?, ?, ?, ?, 'dcr', ?)",
                      (cid, secret, name, json.dumps(uris), core.now()))
    core.log_event("system", name, "oauth.register", cid, {"redirect_uris": uris})
    out = {"client_id": cid, "client_id_issued_at": int(time.time()), "client_name": name, "redirect_uris": uris,
           "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
           "token_endpoint_auth_method": method, "scope": SCOPE}
    if new_secret:
        out |= {"client_secret": new_secret, "client_secret_expires_at": 0}
    return JSONResponse(out, 201, headers={"Cache-Control": "no-store", **CORS})


# ------------------------------------------------------------------ sign-in and consent pages

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} - Context Layer</title><style>
body{{font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;background:#f6f6f4;color:#1f1f1d;margin:0;display:flex;min-height:100vh;align-items:center;justify-content:center}}
main{{background:#fff;border:1px solid #e4e3df;border-radius:12px;padding:28px 32px;width:100%;max-width:400px;box-sizing:border-box}}
h1{{font-size:20px;margin:0 0 4px}} .muted{{color:#6b6a65;font-size:13px}} label{{display:block;margin:14px 0 4px;font-size:13px;font-weight:600}}
input[type=email],input[type=password],input[type=file]{{width:100%;box-sizing:border-box;padding:9px 10px;border:1px solid #d6d5d0;border-radius:8px;font:inherit}}
button{{font:inherit;padding:9px 16px;border-radius:8px;border:1px solid #1f1f1d;background:#1f1f1d;color:#fff;cursor:pointer;margin-top:18px}}
button.secondary{{background:#fff;color:#1f1f1d;border-color:#d6d5d0;margin-left:8px}} ul{{padding-left:18px;margin:10px 0}}
.err{{background:#fdecea;color:#8a1c12;padding:8px 10px;border-radius:8px;margin-top:12px;font-size:14px}}
.ok{{background:#e9f5ec;color:#1c5a2b;padding:8px 10px;border-radius:8px;margin-top:12px;font-size:14px}}
code{{background:#f1f0ec;padding:1px 5px;border-radius:4px}}
</style></head><body><main><div class="muted">Context Layer{org}</div>{body}</main></body></html>"""

CSP = "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"


def page(title: str, body: str, status: int = 200) -> HTMLResponse:
    org = core.get_setting("organisation", "")
    return HTMLResponse(PAGE.format(title=html.escape(title), body=body, org=f" · {html.escape(org)}" if org else ""),
                        status, headers={"Content-Security-Policy": CSP, "Cache-Control": "no-store",
                                         "X-Frame-Options": "DENY"})


AUTH_PARAMS = ("response_type", "client_id", "redirect_uri", "code_challenge", "code_challenge_method", "state",
               "scope", "resource")


def _hidden(params: dict) -> str:
    return "".join(f'<input type="hidden" name="{k}" value="{html.escape(v)}">' for k, v in params.items() if v)


def _back(redirect_uri: str, issuer: str, **kw) -> RedirectResponse:
    q = urlencode({k: v for k, v in kw.items() if v} | {"iss": issuer})
    return RedirectResponse(redirect_uri + ("&" if "?" in redirect_uri else "?") + q, 303)


def _csrf(session: str, params: dict) -> str:
    msg = "|".join([core.token_hash(session), params.get("client_id", ""), params.get("redirect_uri", "")])
    return hmac.new(_secret(), msg.encode(), hashlib.sha256).hexdigest()


def _check_request(request: Request, params: dict):
    """(client, None) when the client and redirect are valid, else (None, error response).
    Errors about the client or redirect are shown here; others go back to the app (RFC 6749 4.1.2.1)."""
    client = get_client(params.get("client_id", ""))
    if client is None:
        return None, page("Unknown app", "<h1>Unknown app</h1><p>This app is not registered with Context Layer. "
                                         "Remove the connector and add it again.</p>", 400)
    ru = params.get("redirect_uri") or (client["redirect_uris"][0] if len(client["redirect_uris"]) == 1 else "")
    if not ru or not any(_same_redirect(r, ru) for r in client["redirect_uris"]):
        return None, page("Wrong return address", "<h1>Wrong return address</h1><p>The app asked to return to an "
                                                  "address it did not register.</p>", 400)
    params["redirect_uri"] = ru
    issuer = base_url(request.scope)
    st = params.get("state")
    if params.get("response_type") != "code":
        return None, _back(ru, issuer, error="unsupported_response_type", state=st)
    if not params.get("code_challenge") or params.get("code_challenge_method") != "S256":
        return None, _back(ru, issuer, error="invalid_request", error_description="PKCE with S256 is required.", state=st)
    res = params.get("resource")
    if res and res.rstrip("/").lower() != resource_url(request).rstrip("/").lower():
        return None, _back(ru, issuer, error="invalid_target", error_description="Unknown resource.", state=st)
    scopes = set((params.get("scope") or SCOPE).split())
    if not scopes <= {SCOPE, "openid", "profile", "email", "offline_access"}:
        return None, _back(ru, issuer, error="invalid_scope", state=st)
    return client, None


def _login_form(params: dict, error: str = "") -> HTMLResponse:
    err = f'<div class="err">{html.escape(error)}</div>' if error else ""
    return page("Sign in", f"""<h1>Sign in</h1><p class="muted">to connect an app to Context Layer</p>{err}
<form method="post" action="/oauth/login">{_hidden(params)}
<label for="e">Email</label><input id="e" name="email" type="email" autocomplete="username" required autofocus>
<label for="p">Password</label><input id="p" name="password" type="password" autocomplete="current-password" required>
<button type="submit">Sign in</button></form>""", 401 if error else 200)


@router.get("/oauth/authorize", include_in_schema=False)
def authorize(request: Request):
    params = {k: request.query_params.get(k, "") for k in AUTH_PARAMS}
    client, bad = _check_request(request, params)
    if bad:
        return bad
    session = request.cookies.get(COOKIE)
    user = core.session_user(session)
    if user is None:
        return _login_form(params)
    host = urlparse(params["redirect_uri"]).hostname
    admin = user["role"] in ("owner", "admin")
    can = ["Search and read the documents you can see", "Answer questions about approved business metrics",
           "Explore the knowledge graph"] + (["Add documents and data tables you upload in the chat"] if admin else [])
    items = "".join(f"<li>{html.escape(c)}</li>" for c in can)
    return page("Allow access", f"""<h1>Allow {html.escape(client['name'])}?</h1>
<p class="muted">Signed in as {html.escape(user['email'])}</p>
<p><b>{html.escape(client['name'])}</b> wants to use Context Layer as you. It will be able to:</p><ul>{items}</ul>
<p class="muted">You will return to <code>{html.escape(host or '')}</code>. You can disconnect it at any time from Agents in Context Layer.</p>
<form method="post" action="/oauth/authorize">{_hidden(params)}<input type="hidden" name="csrf" value="{_csrf(session, params)}">
<button name="decision" value="allow" type="submit">Allow</button><button class="secondary" name="decision" value="deny" type="submit">Cancel</button></form>""")


@router.post("/oauth/login", include_in_schema=False)
async def login(request: Request):
    form = await request.form()
    params = {k: str(form.get(k, "")) for k in AUTH_PARAMS}
    client, bad = _check_request(request, params)
    if bad:
        return bad
    email = str(form.get("email", ""))
    if _limited("login:" + _ip(request), 8) or _limited("login:" + email.lower(), 8):
        return _login_form(params, "Too many attempts. Wait a few minutes and try again.")
    r = core.login(email, str(form.get("password", "")))
    if r is None:
        core.log_event("user", email[:200], "oauth.login", client["name"], {}, ok=False)
        return _login_form(params, "Email or password is not correct.")
    user, token = r
    core.log_event("user", user["email"], "oauth.login", client["name"])
    resp = RedirectResponse("/oauth/authorize?" + urlencode({k: v for k, v in params.items() if v}), 303)
    resp.set_cookie(COOKIE, token, max_age=core.SESSION_DAYS * 86400, httponly=True, samesite="lax",
                    secure=base_url(request.scope).startswith("https://"), path="/")
    return resp


@router.post("/oauth/authorize", include_in_schema=False)
async def decide(request: Request):
    form = await request.form()
    params = {k: str(form.get(k, "")) for k in AUTH_PARAMS}
    client, bad = _check_request(request, params)
    if bad:
        return bad
    session = request.cookies.get(COOKIE)
    user = core.session_user(session)
    if user is None:
        return _login_form(params, "Your sign-in expired. Sign in again.")
    if not hmac.compare_digest(str(form.get("csrf", "")), _csrf(session, params)):
        return page("Try again", "<h1>Try again</h1><p>This page expired. Go back to the app and connect again.</p>", 400)
    issuer = base_url(request.scope)
    if form.get("decision") != "allow":
        core.log_event("user", user["email"], "oauth.deny", client["name"])
        return _back(params["redirect_uri"], issuer, error="access_denied", state=params.get("state"))
    code = "clo_" + secrets.token_urlsafe(32)
    core.db().execute("INSERT INTO oauth_codes VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                      (core.token_hash(code), client["client_id"], user["id"], params["redirect_uri"],
                       params["code_challenge"], SCOPE, params.get("resource") or resource_url(request), _later(CODE_TTL_S)))
    core.db().execute("DELETE FROM oauth_codes WHERE expires < ?", (core.now(),))
    core.log_event("user", user["email"], "oauth.allow", client["name"], {"client_id": client["client_id"]})
    return _back(params["redirect_uri"], issuer, code=code, state=params.get("state"))


# ------------------------------------------------------------------ tokens

def _client_auth(request: Request, form) -> tuple[dict | None, JSONResponse | None]:
    cid, secret = str(form.get("client_id", "")), str(form.get("client_secret", ""))
    h = request.headers.get("authorization", "")
    if h[:6].lower() == "basic ":
        try:
            from urllib.parse import unquote
            a, b = base64.b64decode(h[6:]).decode().split(":", 1)
            cid, secret = unquote(a), unquote(b)
        except Exception:                                  # noqa: BLE001
            return None, _err("invalid_client", "Bad client credentials.", 401)
    client = get_client(cid)
    if client is None:
        return None, _err("invalid_client", "Unknown client.", 401)
    if client["secret_hash"] and not hmac.compare_digest(core.token_hash(secret), client["secret_hash"]):
        return None, _err("invalid_client", "Bad client credentials.", 401)
    return client, None


def issue(client: dict, user_id: int, resource: str | None, family: str | None = None) -> dict:
    access, refresh = core.new_token("cla_"), core.new_token("clr_")
    fam = family or secrets.token_hex(8)
    with core.tx() as c:
        for tok, kind, ttl in ((access, "access", core.ACCESS_TOKEN_S), (refresh, "refresh", core.REFRESH_TOKEN_DAYS * 86400)):
            c.execute("INSERT INTO oauth_tokens (token_hash, kind, family, client_id, client_name, user_id, scope, "
                      "resource, created, expires) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (core.token_hash(tok), kind, fam, client["client_id"], client["name"], user_id, SCOPE,
                       resource, core.now(), _later(ttl)))
        c.execute("DELETE FROM oauth_tokens WHERE expires < ?", (core.now(),))
    return {"access_token": access, "token_type": "Bearer", "expires_in": core.ACCESS_TOKEN_S,
            "refresh_token": refresh, "scope": SCOPE}


def _revoke_family(family: str):
    core.db().execute("UPDATE oauth_tokens SET revoked = ? WHERE family = ? AND revoked IS NULL", (core.now(), family))


@router.post("/oauth/token", include_in_schema=False)
async def token(request: Request):
    form = await request.form()
    if _limited("tok:" + _ip(request), 60, 60):
        return _err("slow_down", "Too many requests.", 429)
    client, bad = _client_auth(request, form)
    if bad:
        return bad
    grant = form.get("grant_type")
    if grant == "authorization_code":
        code = str(form.get("code", ""))
        with core.tx() as c:
            row = c.execute("SELECT * FROM oauth_codes WHERE code_hash = ?", (core.token_hash(code),)).fetchone()
            c.execute("DELETE FROM oauth_codes WHERE code_hash = ?", (core.token_hash(code),))   # single use
        if row is None or row["expires"] < core.now() or row["client_id"] != client["client_id"]:
            return _err("invalid_grant", "The code is invalid, used or expired.")
        ru = str(form.get("redirect_uri", "")) or row["redirect_uri"]
        if ru != row["redirect_uri"]:
            return _err("invalid_grant", "redirect_uri does not match.")
        verifier = str(form.get("code_verifier", ""))
        expect = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        if not (43 <= len(verifier) <= 128) or not hmac.compare_digest(expect, row["challenge"]):
            return _err("invalid_grant", "PKCE check failed.")
        res = str(form.get("resource", "")) or row["resource"]
        if row["resource"] and res.rstrip("/").lower() != row["resource"].rstrip("/").lower():
            return _err("invalid_target", "Unknown resource.")
        u = core.db().execute("SELECT disabled FROM users WHERE id = ?", (row["user_id"],)).fetchone()
        if u is None or u["disabled"]:
            return _err("invalid_grant", "The user is disabled.")
        return JSONResponse(issue(client, row["user_id"], row["resource"]), headers={"Cache-Control": "no-store", **CORS})
    if grant == "refresh_token":
        rt = str(form.get("refresh_token", ""))
        row = core.db().execute("SELECT t.*, u.disabled FROM oauth_tokens t JOIN users u ON u.id = t.user_id "
                                "WHERE t.token_hash = ? AND t.kind = 'refresh'", (core.token_hash(rt),)).fetchone()
        if row is None or row["client_id"] != client["client_id"]:
            return _err("invalid_grant", "Unknown refresh token.")
        if row["revoked"]:
            _revoke_family(row["family"])               # a used refresh token came back: sign this app out
            return _err("invalid_grant", "This refresh token was already used. Connect again.")
        if row["expires"] < core.now() or row["disabled"]:
            return _err("invalid_grant", "Expired. Connect again.")
        core.db().execute("UPDATE oauth_tokens SET revoked = ? WHERE token_hash = ?", (core.now(), row["token_hash"]))
        core.db().execute("UPDATE oauth_tokens SET revoked = ? WHERE family = ? AND kind = 'access' AND revoked IS NULL",
                          (core.now(), row["family"]))
        return JSONResponse(issue(client, row["user_id"], row["resource"], row["family"]),
                            headers={"Cache-Control": "no-store", **CORS})
    return _err("unsupported_grant_type", "Use authorization_code or refresh_token.")


@router.post("/oauth/revoke", include_in_schema=False)
async def revoke(request: Request):
    form = await request.form()
    client, bad = _client_auth(request, form)
    if bad:
        return bad
    row = core.db().execute("SELECT family, client_id FROM oauth_tokens WHERE token_hash = ?",
                            (core.token_hash(str(form.get("token", ""))),)).fetchone()
    if row and row["client_id"] == client["client_id"]:
        _revoke_family(row["family"])
    return Response(status_code=200, headers=CORS)


# ------------------------------------------------------------------ signed-in apps (for the admin web app)

def grants(user_id: int | None = None) -> list[dict]:
    q = ("SELECT t.family, t.client_name, t.client_id, t.user_id, u.email, min(t.created) AS since, "
         "max(t.last_used) AS last_used FROM oauth_tokens t JOIN users u ON u.id = t.user_id "
         "WHERE t.revoked IS NULL AND t.expires > ? AND t.kind = 'refresh'")
    args: list = [core.now()]
    if user_id is not None:
        q += " AND t.user_id = ?"
        args.append(user_id)
    rows = core.db().execute(q + " GROUP BY t.family ORDER BY since DESC", args).fetchall()
    used = {r["family"]: r["lu"] for r in core.db().execute(
        "SELECT family, max(last_used) AS lu FROM oauth_tokens GROUP BY family")}
    return [{"id": r["family"], "app": r["client_name"], "user": r["email"], "user_id": r["user_id"],
             "since": r["since"], "last_used": used.get(r["family"])} for r in rows]


def revoke_grant(family: str, user_id: int | None = None) -> bool:
    q, args = "SELECT 1 FROM oauth_tokens WHERE family = ?", [family]
    if user_id is not None:
        q += " AND user_id = ?"
        args.append(user_id)
    if not core.db().execute(q, args).fetchone():
        return False
    _revoke_family(family)
    return True


def www_authenticate(scope, error: bool) -> bytes:
    base = base_url(scope)
    v = f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource/mcp", scope="{SCOPE}"'
    if error:
        v += ', error="invalid_token"'
    return v.encode()


def enabled() -> bool:
    return os.environ.get("CL_OAUTH", "1") != "0"
