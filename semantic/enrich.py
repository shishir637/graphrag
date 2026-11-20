"""Phase 4 - Enrichment: make the model findable from everyday words.

  - value lists : DuckDB pulls the distinct values of each category column (for checking filter values)
  - synonyms    : reviewed everyday words -> metric / dimension / value (semantic_layer/enrichment.yaml)
  - verified    : reviewed question -> request pairs; each one is compiled, parsed by SQLGlot and
                  EXPLAINed by DuckDB before it is stored
  - instructions: semantic_layer/instructions.md (fiscal year, default filters, wording)
Stored in sl_store/enrichment.duckdb. Metrics, dimensions, synonyms and verified questions are also
embedded with BGE-M3 into a Qdrant collection, so a new question finds its closest matches
(the RAG retrieval, reused).

Output: outputs/sl_4_enrichment.json
"""
import json

import duckdb

from rag.utils import write_json

from .compiler import compile_query
from .config import INSTRUCTIONS_FILE, SEMANTIC_COLLECTION, STORE_DB, STORE_DIR
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

    STORE_DIR.mkdir(exist_ok=True)
    STORE_DB.unlink(missing_ok=True)
    store = duckdb.connect(str(STORE_DB))
    store.execute("CREATE TABLE synonyms (term VARCHAR, target VARCHAR)")
    store.executemany("INSERT INTO synonyms VALUES (?, ?)", [(s.term, s.target) for s in enrichment.synonyms])
    store.execute("CREATE TABLE value_lists (dimension VARCHAR, value VARCHAR)")
    store.executemany("INSERT INTO value_lists VALUES (?, ?)", [(d, v) for d, vs in values.items() for v in vs])
    store.execute("CREATE TABLE verified_queries (question VARCHAR, query JSON, sql VARCHAR, verified_by VARCHAR)")
    store.executemany("INSERT INTO verified_queries VALUES (?, ?, ?, ?)", [
        (v.question, json.dumps(v.query.model_dump(mode="json", exclude_defaults=True)), sql, v.verified_by)
        for v, sql in compiled])
    store.execute("CREATE TABLE instructions (text VARCHAR)")
    store.execute("INSERT INTO instructions VALUES (?)", [INSTRUCTIONS_FILE.read_text(encoding="utf-8")])
    store.close()

    items = catalog_items(model, metrics, enrichment, verified)
    n_points = _index_in_qdrant(items) if embed else 0
    result = {
        "store": str(STORE_DB), "synonyms": len(enrichment.synonyms),
        "value_lists": {d: len(v) for d, v in values.items()},
        "verified_queries": len(compiled), "qdrant_collection": SEMANTIC_COLLECTION, "qdrant_points": n_points,
        "items_by_kind": {k: sum(i["kind"] == k for i in items) for k in ("metric", "dimension", "verified")},
    }
    write_json("sl_4_enrichment.json", result)
    print(f"  {result['synonyms']} synonyms, {sum(result['value_lists'].values())} values in "
          f"{len(values)} lists, {len(compiled)} verified queries, {n_points} items embedded in Qdrant")
    return result


# --- read side, used when answering ---
def load_store() -> dict:
    if not STORE_DB.exists():
        raise SystemExit("semantic layer store not built - run `python rag.py sl-enrich` first")
    s = duckdb.connect(str(STORE_DB), read_only=True)
    values: dict[str, list[str]] = {}
    for d, v in s.execute("SELECT dimension, value FROM value_lists").fetchall():
        values.setdefault(d, []).append(v)
    out = {"synonyms": s.execute("SELECT term, target FROM synonyms").fetchall(), "values": values,
           "instructions": s.execute("SELECT text FROM instructions").fetchone()[0]}
    s.close()
    return out
