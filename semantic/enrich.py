"""Phase 4 - Enrichment: make the model findable from everyday words.

  - value lists : DuckDB pulls the distinct values of each category column (for checking filter values)
  - synonyms    : reviewed everyday words -> metric / dimension / value (semantic_layer/enrichment.yaml)
  - verified    : reviewed question -> request pairs; each one is compiled, parsed by SQLGlot and
                  EXPLAINed by DuckDB before it is stored
  - instructions: semantic_layer/instructions.md (fiscal year, default filters, wording)
The metric catalog (synonyms, known values, instructions) is an in-memory structure built from the semantic
layer: inside a workspace from Postgres (sl_definitions, sl_value_lists), cached by the layer's state version
(sl_state, which every definition or value-list change moves); on the command line from the YAML files and the
tables, cached by their modification times. Nothing is written to disk (0.7: the DuckDB store file is gone).
Metrics, dimensions, synonyms and verified questions can also be embedded with BGE-M3 into a Qdrant collection,
so a new question finds its closest matches (the RAG retrieval, reused).

Output: outputs/sl_4_enrichment.json
"""
import threading

from rag.utils import write_json

from .compiler import compile_query
from .config import INSTRUCTIONS_FILE, LAYER_DIR, SEMANTIC_COLLECTION
from .db import connect
from .yamlio import load_enrichment, load_metrics, load_model, load_verified


def _value_lists(model, enrichment, con) -> dict[str, list[str]]:
    out = {}
    for ref in enrichment.value_list_dimensions:
        ent, dim = ref.split(".")
        d = model.entity(ent).dimension(dim)
        if not d:
            raise ValueError(f"value_list_dimensions: unknown dimension '{ref}'")
        table = model.entity(ent).table
        out[ref] = [r[0] for r in con.execute(
            f'SELECT DISTINCT "{d.column}"::VARCHAR FROM "{table}" WHERE "{d.column}" IS NOT NULL ORDER BY 1').fetchall()]
    return out


def _check_synonyms(model, metrics, enrichment, values) -> list[str]:
    problems, metric_names = [], {m.name for m in metrics.metrics}
    dims = {f"{e.name}.{d.name}" for e in model.entities for d in e.dimensions}
    for s in enrichment.synonyms:
        target, _, value = s.target.partition("=")
        if "." not in target:
            if target not in metric_names:
                problems.append(f"synonym '{s.term}': unknown metric '{target}'")
        elif target not in dims:
            problems.append(f"synonym '{s.term}': unknown dimension '{target}'")
        elif value and value not in values.get(target, []):
            problems.append(f"synonym '{s.term}': '{value}' is not a value of {target}")
    return problems


def catalog_items(model, metrics, enrichment, verified) -> list[dict]:
    """Everything a question might need to find, as short texts for embedding."""
    syn: dict[str, list[str]] = {}
    for s in enrichment.synonyms:
        syn.setdefault(s.target.split("=")[0], []).append(s.term)
    items = []
    for m in metrics.metrics:
        if m.status != "approved":
            continue
        also = f" Also called: {', '.join(syn[m.name])}." if m.name in syn else ""
        items.append({"kind": "metric", "ref": m.name,
                      "text": f"Metric {m.label} ({m.name}): {m.description}{also}"})
    for e in model.entities:
        for d in e.dimensions:
            if d.type == "key":
                continue
            ref = f"{e.name}.{d.name}"
            also = f" Also called: {', '.join(syn[ref])}." if ref in syn else ""
            grains = " Time dimension: add __day, __week, __month, __quarter, __year or __fiscal_year." \
                if d.type == "time" else ""
            items.append({"kind": "dimension", "ref": ref,
                          "text": f"Dimension {d.label or d.name} ({ref}) of {e.label or e.name}: {d.description}{also}{grains}"})
    for v in verified.queries:
        items.append({"kind": "verified", "ref": v.question, "text": v.question,
                      "query": v.query.model_dump(mode="json", exclude_defaults=True)})
    return items


def _index_in_qdrant(items: list[dict]) -> int:
    from qdrant_client import models

    from rag.models import embed_dense
    from rag.store import client
    from rag.config import EMBED_DIM

    qc = client()
    if qc.collection_exists(SEMANTIC_COLLECTION):
        qc.delete_collection(SEMANTIC_COLLECTION)
    qc.create_collection(SEMANTIC_COLLECTION, vectors_config={
        "dense": models.VectorParams(size=EMBED_DIM, distance=models.Distance.COSINE)})
    qc.create_payload_index(SEMANTIC_COLLECTION, "kind", models.PayloadSchemaType.KEYWORD)
    vecs = embed_dense([i["text"] for i in items])
    qc.upsert(SEMANTIC_COLLECTION, wait=True, points=[
        models.PointStruct(id=n, vector={"dense": v}, payload=i) for n, (i, v) in enumerate(zip(items, vecs))])
    return qc.count(SEMANTIC_COLLECTION).count


def build_enrichment(embed: bool = True) -> dict:
    model, metrics, enrichment, verified = load_model(), load_metrics(), load_enrichment(), load_verified()
    con = connect()
    values = _value_lists(model, enrichment, con)
    problems = _check_synonyms(model, metrics, enrichment, values)

    compiled = []
    for v in verified.queries:
        try:
            sql = compile_query(v.query, model, metrics).sql
            con.execute("EXPLAIN " + sql)
            compiled.append((v, sql))
        except Exception as e:
            problems.append(f"verified query {v.question!r}: {str(e).splitlines()[0]}")
    if problems:
        for p in problems:
            print("   -", p)
        raise SystemExit(f"{len(problems)} enrichment problems - nothing was stored")

    from .repo import in_workspace
    if in_workspace():                       # the app: known values go to Postgres with the definitions
        from . import repo
        repo.save_value_lists(values)
        result = {"store": "postgres", "synonyms": len(enrichment.synonyms),
                  "value_lists": {d: len(v) for d, v in values.items()}, "verified_queries": len(compiled)}
        print(f"  {result['synonyms']} synonyms, {sum(result['value_lists'].values())} values in "
              f"{len(values)} lists, {len(compiled)} verified queries checked")
        return result

    _CATALOG.clear()                          # the command line: the catalog is rebuilt in memory on next use
    items = catalog_items(model, metrics, enrichment, verified)
    n_points = _index_in_qdrant(items) if embed else 0
    result = {
        "store": "memory", "synonyms": len(enrichment.synonyms),
        "value_lists": {d: len(v) for d, v in values.items()},
        "verified_queries": len(compiled), "qdrant_collection": SEMANTIC_COLLECTION, "qdrant_points": n_points,
        "items_by_kind": {k: sum(i["kind"] == k for i in items) for k in ("metric", "dimension", "verified")},
    }
    write_json("sl_4_enrichment.json", result)
    print(f"  {result['synonyms']} synonyms, {sum(result['value_lists'].values())} values in "
          f"{len(values)} lists, {len(compiled)} verified queries, {n_points} items embedded in Qdrant")
    return result


# --- read side, used when answering: the metric catalog in memory ---
_CATALOG: dict = {}                          # workspace (or "" on the command line) -> (stamp, catalog)
_LOCK = threading.Lock()
MAX_CATALOGS = 256


def _stamp():
    """What the catalog depends on: the layer's state version in a workspace; files on the command line."""
    from .repo import in_workspace
    if in_workspace():
        from . import repo
        return ("v", repo.layer_version())
    from .db import table_files
    import os
    files = sorted(LAYER_DIR.glob("*.yaml")) + ([INSTRUCTIONS_FILE] if INSTRUCTIONS_FILE.exists() else [])
    return (tuple((str(p), os.path.getmtime(p)) for p in files),
            tuple((n, os.path.getmtime(p)) for n, p in table_files().items()))


def _build_catalog() -> dict:
    from .repo import in_workspace
    from .yamlio import load_enrichment
    if in_workspace():
        from . import repo
        return {"synonyms": [(x.term, x.target) for x in load_enrichment().synonyms], "values": repo.value_lists(),
                "instructions": repo.load_instructions()}
    enrichment = load_enrichment()
    values = _value_lists(load_model(), enrichment, connect())
    return {"synonyms": [(s.term, s.target) for s in enrichment.synonyms], "values": values,
            "instructions": INSTRUCTIONS_FILE.read_text(encoding="utf-8") if INSTRUCTIONS_FILE.exists() else ""}


def catalog() -> dict:
    """{"synonyms": [(term, target)], "values": {dimension: [values]}, "instructions": str}, cached per workspace
    and rebuilt when the semantic layer changes."""
    from rag.wspath import current_workspace
    key, stamp = current_workspace() or "", _stamp()
    with _LOCK:
        hit = _CATALOG.get(key)
        if hit and hit[0] == stamp:
            return hit[1]
    c = _build_catalog()
    with _LOCK:
        _CATALOG.pop(key, None)
        _CATALOG[key] = (stamp, c)
        while len(_CATALOG) > MAX_CATALOGS:
            _CATALOG.pop(next(iter(_CATALOG)))
    return c


def load_store() -> dict:
    """The metric catalog (kept under its old name for callers)."""
    return catalog()
