"""Which host names the app answers to, and what each may reach.

  local   127.0.0.1, localhost, ::1 (and the Docker service name "app"): everything - admin web app, API, MCP
  public  names in CL_PUBLIC_HOSTS (comma separated; "*.trycloudflare.com" style wildcards allowed), e.g. a
          Cloudflare Tunnel hostname: only the MCP endpoint, its sign-in pages (/.well-known, /oauth), one-time
          upload links (/upload) and the health check, unless
          CL_PUBLIC_ADMIN=1 also publishes the admin web app (put Cloudflare Access or a VPN in front first)
  other   refused with 421 (protects against DNS rebinding)

The public MCP link shown to admins comes from CL_PUBLIC_URL, or is read from a Cloudflare Quick Tunnel's
metrics endpoint (docker compose --profile quicktunnel) when one is running.
"""
import fnmatch
import os
import time

LOCAL = {"127.0.0.1", "localhost", "::1", "[::1]", "app", "testserver"}
PUBLIC_PATHS = ("/mcp", "/api/v1/health", "/.well-known", "/oauth", "/upload", "/invite", "/account")


def _host(scope) -> str:
    h = ""
    for k, v in scope.get("headers", []):
        if k == b"host":
            h = v.decode("latin-1").lower()
            break
    if h.startswith("["):                                  # [::1]:8080
        return h.split("]")[0] + "]"
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def public_patterns() -> list[str]:
    return [h.strip().lower() for h in os.environ.get("CL_PUBLIC_HOSTS", "").split(",") if h.strip()]


def classify(scope) -> str:
    h = _host(scope)
    if not h or h in LOCAL:
        return "local"
    if any(fnmatch.fnmatch(h, p) for p in public_patterns()):
        return "public"
    return "other"


def allowed(scope) -> tuple[bool, int, str]:
    """(ok, status, message) for an incoming HTTP request."""
    kind = classify(scope)
    if kind == "local":
        return True, 200, ""
    if kind == "other":
        return False, 421, "Unknown host name. Add it to CL_PUBLIC_HOSTS to serve Context Layer under it."
    path = scope.get("path", "")
    if os.environ.get("CL_PUBLIC_ADMIN") == "1" or any(path == p or path.startswith(p + "/") for p in PUBLIC_PATHS):
        return True, 200, ""
    return False, 404, "Only the MCP endpoint (/mcp) is published on this address."


_QT = {"t": 0.0, "url": None}


def public_mcp_url() -> str | None:
    """CL_PUBLIC_URL, else the hostname of a running Cloudflare Quick Tunnel (cached for 30 s)."""
    if os.environ.get("CL_PUBLIC_URL"):
        return os.environ["CL_PUBLIC_URL"].rstrip("/") + "/mcp"
    if time.time() - _QT["t"] < 30:
        return _QT["url"]
    url = None
    try:
        import httpx
        r = httpx.get(os.environ.get("CL_QUICKTUNNEL_METRICS", "http://quicktunnel:2000/quicktunnel"), timeout=1.0)
        host = r.json().get("hostname")
        if host:
            url = f"https://{host}/mcp"
    except Exception:                                      # noqa: BLE001 - no tunnel running
        url = None
    _QT.update(t=time.time(), url=url)
    return url


def base_url(scope) -> str:
    """The address the caller used, e.g. https://x.trycloudflare.com or http://localhost:8080.
    Public names are always https (the tunnel ends TLS); CL_PUBLIC_URL wins for public names when set."""
    host = ""
    for k, v in scope.get("headers", []):
        if k == b"host":
            host = v.decode("latin-1")
            break
    kind = classify(scope)
    if kind == "public":
        if os.environ.get("CL_PUBLIC_URL"):
            return os.environ["CL_PUBLIC_URL"].rstrip("/")
        return f"https://{host}"
    return f"{scope.get('scheme', 'http')}://{host or 'localhost'}"
