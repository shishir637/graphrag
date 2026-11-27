"""Nightly check of the company layer's metrics (no AI model; queued by app/scheduler.py once a day per workspace).

  - Every approved metric is compiled and run on the current data. One that fails (a column was renamed or dropped,
    a table is gone) is set to needs_review: agents stop using it (not planned, not listed, refused by name) and
    company admins see it with the reason on the Approvals tab.
  - A metric this check took out that works again is approved again on the next run.
  - Data tables not updated for CL_STALE_DAYS (default 30) are reported as stale.
The results are kept in the workspace setting metric_check and every run is an event (Activity tab).
"""
import os
import time
from datetime import datetime, timezone

from .schema import SemanticQuery

SETTING = "metric_check"


def _probe(m, model) -> SemanticQuery:
    """The smallest valid request for a metric (a running total needs its time dimension)."""
    dims = []
    if m.type == "cumulative" and m.time_column and "." in m.time_column:
        ent, col = m.time_column.split(".", 1)
        e = next((x for x in model.entities if x.name == ent), None)
        d = next((d for d in (e.dimensions if e else []) if d.column == col and d.type == "time"), None)
        if d:
            dims = [f"{ent}.{d.name}__month"]
    return SemanticQuery(metrics=[m.name], dimensions=dims, limit=1)


def _missing_columns(error: str) -> str:
    """A short reason in plain words for the most common failure."""
    e = error.lower()
    if ("not found" in e and ("column" in e or "referenced" in e)) or "does not have a column" in e:
        return "a column it uses was renamed or removed"
    if "table" in e and ("does not exist" in e or "not found" in e or "catalog" in e):
        return "a table it uses is gone"
    return "it no longer runs on the current data"


def state() -> dict:
    from app import core
    return core.get_setting(SETTING, {}) or {}


def run(by: str = "nightly check") -> dict:
    from app import core
    from .auto_model import set_status
    from .compiler import compile_query
    from .db import connect, table_files
    from .yamlio import load_metrics, load_model
    model, metrics = load_model(), load_metrics()
    prev = state()
    taken_out = dict(prev.get("taken_out") or {})
    if not table_files():                     # no data (e.g. a sync failed): never take metrics out for that
        core.log_event("system", "monitor", "semantic.check", "skipped", {"reason": "no data tables"}, ok=False)
        return {"checked": 0, "skipped": "no data tables"}
    con = connect()
    out = {"checked": 0, "failed": [], "restored": [], "stale_tables": []}
    for m in metrics.metrics:
        if m.status == "needs_review" and m.name not in taken_out:
            continue                                      # someone else asked for a review: leave it
        if m.status not in ("approved", "needs_review"):
            continue
        out["checked"] += 1
        try:
            sql = compile_query(_probe(m, model), model, metrics).sql
            con.execute(sql).fetchall()
            if m.status == "needs_review":
                set_status(m.name, "approved", by)
                taken_out.pop(m.name, None)
                out["restored"].append(m.name)
        except Exception as e:                            # noqa: BLE001 - recorded with the metric
            msg = str(e).splitlines()[0][:300]
            if m.status == "approved":
                set_status(m.name, "needs_review", by)
                out["failed"].append({"metric": m.name, "error": msg})
            taken_out[m.name] = {"error": msg, "why": _missing_columns(msg), "label": m.label or m.name,
                                 "since": (taken_out.get(m.name) or {}).get("since") or core.now()}
    days = int(os.environ.get("CL_STALE_DAYS", "30"))
    for name, path in table_files().items():
        age = (time.time() - os.path.getmtime(path)) / 86400
        if age > days:
            out["stale_tables"].append({"table": name, "days": int(age)})
    out["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    core.set_setting(SETTING, {"last": out["at"], "taken_out": taken_out, "stale_tables": out["stale_tables"]})
    core.log_event("system", "monitor", "semantic.check", f"{out['checked']} metrics", out, ok=not out["failed"])
    print(f"  checked {out['checked']} metrics: {len(out['failed'])} need review, {len(out['restored'])} restored, "
          f"{len(out['stale_tables'])} stale tables")
    return out


def taken_out() -> list[dict]:
    """Metrics this check took out that still need review (for the Approvals tab)."""
    from .yamlio import load_metrics
    try:
        now_review = {m.name for m in load_metrics().metrics if m.status == "needs_review"}
    except Exception:                                      # noqa: BLE001 - no layer
        return []
    return [{"metric": k} | v for k, v in (state().get("taken_out") or {}).items() if k in now_review]


def due() -> int:
    """For the scheduler: queue the check in every workspace whose last run is older than CL_MONITOR_HOURS (24)."""
    from app import core, jobs
    from app import db as D
    hours = float(os.environ.get("CL_MONITOR_HOURS", "24"))
    tenants = [t["id"] for t in core.list_tenants()] if D.postgres() else [None]
    n = 0
    for t in tenants:
        with core.tenant_scope(t):
            last = state().get("last")
            queued = core.get_setting("metric_check_queued")
            mark = last or queued
            if mark:
                try:
                    if (datetime.now(timezone.utc) - datetime.fromisoformat(mark)).total_seconds() < hours * 3600:
                        continue
                except ValueError:
                    pass
            core.set_setting("metric_check_queued", core.now())
            jobs.enqueue("monitor", "Checking metric definitions", lambda info: run(), "nightly check", {})
            n += 1
    return n
