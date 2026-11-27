"""End to end, the way Claude or ChatGPT does it: the official MCP client library connects with only the URL,
gets a 401, discovers the sign-in server, registers, opens the sign-in page (a scripted browser signs in and
clicks Allow), exchanges the code, then adds a document from the "chat", waits for the real rebuild and
answers from it.

Run against a running server:  python experiments/oauth_e2e.py http://127.0.0.1:8099 owner@example.com 'password'
"""
import asyncio
import json
import re
import sys
import time
from urllib.parse import parse_qs, urlparse

import httpx
import httpx2
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

BASE, EMAIL, PASSWORD = sys.argv[1].rstrip("/"), sys.argv[2], sys.argv[3]
FACT = ("# Lumora discount approvals (2026)\n\n## Approval limits\n\nDiscounts above 35% on any Lumora contract "
        "need written approval from Meera Iyer, Chief Revenue Officer. Discounts between 20% and 35% need the "
        "regional sales director.\n")


class Memory(TokenStorage):
    def __init__(self):
        self.tokens, self.client = None, None

    async def get_tokens(self) -> OAuthToken | None:
        return self.tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.tokens = tokens

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self.client

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self.client = client_info


result: dict = {}
steps: list[str] = []


async def browser(url: str):
    """What the person does in the pop-up: sign in, then Allow."""
    steps.append(f"sign-in page opened: {urlparse(url).path}")
    async with httpx.AsyncClient(base_url=BASE, follow_redirects=False) as b:
        q = parse_qs(urlparse(url).query)
        params = {k: v[0] for k, v in q.items()}
        r = await b.get(url)
        assert "Sign in" in r.text, r.text[:300]
        r = await b.post("/oauth/login", data=params | {"email": EMAIL, "password": PASSWORD})
        assert r.status_code == 303, r.text[:300]
        r = await b.get(r.headers["location"])
        assert "Allow" in r.text
        steps.append("signed in; consent page: " + re.search(r"<h1>(.*?)</h1>", r.text).group(1))
        csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', r.text).group(1)
        r = await b.post("/oauth/authorize", data=params | {"csrf": csrf, "decision": "allow"})
        back = parse_qs(urlparse(r.headers["location"]).query)
        result.update(code=back["code"][0], state=back.get("state", [None])[0], iss=back.get("iss", [None])[0])
        steps.append("clicked Allow; redirected back with a code")


async def callback() -> AuthorizationCodeResult:
    return AuthorizationCodeResult(**result)


def sc(r):
    if r.is_error:
        raise RuntimeError(r.content[0].text)
    return r.structured_content


async def main():
    auth = OAuthClientProvider(
        server_url=f"{BASE}/mcp",
        client_metadata=OAuthClientMetadata(client_name="E2E test (like Claude)", redirect_uris=["http://localhost:3030/callback"],
                                            grant_types=["authorization_code", "refresh_token"],
                                            response_types=["code"], token_endpoint_auth_method="none"),
        storage=Memory(), redirect_handler=browser, callback_handler=callback)
    t0 = time.time()
    async with httpx2.AsyncClient(auth=auth, timeout=120) as http, \
            streamable_http_client(f"{BASE}/mcp", http_client=http) as (read, write, *_), \
            ClientSession(read, write) as s:
        await s.initialize()
        steps.append(f"connected over MCP in {time.time() - t0:.1f}s")
        tools = [t.name for t in (await s.list_tools()).tools]
        steps.append(f"{len(tools)} tools: {', '.join(tools)}")
        before = sc(await s.call_tool("search_documents", {"query": "Who approves discounts above 35%?", "top_k": 3}))
        steps.append("before upload, top passage: " + before["passages"][0]["source"] if before["passages"] else "no passages")
        up = sc(await s.call_tool("add_document", {"filename": "lumora_discount_policy.pdf", "text": FACT}))
        steps.append(f"add_document: saved {up['saved']['file']} ({up['saved']['kind']}), job {up['job_id']} {up['status']}")
        t1 = time.time()
        while True:
            st = sc(await s.call_tool("build_status", {"job_id": up["job_id"]}))
            if st["status"] in ("done", "failed"):
                break
            await asyncio.sleep(5)
        steps.append(f"build_status: {st['status']} after {time.time() - t1:.0f}s; files {st['files']}; graph {st.get('graph')}")
        if st["status"] != "done":
            raise RuntimeError(st)
        ans = sc(await s.call_tool("ask", {"question": "Who approves discounts above 35% on Lumora contracts?"}))
        top = ans["passages"][0] if ans.get("passages") else {}
        steps.append(f"ask: route {ans['route']}; top passage {top.get('source')}: {top.get('text', '')[:120]!r}")
        assert top.get("source") == "lumora_discount_policy.md" and "Meera Iyer" in top.get("text", "")
        link = sc(await s.call_tool("create_upload_link", {}))
        steps.append(f"create_upload_link: {link['upload_url'][:len(BASE) + 13]}... valid {link['expires_in_minutes']} min")
    print(json.dumps(steps, indent=1))
    print("E2E OK")


asyncio.run(main())
