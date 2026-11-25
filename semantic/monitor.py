"""Nightly check of a workspace's semantic layer (queued by app/scheduler.py, no AI model).

  - Every approved metric is compiled and run on the current data. One that fails (a column was renamed, a
    table is gone) is set to needs_review: agents stop using it, the owner sees why on the Metrics page.
  - A metric this check took out that works again is approved again.
  - Data tables not updated for CL_STALE_DAYS (default 30) are reported as stale.
Every change is logged as an event and kept in the definition's history.
"""
import os
import time
from datetime import datetime, timezone

from .schema import SemanticQuery

CHECK_NOTE = "check failed: "


def _probe(m, model) -> SemanticQuery:
    """The smallest valid request for a metric (a running total needs its time dimension)."""
    dims = []
    if m.type == "cumulative" and m.time_column and "." in m.time_column:
        ent, col = m.time_column.split(".", 1)
        try:
            d = next((d for d in model.entity(ent).dimensions if d.column == col and d.type == "time"), None)
        except KeyError:
            d = None
        if d:
            dims = [f"{ent}.{d.name}__month"]
    return SemanticQuery(metrics=[m.name], dimensions=dims, limit=1)


def run() -> dict:
    from app import core
    from . import repo
    from .compiler import compile_query
    from .db import connect, table_files
    from .yamlio import load_metrics, load_model
    model, metrics = load_model(), load_metrics()
    if not table_files():                     # no data (e.g. a sync failed): never take metrics out for that
        core.log_event("system", "monitor", "semantic.check", "skipped", {"reason": "no data tables"}, ok=False)
        print("  no data tables: check skipped")
        return {"checked": 0, "skipped": "no data tables"}
    con = connect()
    out = {"checked": 0, "failed": [], "restored": [], "stale_tables": []}
    rows = {r["name"]: r for r in repo.rows("metric", ("approved", "needs_review"))}
    for m in metrics.metrics:
        r = rows.get(m.name)
        if r is None or (r["status"] == "needs_review" and not r["note"].startswith(CHECK_NOTE)):
            continue                                      # someone else asked for a review: leave it
        out["checked"] += 1
        try:
            sql = compile_query(_probe(m, model), model, metrics).sql
            con.execute(sql).fetchall()
            if r["status"] == "needs_review":
                repo.set_status("metric", m.name, "approved", "monitor", note="check passed again")
                out["restored"].append(m.name)
        except Exception as e:                            # noqa: BLE001 - recorded on the definition
            msg = str(e).splitlines()[0][:300]
            if r["status"] == "approved":
                repo.set_status("metric", m.name, "needs_review", "monitor", note=CHECK_NOTE + msg)
                out["failed"].append({"metric": m.name, "error": msg})
    days = int(os.environ.get("CL_STALE_DAYS", "30"))
    for name, path in table_files().items():
        age = (time.time() - os.path.getmtime(path)) / 86400
        if age > days:
            out["stale_tables"].append({"table": name, "days": int(age)})
    out["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ok = not out["failed"]
    core.log_event("system", "monitor", "semantic.check", f"{out['checked']} metrics", out, ok=ok)
    print(f"  checked {out['checked']} metrics: {len(out['failed'])} need review, {len(out['restored'])} restored, "
          f"{len(out['stale_tables'])} stale tables")
    return out
