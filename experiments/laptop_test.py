"""Full v0.2 test inside the Docker image on the laptop. Runs against a throw-away app instance (own user
database in CL_DATA_DIR) so the real accounts are untouched; documents, tables and the index are the shared
volumes, so the test data stays available in the real app afterwards.

  1. set up a test owner; graph mode fast
  2. upload the Lumora test files: 4 tables (Excel, JSON, JSON Lines, CSV), 4 documents (ops group for incidents)
  3. prepare documents, prepare data, approve the drafted metrics, build the graph (jobs, timed)
  4. run the end-to-end checks (experiments/e2e_app.py) and the Lumora retrieval benchmark
"""
import json
import subprocess
import sys
import time
from pathlib import Path

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8099"
ROOT = Path(__file__).resolve().parent.parent
TD = ROOT / "test-data" / "lumora"
c = httpx.Client(base_url=BASE, headers={"X-CL": "1"}, timeout=600)


def log(m):
    print(time.strftime("%H:%M:%S"), m, flush=True)


for _ in range(60):
    try:
        if c.get("/api/v1/health").status_code == 200:
            break
    except httpx.HTTPError:
        time.sleep(5)
r = c.post("/api/v1/setup", json={"name": "Test Owner", "email": "owner@example.com", "password": "test-pass-12345",
                                  "organisation": "v0.2 test"})
log(f"setup owner: {r.status_code}")
c.put("/api/v1/settings/graph", json={"mode": "fast"})

files = [("files", (p.name, p.read_bytes())) for p in sorted((TD / "tables").iterdir())]
log(f"upload tables: {c.post('/api/v1/tables', files=files).json()}")
for p in sorted((TD / "documents").iterdir()):
    g = "ops" if "incidents" in p.name else "public"
    r = c.post("/api/v1/documents", files=[("files", (p.name, p.read_bytes()))], data={"groups": g})
    log(f"upload {p.name} ({g}): {r.status_code}")
c.patch("/api/v1/documents/lumora_service_catalog.csv", json={"groups": ["public", "ops"]})


def job(kind):
    t0 = time.perf_counter()
    r = c.post(f"/api/v1/jobs/{kind}")
    if r.status_code != 200:
        log(f"job {kind}: could not start: {r.text[:200]}")
        return False
    jid = r.json()["job"]["id"]
    while True:
        time.sleep(15)
        j = c.get(f"/api/v1/jobs/{jid}").json()["job"]
        if j["status"] not in ("queued", "running"):
            log(f"job {kind}: {j['status']} in {time.perf_counter() - t0:.0f}s; last log: {' | '.join(j['log'][-3:])}"
                + (f" ERROR {j['error']}" if j.get("error") else ""))
            return j["status"] == "done"


job("documents")
job("data")
drafts = [m["name"] for m in c.get("/api/v1/metrics").json()["metrics"] if m["status"] == "draft"]
for name in drafts:
    c.put(f"/api/v1/metrics/{name}/status", json={"status": "approved"})
log(f"approved {len(drafts)} drafted metrics: {drafts}")
job("data")
job("graph")

log("end-to-end checks")
subprocess.run([sys.executable, str(ROOT / "experiments" / "e2e_app.py"), BASE])

log("checks finished; the retrieval benchmark runs after the test server stops (experiments/laptop_bench.py)")
