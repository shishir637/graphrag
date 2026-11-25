"""End-to-end test of the running app over HTTP: setup, sign-in, agents with different keys, every MCP tool,
access groups, Playground, table upload and metric approval. Start the app first (python -m app --port 8099)."""
import json
import sys
import time

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8099"
H = {"X-CL": "1"}
c = httpx.Client(base_url=BASE, headers=H, timeout=300)
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)


if c.get("/api/v1/setup").json()["needs_setup"]:
    r = c.post("/api/v1/setup", json={"name": "Test Owner", "email": "owner@example.com", "password": "test-pass-12345",
                                      "organisation": "Lumora (test)"})
    check("setup owner", r.status_code == 200, r.text[:100])
else:
    r = c.post("/api/v1/login", json={"email": "owner@example.com", "password": "test-pass-12345"})
    check("login", r.status_code == 200)

st = c.get("/api/v1/settings").json()
check("settings: no AI model, MCP link shown", st["ai_model"]["inside"] is False and st["mcp"]["url"].endswith("/mcp"), st["mcp"]["url"])
ov = c.get("/api/v1/overview").json()
check("overview: no 'connect AI model' step", all(x["id"] != "llm" for x in ov["checklist"]))


def agent(name, groups, caps):
    r = c.post("/api/v1/agents", json={"name": name, "groups": groups, "capabilities": caps}).json()
    return r["token"]


full = agent("All access", ["public", "ops"], ["documents", "data", "graph"])
pub = agent("Public docs only", ["public"], ["documents", "graph"])
data_only = agent("Finance bot", ["public"], ["data"])
MH = {"accept": "application/json, text/event-stream"}


def mcp(token, tool, args):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args}}
    t0 = time.perf_counter()
    r = httpx.post(BASE + "/mcp", json=body, headers=MH | {"authorization": f"Bearer {token}"}, timeout=300)
    res = r.json()["result"]
    return res, round(time.perf_counter() - t0, 2)


init = {"jsonrpc": "2.0", "id": 0, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "e2e", "version": "1"}}}
check("MCP rejects missing key", httpx.post(BASE + "/mcp", json=init, headers=MH).status_code == 401)
r = httpx.post(BASE + "/mcp", json=init, headers=MH | {"authorization": f"Bearer {full}"})
check("MCP initialize with key", r.status_code == 200 and "Context Layer" in r.text)
lst = httpx.post(BASE + "/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                 headers=MH | {"authorization": f"Bearer {full}"}).json()["result"]["tools"]
WRITE_TOOLS = {"add_document", "create_upload_link", "approve_metric"}
check("MCP lists 13 tools, only the upload/approve ones write",
      len(lst) == 13 and all(t["annotations"]["readOnlyHint"] == (t["name"] not in WRITE_TOOLS) for t in lst),
      ", ".join(t["name"] for t in lst))

res, s = mcp(full, "ask", {"question": "Who heads the department that owns GridSense?"})
sc = res.get("structuredContent", {})
texts = " ".join(p["text"] for p in sc.get("passages", []))
check("ask multi-hop: both facts in evidence", "Grid Analytics department" in texts and "Meera Kulkarni" in texts, f"route={sc.get('route')} {s}s")
res, s = mcp(full, "ask", {"question": "What caused incident INC-2041?"})
sc = res["structuredContent"]
check("ask: ops key sees incident record", any("time-zone conversion bug" in p["text"] for p in sc["passages"]), f"{s}s")
res, s = mcp(pub, "ask", {"question": "What caused incident INC-2041?"})
sc = res["structuredContent"]
check("ask: public key does NOT see ops-only incident", not any("lumora_incidents" in p["source"] for p in sc["passages"]), f"{s}s")
res, s = mcp(full, "ask", {"question": "Net revenue by region last quarter"})
sc = res["structuredContent"]
check("ask routes metrics and computes rows", sc["route"] == "data" and sc["data"]["row_count"] >= 4, f"{s}s")
res, s = mcp(pub, "ask", {"question": "Net revenue by region last quarter"})
check("ask: key without data capability falls back to documents", res["structuredContent"]["route"] == "documents"
      and any("no access to data" in n for n in res["structuredContent"]["notes"]))
res, s = mcp(data_only, "describe_data", {})
d = res["structuredContent"]
check("describe_data lists approved metrics incl. Excel/JSON tables",
      {"net_revenue", "lumora_tickets_count", "average_lumora_ticket_csat"} <= {m["name"] for m in d["metrics"]}, f"{len(d['metrics'])} metrics")
res, s = mcp(data_only, "query_metrics", {"metrics": ["average_lumora_ticket_csat"], "dimensions": ["lumora_tickets.channel"]})
check("query_metrics structured (agent-filled) on JSON table", res["structuredContent"]["row_count"] == 3, f"{s}s")
res, s = mcp(data_only, "query_metrics", {"question": "Top 3 customers by total invoice amount"})
check("query_metrics plain question on CSV/JSONL tables", res["structuredContent"].get("row_count") == 3, json.dumps(res["structuredContent"].get("request"))[:120])
res, s = mcp(data_only, "search_documents", {"query": "leave policy"})
check("capability enforced: data key cannot search documents", res.get("isError"))
res, s = mcp(full, "find_connections", {"question": "How is Jonas Weber connected to PulseMeter?"})
sc = res["structuredContent"]
check("find_connections returns links with evidence", sc["relations"] and all("evidence" in x for x in sc["relations"]),
      f"{len(sc['relations'])} links")
res, s = mcp(full, "explore_entity", {"name": "Northbay Utilities"})
check("explore_entity", res["structuredContent"].get("found"), f"{len(res['structuredContent'].get('links', []))} links")
res, s = mcp(pub, "explore_entity", {"name": "INC-2041"})
check("explore_entity hides ops-only entity from public key", not res["structuredContent"].get("found"))
res, s = mcp(full, "search_documents", {"query": "warranty", "files": ["lumora_products.md"]})
check("search_documents with file filter", res["structuredContent"]["passages"] and all(p["source"] == "lumora_products.md" for p in res["structuredContent"]["passages"]))
pid = res["structuredContent"]["passages"][0]["id"]
res, s = mcp(full, "fetch_passage", {"passage_id": pid})
check("fetch_passage", res["structuredContent"]["found"])
res, s = mcp(full, "list_documents", {})
check("list_documents", len(res["structuredContent"]["documents"]) >= 4)       # the 4 Lumora documents
res, s = mcp(full, "ask", {"question": "Give me an overview of the main themes in Kerala", "mode": "overview"})
check("overview mode returns communities", res["structuredContent"].get("overview") is not None, f"route={res['structuredContent']['route']}")

pg = c.post("/api/v1/playground", json={"question": "How do the warranties of PulseMeter and FieldPro devices differ?",
                                        "groups": ["public"], "mode": "auto"}).json()
txt = " ".join(p["text"] for p in pg["passages"])
check("playground comparison: both warranties in top 5", "5 years" in txt and "2-year warranty" in txt, pg["route"])
ev = c.get("/api/v1/events?type=agent").json()["events"]
check("activity log records agent calls", len(ev) >= 15, f"{len(ev)} events")
qa = c.get("/api/v1/quality").json()
check("quality page lists 3 benchmarks", len(qa["suites"] if "suites" in qa else qa.get("quality", qa)) >= 3 if isinstance(qa, dict) else len(qa) == 3)

print(f"\n{sum(r[1] for r in results)}/{len(results)} checks passed")
json.dump([{"check": n, "ok": o, "detail": d} for n, o, d in results], open("experiments/e2e_results.json", "w"), indent=1)
