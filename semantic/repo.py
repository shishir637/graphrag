"""The semantic layer of the current workspace, stored in Postgres (table sl_definitions, see
migrations/002_semantic.sql). Returns the same Pydantic objects as the YAML loaders (semantic/schema.py), so the
planner and compiler do not care where definitions live.

Statuses: draft (not used by agents), approved, needs_review (failed a check; hidden from agents), retired.
Every change is versioned and kept in sl_history (database trigger). YAML is only for import and export.
"""
import json
from pathlib import Path

from .schema import Enrichment, Join, Metric, MetricSet, SemanticModel, Synonym, VerifiedQuery, VerifiedSet

KINDS = ("entity", "join", "metric", "synonym", "value_list", "verified", "instructions")
STATUSES = ("draft", "approved", "needs_review", "retired")
LIVE = ("draft", "approved", "needs_review")          # everything except retired
METRIC_META = ("status", "owner")                       # kept in columns, not in the definition


def _db():
    from app.db import current_tenant, db
    t = current_tenant()
    if not t:
        raise RuntimeError("No workspace in scope for the semantic layer.")
    return db(), t


def in_workspace() -> bool:
    from app.db import current_tenant
    return bool(current_tenant())


def _now() -> str:
    from app.core import now
    return now()


# ------------------------------------------------------------------ names

def join_name(j: Join | dict) -> str:
    j = j if isinstance(j, dict) else j.model_dump()
    return f"{j['from_entity']}.{j['from_column']}->{j['to_entity']}.{j['to_column']}"


def synonym_name(s: Synonym | dict) -> str:
    s = s if isinstance(s, dict) else s.model_dump()
    return f"{s['term']}=>{s['target']}"


# ------------------------------------------------------------------ read

def layer_version() -> int:
    d, t = _db()
    r = d.execute("SELECT version FROM sl_state WHERE tenant_id = ?", (t,)).fetchone()
    return r[0] if r else 0


def get_state() -> dict:
    d, t = _db()
    r = d.execute("SELECT version, updated_at FROM sl_state WHERE tenant_id = ?", (t,)).fetchone()
    return {"version": r[0], "updated_at": r[1]} if r else {"version": 0, "updated_at": None}


def rows(kind: str, statuses=LIVE) -> list[dict]:
    d, t = _db()
    out = d.execute("SELECT * FROM sl_definitions WHERE tenant_id = ? AND kind = ? AND status = ANY(?) "
                    "ORDER BY position, name", (t, kind, list(statuses))).fetchall()
    return [_row(r) for r in out]


def _row(r) -> dict:
    x = {k: r[k] for k in r.keys()}
    if isinstance(x["definition"], str):
        x["definition"] = json.loads(x["definition"])
    return x


def get(kind: str, name: str) -> dict | None:
    d, t = _db()
    r = d.execute("SELECT * FROM sl_definitions WHERE tenant_id = ? AND kind = ? AND name = ?", (t, kind, name)).fetchone()
    return _row(r) if r else None


def is_empty() -> bool:
    d, t = _db()
    return d.execute("SELECT count(*) FROM sl_definitions WHERE tenant_id = ?", (t,)).fetchone()[0] == 0


def load_model() -> SemanticModel:
    ents = [r["definition"] for r in rows("entity", ("approved",))]
    names = {e["name"] for e in ents}
    joins = [r["definition"] for r in rows("join", ("approved",))
             if r["definition"]["from_entity"] in names and r["definition"]["to_entity"] in names]
    return SemanticModel(version=layer_version(), entities=ents, joins=joins)


def load_metrics() -> MetricSet:
    ms = []
    for r in rows("metric"):
        st = "approved" if r["status"] == "approved" else "draft" if r["status"] == "draft" else "needs_review"
        ms.append(Metric.model_validate(r["definition"] | {"status": st, "owner": r["owner"]}))
    return MetricSet(version=layer_version(), metrics=ms)


def load_enrichment() -> Enrichment:
    return Enrichment(version=layer_version(),
                      synonyms=[r["definition"] for r in rows("synonym", ("approved",))],
                      value_list_dimensions=[r["name"] for r in rows("value_list", ("approved",))])


def load_verified() -> VerifiedSet:
    return VerifiedSet(version=layer_version(), queries=[r["definition"] for r in rows("verified", ("approved",))])


def load_instructions() -> str:
    r = get("instructions", "main")
    return r["definition"].get("text", "") if r and r["status"] == "approved" else ""


def value_lists() -> dict[str, list[str]]:
    d, t = _db()
    out: dict[str, list[str]] = {}
    for dim, v in d.execute("SELECT dimension, value FROM sl_value_lists WHERE tenant_id = ? ORDER BY dimension, value", (t,)):
        out.setdefault(dim, []).append(v)
    return out


def history(kind: str, name: str) -> list[dict]:
    d, t = _db()
    return [{k: r[k] for k in r.keys() if k != "tenant_id"} for r in d.execute(
        "SELECT * FROM sl_history WHERE tenant_id = ? AND kind = ? AND name = ? ORDER BY id", (t, kind, name))]


# ------------------------------------------------------------------ write

def put(kind: str, name: str, definition: dict, *, status: str, source: str, by: str = "", owner: str | None = None,
        domain: str | None = None, position: int | None = None, note: str = "") -> dict:
    """Insert or update one definition (history and version are kept by the database)."""
    if kind not in KINDS or status not in STATUSES:
        raise ValueError(f"bad kind/status: {kind}/{status}")
    from app.db import tx
    _, t = _db()
    with tx() as c:
        old = c.execute("SELECT owner, domain, position FROM sl_definitions WHERE tenant_id = ? AND kind = ? AND name = ?",
                        (t, kind, name)).fetchone()
        vals = dict(domain=domain or (old["domain"] if old else "general"),
                    definition=json.dumps(definition, ensure_ascii=False), status=status,
                    owner=owner if owner is not None else (old["owner"] if old else ""), source=source, note=note[:500],
                    position=position if position is not None else (old["position"] if old else _next_position(c, t, kind)),
                    updated_by=by, updated_at=_now())
        if old:
            c.execute("UPDATE sl_definitions SET domain = ?, definition = ?::jsonb, status = ?, owner = ?, source = ?, "
                      "note = ?, position = ?, updated_by = ?, updated_at = ? WHERE tenant_id = ? AND kind = ? AND name = ?",
                      (*vals.values(), t, kind, name))
        else:
            c.execute("INSERT INTO sl_definitions (domain, definition, status, owner, source, note, position, updated_by, "
                      "updated_at, tenant_id, kind, name) VALUES (?, ?::jsonb, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (*vals.values(), t, kind, name))
    return get(kind, name)


def _next_position(c, t, kind) -> int:
    return c.execute("SELECT COALESCE(max(position), -1) + 1 FROM sl_definitions WHERE tenant_id = ? AND kind = ?",
                     (t, kind)).fetchone()[0]


def set_status(kind: str, name: str, status: str, by: str, note: str = "") -> dict:
    if status not in STATUSES:
        raise ValueError(f"Status must be one of {', '.join(STATUSES)}.")
    r = get(kind, name)
    if r is None:
        raise KeyError(name)
    owner = by if status == "approved" and not r["owner"] else r["owner"]
    return put(kind, name, r["definition"], status=status, source=r["source"], by=by, owner=owner,
               note=note or r["note"])


def delete(kind: str, name: str, by: str = "") -> bool:
    from app.db import tx
    _, t = _db()
    with tx() as c:
        c.execute("SELECT set_config('app.actor', ?, true)", (by,))
        return c.execute("DELETE FROM sl_definitions WHERE tenant_id = ? AND kind = ? AND name = ?",
                         (t, kind, name)).rowcount > 0


def save_value_lists(values: dict[str, list[str]]):
    from app.db import tx
    _, t = _db()
    with tx() as c:
        c.execute("DELETE FROM sl_value_lists WHERE tenant_id = ?", (t,))
        for dim, vs in values.items():
            for v in dict.fromkeys(str(x) for x in vs):
                c.execute("INSERT INTO sl_value_lists (tenant_id, dimension, value) VALUES (?, ?, ?)", (t, dim, v))


# ------------------------------------------------------------------ whole sets (drafting, import, export)

def import_layer(model: SemanticModel | None = None, metrics: MetricSet | None = None,
                 enrichment: Enrichment | None = None, verified: VerifiedSet | None = None,
                 instructions: str | None = None, *, source: str, by: str = "", certified: bool = False,
                 domain: str | None = None) -> dict:
    """Store definitions, each with the status the approval policy gives it (semantic/policy.py).
    Never downgrades an approved definition that did not change."""
    from .policy import status_for
    n = {k: 0 for k in KINDS}

    def store(kind, name, definition, declared=None, owner=None):
        old = get(kind, name)
        st = status_for(kind, source=source, certified=certified, declared=declared, old=old, new=definition)
        if old and old["definition"] == definition and old["status"] == st:
            return
        put(kind, name, definition, status=st, source=source, by=by, owner=owner, domain=domain)
        n[kind] += 1

    if model is not None:
        for e in model.entities:
            store("entity", e.name, e.model_dump(mode="json"))
        for j in model.joins:
            store("join", join_name(j), j.model_dump(mode="json"))
    if metrics is not None:
        for m in metrics.metrics:
            d = m.model_dump(mode="json", exclude=set(METRIC_META))
            store("metric", m.name, d, declared=m.status, owner=m.owner)
    if enrichment is not None:
        for s in enrichment.synonyms:
            store("synonym", synonym_name(s), s.model_dump(mode="json"))
        for dim in enrichment.value_list_dimensions:
            store("value_list", dim, {"dimension": dim})
    if verified is not None:
        for v in verified.queries:
            store("verified", v.question, v.model_dump(mode="json", exclude_defaults=False))
    if instructions is not None and instructions.strip():
        store("instructions", "main", {"text": instructions})
    if n["entity"] or n["join"]:                  # the graph's ontology follows the data's vocabulary
        try:
            from graph.ontology import sync_from_semantic
            sync_from_semantic(by=by or source)
        except Exception as e:                    # noqa: BLE001 - the semantic layer must not fail on this
            print(f"  ontology not updated from the semantic model: {e}")
    return {k: v for k, v in n.items() if v}


def import_dir(path: Path, *, source: str = "import", by: str = "", certified: bool = True) -> dict:
    """Import a folder of layer YAML files (model.yaml, metrics.yaml, enrichment.yaml, verified_queries.yaml,
    instructions.md); missing files are skipped."""
    from . import yamlio
    f = lambda name: (path / name) if (path / name).exists() else None      # noqa: E731
    return import_layer(
        model=yamlio.load_model(f("model.yaml")) if f("model.yaml") else None,
        metrics=yamlio.load_metrics(f("metrics.yaml")) if f("metrics.yaml") else None,
        enrichment=yamlio.load_enrichment(f("enrichment.yaml")) if f("enrichment.yaml") else None,
        verified=yamlio.load_verified(f("verified_queries.yaml")) if f("verified_queries.yaml") else None,
        instructions=f("instructions.md").read_text(encoding="utf-8") if f("instructions.md") else None,
        source=source, by=by, certified=certified)


def import_texts(files: dict[str, str], *, source: str = "import", by: str = "", certified: bool = True) -> dict:
    """Import YAML given as text, keyed by file name (from an upload or the API)."""
    import io
    from ruamel.yaml import YAML
    from . import yamlio
    from .schema import Enrichment as E, MetricSet as M, SemanticModel as S, VerifiedSet as V

    def parse(name, cls):
        if name not in files:
            return None
        data = yamlio._plain(YAML(typ="safe").load(io.StringIO(files[name])))
        return cls.model_validate(data)
    return import_layer(model=parse("model.yaml", S), metrics=parse("metrics.yaml", M),
                        enrichment=parse("enrichment.yaml", E), verified=parse("verified_queries.yaml", V),
                        instructions=files.get("instructions.md"), source=source, by=by, certified=certified)


def export_texts() -> dict[str, str]:
    """The workspace's layer as YAML files (drafts included, with their status; retired left out)."""
    import io
    from ruamel.yaml import YAML
    y = YAML()
    y.indent(mapping=2, sequence=4, offset=2)

    def dump(data) -> str:
        buf = io.StringIO()
        y.dump(data, buf)
        return buf.getvalue()
    out = {
        "model.yaml": dump({"version": 1, "entities": [r["definition"] for r in rows("entity")],
                            "joins": [r["definition"] for r in rows("join")]}),
        "metrics.yaml": dump({"version": 1, "metrics": [
            r["definition"] | {"owner": r["owner"], "status": "approved" if r["status"] == "approved" else "draft"}
            for r in rows("metric")]}),
        "enrichment.yaml": dump({"version": 1, "synonyms": [r["definition"] for r in rows("synonym")],
                                 "value_list_dimensions": [r["name"] for r in rows("value_list")]}),
        "verified_queries.yaml": dump({"version": 1, "queries": [r["definition"] for r in rows("verified")]}),
    }
    instr = load_instructions()
    if instr:
        out["instructions.md"] = instr
    return out
