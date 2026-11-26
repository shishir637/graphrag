"""The knowledge graph's ontology per workspace (Postgres table onto_definitions, migrations/006_ontology.sql).

load() returns the same dict as graph_layer/schema.yaml (what nlp_extract.schema() always returned):
  entity_types {Type: {description, gliner_label, parent_type}}, relation_types {TYPE: {description, domain_type,
  range_type}}, spacy_label_map {LABEL: Type|None}, verb_rules {lemma: {type, preps, passive, reverse}},
  copula_rules {noun: TYPE}, head_words [{pattern, type}], gazetteer {Type: [names]}
built from APPROVED rows only. Outside a workspace (command line) the YAML file is used as before.

Where definitions come from (same statuses and approval policy as the semantic layer):
  import / template   graph_layer/schema.yaml for the first workspace, the GitHub template on connecting GitHub
  semantic            every semantic entity is an entity type and every join a relation type (source=semantic,
                      approved), so the graph and the data share one vocabulary
  documents           frequent names that only fit a generic type, grouped by head word ("... Policy"), become
                      draft types with examples; an owner approves them
Rows are named entity_type:<Type>, relation_type:<TYPE>, rule:<kind>:<key>.
"""
import hashlib
import json
import re
from collections import Counter, defaultdict

RULE_KINDS = ("spacy_label_map", "verb", "copula", "head_word", "gazetteer")
GENERIC_TYPES = {"Person", "Organisation", "Place", "Product", "Event", "Document"}
_CACHE: dict = {}


def _ws() -> str | None:
    from rag.wspath import current_workspace
    return current_workspace()


def _db():
    from app.db import db
    return db()


# ------------------------------------------------------------------ read

def version() -> int:
    r = _db().execute("SELECT version FROM onto_state WHERE tenant_id = ?", (_ws(),)).fetchone()
    return r[0] if r else 0


def rows(kind: str | None = None, statuses=("draft", "approved", "needs_review")) -> list[dict]:
    q, args = "SELECT * FROM onto_definitions WHERE tenant_id = ? AND status = ANY(?)", [_ws(), list(statuses)]
    if kind:
        q += " AND kind = ?"
        args.append(kind)
    out = []
    for r in _db().execute(q + " ORDER BY kind, position, name", args):
        x = {k: r[k] for k in r.keys()}
        if isinstance(x["definition"], str):
            x["definition"] = json.loads(x["definition"])
        out.append(x)
    return out


def get(kind: str, name: str) -> dict | None:
    r = [x for x in rows(kind, ("draft", "approved", "needs_review", "retired")) if x["name"] == name]
    return r[0] if r else None


def to_schema(items: list[dict]) -> dict:
    """Approved rows -> the schema dict the extractors use."""
    out = {"entity_types": {}, "relation_types": {}, "spacy_label_map": {}, "verb_rules": {}, "copula_rules": {},
           "head_words": [], "gazetteer": {}, "constraints": {}}
    for x in items:
        d = x["definition"]
        if x["kind"] == "entity_type":
            out["entity_types"][x["name"]] = {k: v for k, v in d.items() if v not in (None, "", [])}
        elif x["kind"] == "relation_type":
            out["relation_types"][x["name"]] = {k: v for k, v in d.items() if v not in (None, "", [])}
        else:
            k, key, val = d["kind"], d["key"], d.get("value")
            if k == "spacy_label_map":
                out["spacy_label_map"][key] = val
            elif k == "verb":
                out["verb_rules"][key] = val
            elif k == "copula":
                out["copula_rules"][key] = val
            elif k == "head_word":
                out["head_words"].append({"pattern": key, "type": val})
            elif k == "gazetteer":
                out["gazetteer"][key] = val
            elif k == "constraint":                     # checks on facts read from documents (facts/validate.py)
                out["constraints"][key] = val
    return out


def load() -> dict:
    """The workspace's approved ontology (cached per workspace and version). An empty ontology falls back to the
    generic types, so extraction always has something to work with."""
    from .nlp_extract import GENERIC_SCHEMA
    t, v = _ws(), version()
    hit = _CACHE.get(t)
    if hit and hit[0] == v:
        return hit[1]
    s = to_schema(rows(statuses=("approved",)))
    if not s["entity_types"]:
        s = {**GENERIC_SCHEMA, **{k: val for k, val in s.items() if val}}
    s.setdefault("relation_types", {}).setdefault("RELATED_TO", {"description": "any other meaningful link"})
    _CACHE[t] = (v, s)
    while len(_CACHE) > 128:
        _CACHE.pop(next(iter(_CACHE)))
    return s


def fingerprint(s: dict | None = None) -> str:
    """Changes when the approved ontology changes: stored on extraction records so a change re-extracts."""
    s = s if s is not None else load()
    keep = {k: s.get(k) for k in ("entity_types", "relation_types", "spacy_label_map", "verb_rules", "copula_rules",
                                  "head_words", "gazetteer")}
    return hashlib.sha1(json.dumps(keep, sort_keys=True, default=str).encode()).hexdigest()[:12]


def history(kind: str, name: str) -> list[dict]:
    return [{k: r[k] for k in r.keys() if k != "tenant_id"} for r in _db().execute(
        "SELECT * FROM onto_history WHERE tenant_id = ? AND kind = ? AND name = ? ORDER BY id", (_ws(), kind, name))]


# ------------------------------------------------------------------ write

def put(kind: str, name: str, definition: dict, *, status: str, source: str, by: str = "", note: str = "") -> dict:
    from app.core import now
    from app.db import tx
    t = _ws()
    if not t:
        raise RuntimeError("No workspace in scope for the ontology.")
    with tx() as c:
        old = c.execute("SELECT 1 FROM onto_definitions WHERE tenant_id = ? AND kind = ? AND name = ?",
                        (t, kind, name)).fetchone()
        args = (json.dumps(definition, ensure_ascii=False), status, source, note[:500], by, now())
        if old:
            c.execute("UPDATE onto_definitions SET definition = ?::jsonb, status = ?, source = ?, note = ?, updated_by = ?, "
                      "updated_at = ? WHERE tenant_id = ? AND kind = ? AND name = ?", (*args, t, kind, name))
        else:
            c.execute("INSERT INTO onto_definitions (definition, status, source, note, updated_by, updated_at, tenant_id, "
                      "kind, name) VALUES (?::jsonb, ?, ?, ?, ?, ?, ?, ?, ?)", (*args, t, kind, name))
    return get(kind, name)


def set_status(kind: str, name: str, status: str, by: str) -> dict:
    """Approving (or retiring) a proposed type does the same to the head-word rules proposed with it."""
    r = get(kind, name)
    if r is None:
        raise KeyError(name)
    out = put(kind, name, r["definition"], status=status, source=r["source"], by=by, note=r["note"])
    if kind == "entity_type":
        for x in rows("rule", ("draft", "approved", "needs_review", "retired")):
            if x["definition"].get("value") == name and x["source"] == r["source"] and x["status"] != status:
                put("rule", x["name"], x["definition"], status=status, source=x["source"], by=by, note=x["note"])
    return out


def _store(kind, name, definition, *, source, by, certified=False, note="") -> bool:
    from semantic.policy import status_for
    old = get(kind, name)
    policy_kind = "metric" if source in ("documents", "learned") else "entity"      # proposals need an owner
    st = status_for(policy_kind, source=source, certified=certified, old=old, new=definition)
    if old and old["definition"] == definition and old["status"] == st:
        return False
    put(kind, name, definition, status=st, source=source, by=by, note=note)
    return True


def import_schema(s: dict, *, source: str = "import", by: str = "", certified: bool = True) -> dict:
    """Store a schema dict (the graph_layer/schema.yaml format)."""
    n = Counter()
    for name, d in (s.get("entity_types") or {}).items():
        n["entity_type"] += _store("entity_type", name, dict(d), source=source, by=by, certified=certified)
    for name, d in (s.get("relation_types") or {}).items():
        n["relation_type"] += _store("relation_type", name, dict(d or {}), source=source, by=by, certified=certified)
    rules = [("spacy_label_map", k, v) for k, v in (s.get("spacy_label_map") or {}).items()]
    rules += [("verb", k, v) for k, v in (s.get("verb_rules") or {}).items()]
    rules += [("copula", k, v) for k, v in (s.get("copula_rules") or {}).items()]
    rules += [("head_word", r["pattern"], r["type"]) for r in s.get("head_words") or []]
    rules += [("gazetteer", k, v) for k, v in (s.get("gazetteer") or {}).items()]
    rules += [("constraint", k, v) for k, v in (s.get("constraints") or {}).items()]
    for k, key, val in rules:
        n["rule"] += _store("rule", f"{k}:{key}", {"kind": k, "key": key, "value": val}, source=source, by=by,
                            certified=certified)
    return dict(n)


def export_schema(include_drafts: bool = False) -> dict:
    sts = ("draft", "approved", "needs_review") if include_drafts else ("approved",)
    return {k: v for k, v in to_schema(rows(statuses=sts)).items() if v}


def seed_generic(by: str = "system"):
    from .nlp_extract import GENERIC_SCHEMA
    import_schema(GENERIC_SCHEMA, source="template", by=by)


def seed_file(by: str = "system") -> dict:
    import yaml
    from .config import SCHEMA_FILE
    return import_schema(yaml.safe_load(SCHEMA_FILE.read_text(encoding="utf-8")), source="import", by=by)


# ------------------------------------------------------------------ from the semantic model

def type_name(entity: str) -> str:
    """customers -> Customer, github_pull_requests -> GithubPullRequest."""
    parts = [p for p in entity.split("_") if p]
    if parts:
        w = parts[-1]
        parts[-1] = w[:-3] + "y" if w.endswith("ies") else w[:-1] if w.endswith("s") and not w.endswith("ss") else w
    return "".join(p[:1].upper() + p[1:] for p in parts) or "Thing"


def join_relation(from_entity: str, to_entity: str) -> str:
    return "BELONGS_TO_" + re.sub(r"(?<!^)(?=[A-Z])", "_", type_name(to_entity)).upper()


def _taken(kind: str, name: str) -> bool:
    old = get(kind, name)
    return bool(old) and old["source"] != "semantic"


def sync_from_semantic(model=None, by: str = "semantic") -> dict:
    """Every semantic entity becomes an entity type and every join a relation type (approved: they mirror reviewed
    definitions). Runs after the semantic layer changes."""
    if model is None:
        from semantic.yamlio import load_model
        model = load_model()
    n = Counter()
    for e in model.entities:
        d = {"description": e.description or e.label or e.name, "gliner_label": (e.label or e.name).lower(),
             "semantic_entity": e.name}
        if _taken("entity_type", type_name(e.name)):
            continue                                    # a reviewed type of that name exists: it stays as it is
        n["entity_type"] += _store("entity_type", type_name(e.name), d, source="semantic", by=by)
    for j in model.joins:
        d = {"description": f"{j.from_entity}.{j.from_column} -> {j.to_entity}.{j.to_column}",
             "domain_type": type_name(j.from_entity), "range_type": type_name(j.to_entity),
             "semantic_join": f"{j.from_entity}.{j.from_column}->{j.to_entity}.{j.to_column}"}
        if _taken("relation_type", join_relation(j.from_entity, j.to_entity)):
            continue
        n["relation_type"] += _store("relation_type", join_relation(j.from_entity, j.to_entity), d, source="semantic", by=by)
    return dict(n)


# ------------------------------------------------------------------ from documents

def propose_from_extractions(path, min_names: int = 5, by: str = "documents") -> list[dict]:
    """Names typed only with a generic type that share a head word ("Leave Policy", "Travel Policy"...) become a
    draft entity type with a head-word rule and examples, for an owner to approve."""
    if not path or not __import__("os").path.exists(path):
        return []
    s = load()
    known = {t.lower() for t in s["entity_types"]}
    covered = {r["pattern"].lower() for r in s.get("head_words") or []}
    heads: dict[str, set] = defaultdict(set)
    for line in open(path, encoding="utf-8"):
        if not line.strip():
            continue
        for e in json.loads(line).get("entities") or []:
            name, typ = (e.get("name") or "").strip(), e.get("type")
            words = name.split()
            if typ not in GENERIC_TYPES or len(words) < 2:
                continue
            head = re.sub(r"[^A-Za-z]", "", words[-1])
            if len(head) >= 4 and head[:1].isupper() and head.lower() not in known:
                heads[head].add(name)
    out = []
    for head, names in sorted(heads.items(), key=lambda x: -len(x[1])):
        if len(names) < min_names or head.lower() in covered:
            continue
        t = head[:1].upper() + head[1:].lower()
        if get("entity_type", t):
            continue
        ex = sorted(names)[:8]
        put("entity_type", t, {"description": f"{t} (found in the documents)", "gliner_label": t.lower()},
            status="draft", source="documents", by=by, note=f"{len(names)} names such as {', '.join(ex[:4])}")
        put("rule", f"head_word:\\b{head.lower()}\\b", {"kind": "head_word", "key": rf"\b{head.lower()}\b", "value": t},
            status="draft", source="documents", by=by, note=f"names ending in '{head}' get type {t}")
        out.append({"type": t, "names": len(names), "examples": ex})
    return out
