"""The knowledge graph's ontology (entity and relation types), proposed by a free local AI model and approved by
an admin, instead of a hand-written list.

  where it lives   semantic_layer/ontology.yaml = definitions of kind entity_type / relation_type in the
                   definitions store (Postgres): every change recorded with who and when, undoable on the Layers
                   page. Until one is approved, graph_layer/schema.yaml (the hand-written list) is used.
  propose          sample passages spread over all documents -> batches of 6 -> the AI model configured under
                   Settings -> AI extraction (default suggestion: a local model through Ollama, e.g. qwen3.5:4b,
                   Apache-2.0: free, data stays on the machine) proposes types with a definition, a short label
                   for GLiNER and examples copied from the passages -> merged across batches -> a type is kept
                   only when its examples really occur in the passages (no invented types) and it was proposed
                   at least twice or has two real examples -> one more call merges near-duplicate types.
                   Compared with the current ontology: new / already there / not seen in these documents.
  approve          the admin keeps the types they want (Knowledge graph page); the ontology is replaced
                   (recorded in the history) and the next graph build uses it: GLiNER looks for exactly these
                   types, and AI extraction is told them.
"""
import json
import random
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROPOSALS = ROOT / "outputs" / "ontology_proposals.json"
SAMPLE = 30
BATCH = 6
MAX_TYPES = 18
NAME = re.compile(r"^[A-Z][A-Za-z0-9]{1,39}$")
REL = re.compile(r"^[A-Z][A-Z0-9_]{1,39}$")

PROMPT = """You design the ontology (the kinds of things and links) for a company's knowledge graph.
Below are passages from the company's own documents. List the KINDS of named things these passages are about,
and the kinds of links between them, so that a search engine can find every named thing of each kind.

Rules:
- Entity types are kinds of NAMED things (a person, a team, a software service, a customer company, a policy,
  an incident, a product plan...). Not dates, amounts, metrics or generic nouns.
- Names in PascalCase, singular: Person, Team, Service, Customer, Policy, Incident.
- Prefer a few general types over many narrow ones; never two types for the same kind of thing.
- label: 2-5 lowercase words a named-entity recogniser can look for ("software service or system").
- examples: up to 4 names copied EXACTLY as written in the passages. No example, no type.
- Relation types in UPPER_SNAKE_CASE (OWNS, LEADS, MEMBER_OF, CAUSED_BY), each with the source and target
  entity type and up to 2 examples "source -> target" copied from the passages.

Reply with JSON only:
{"entity_types": [{"name": "", "description": "", "label": "", "examples": [""]}],
 "relation_types": [{"name": "", "source": "", "target": "", "description": "", "examples": [""]}]}"""

MERGE_PROMPT = """These entity types were proposed separately for one company's knowledge graph. Some mean the
same kind of thing (e.g. Employee and Person, Service and SoftwareService). Group the names that mean the same
kind of thing and choose the best name of each group. Keep every type that is different.
Reply with JSON only: {"groups": [{"keep": "Person", "same_as": ["Employee"]}]}"""


# ---------------------------------------------------------------- the ontology in use

def _store_file() -> Path:
    from semantic import config as SC
    return SC.LAYER_DIR / "ontology.yaml"


def approved() -> dict | None:
    """The approved ontology (company layer), or None when there is none yet."""
    from semantic import defstore
    from semantic.yamlio import read
    f = _store_file()
    try:
        if defstore.enabled():
            with defstore.acting("system", "graph", stack=(defstore.COMPANY,), write_layer=defstore.COMPANY):
                data = read(f) or {}
        elif f.exists():
            data = read(f) or {}
        else:
            return None
    except Exception:                                   # noqa: BLE001 - no store yet
        return None
    ents = [e for e in data.get("entity_types") or [] if isinstance(e, dict) and e.get("name")]
    if not ents:
        return None
    return {"entity_types": ents, "relation_types": [r for r in data.get("relation_types") or [] if isinstance(r, dict)
                                                     and r.get("name")]}


def as_schema(o: dict) -> dict:
    """The approved ontology in schema.yaml's shape (what the extractors read)."""
    ents = {e["name"]: {"description": e.get("description") or e["name"],
                        "gliner_label": (e.get("label") or e.get("description") or e["name"]).lower()[:60]}
            for e in o["entity_types"]}
    rels = {r["name"]: {"description": r.get("description") or r["name"],
                        **({"source": r["source"]} if r.get("source") else {}),
                        **({"target": r["target"]} if r.get("target") else {})}
            for r in o.get("relation_types") or []}
    rels.setdefault("RELATED_TO", {"description": "any other meaningful link; the description says what it is"})
    return {"entity_types": ents, "relation_types": rels}


def current() -> dict:
    """What the graph uses now: the approved ontology, else the hand-written schema.yaml."""
    from graph.nlp_extract import schema
    s = schema()
    o = approved()
    return {"source": "approved" if o else "hand-written (graph_layer/schema.yaml)",
            "entity_types": [{"name": k, "description": v.get("description", ""), "label": v.get("gliner_label", "")}
                             for k, v in s["entity_types"].items()],
            "relation_types": [{"name": k, "description": v.get("description", "")}
                               for k, v in (s.get("relation_types") or {}).items()]}


# ---------------------------------------------------------------- proposing

def _sample(n: int = SAMPLE, seed: int = 7) -> list[dict]:
    """Passages spread over the documents: round-robin across files, longest first within a file."""
    from rag.utils import read_json
    try:
        chunks = read_json("2_chunks.json") or []
    except SystemExit:
        chunks = []
    try:                                               # company documents only: a person's own files stay theirs
        from app import engine
        amap = engine.access_map()
    except Exception:                                   # noqa: BLE001
        amap = {}
    personal = {f for f, g in amap.items() if g and all(str(x).startswith("layer:") for x in g)}
    by_doc: dict[str, list] = {}
    for c in chunks:
        if len(c.get("text", "")) >= 120 and c["source"] not in personal:
            by_doc.setdefault(c["source"], []).append(c)
    rnd = random.Random(seed)
    for v in by_doc.values():
        rnd.shuffle(v)
        v.sort(key=lambda c: -len(c["text"]))
    out, docs = [], sorted(by_doc)
    while len(out) < n and any(by_doc.values()):
        for d in docs:
            if by_doc[d] and len(out) < n:
                out.append(by_doc[d].pop(0))
    return out


def _canon(name: str) -> str:
    """'product plan', 'Product_Plan', 'ProductPlans' -> 'ProductPlan'."""
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", str(name or "")) if p]
    if not parts:
        return ""
    n = "".join(p[0].upper() + p[1:] for p in parts)
    if n.endswith("ies") and len(n) > 4:
        n = n[:-3] + "y"
    elif n.endswith("s") and not n.endswith("ss") and len(n) > 3:
        n = n[:-1]
    return n


def _found(example: str, text: str) -> bool:
    e = " ".join(str(example).split()).lower()
    return len(e) >= 2 and e in text


def _ask(cfg: dict, system: str, user: str) -> dict:
    import os
    from graph.extract_ai import _call, parse_reply
    text, _ = _call(cfg, system, user, timeout=int(os.environ.get("CL_ONTOLOGY_TIMEOUT", "900")))
    return parse_reply(text)


def propose(cfg: dict | None = None, ask=None, sample: list[dict] | None = None, log=print) -> dict:
    """Ask the configured AI model for an ontology; returns and saves the proposal (nothing is changed yet).
    ask(cfg, system, user) -> dict can be replaced in tests."""
    from graph.extract_ai import config, destination, is_local, problems
    cfg = cfg or config()
    bad = problems(cfg)
    if bad:
        raise RuntimeError(" ".join(bad))
    ask = ask or _ask
    sample = sample if sample is not None else _sample()
    if not sample:
        raise RuntimeError("No passages yet: add documents and build first.")
    corpus = " ".join(" ".join(c["text"].split()) for c in sample).lower()
    log(f"[1/3] Asking {cfg['model']} at {destination(cfg)} ({'on this machine or network' if is_local(cfg) else 'outside'})"
        f" about {len(sample)} passages from {len({c['source'] for c in sample})} documents")
    ents: dict[str, dict] = {}
    rels: dict[str, dict] = {}
    t0 = time.perf_counter()
    size = BATCH if not is_local(cfg) else 4            # smaller requests for a model on a laptop CPU
    batches = [sample[i:i + size] for i in range(0, len(sample), size)]
    failed = 0
    for bi, batch in enumerate(batches, 1):
        user = "\n\n".join(f"<passage file=\"{c['source']}\" section=\"{' > '.join(c.get('headings') or [])}\">\n"
                           f"{c['text'][:1500]}\n</passage>" for c in batch)
        try:
            raw = ask(cfg, PROMPT, user)
        except Exception as e:                          # noqa: BLE001 - one bad batch does not stop the proposal
            failed += 1
            log(f"  batch {bi}/{len(batches)} failed: {str(e)[:200]}")
            continue
        for e in raw.get("entity_types") or []:
            if not isinstance(e, dict):
                continue
            name = _canon(e.get("name"))
            if not NAME.match(name or "-"):
                continue
            ex = [str(x).strip() for x in (e.get("examples") or []) if str(x).strip()][:6]
            d = ents.setdefault(name, {"name": name, "description": "", "label": "", "examples": [], "votes": 0})
            d["votes"] += 1
            d["description"] = d["description"] or str(e.get("description") or "")[:200]
            d["label"] = d["label"] or " ".join(str(e.get("label") or "").lower().split())[:60]
            for x in ex:
                if x not in d["examples"] and _found(x, corpus):
                    d["examples"].append(x)
        for r in raw.get("relation_types") or []:
            if not isinstance(r, dict):
                continue
            name = re.sub(r"[^A-Z0-9_]", "_", str(r.get("name") or "").upper()).strip("_")
            if not REL.match(name or "-"):
                continue
            d = rels.setdefault(name, {"name": name, "source": _canon(r.get("source")), "target": _canon(r.get("target")),
                                       "description": str(r.get("description") or "")[:200], "examples": [], "votes": 0})
            d["votes"] += 1
            for x in (r.get("examples") or [])[:3]:
                parts = [p.strip() for p in re.split(r"->|→", str(x)) if p.strip()]
                if len(parts) == 2 and all(_found(p, corpus) for p in parts) and x not in d["examples"]:
                    d["examples"].append(str(x).strip())
        log(f"  batch {bi}/{len(batches)}: {len(ents)} entity types, {len(rels)} link types so far "
            f"({time.perf_counter() - t0:.0f}s)")
    if failed == len(batches):
        raise RuntimeError("The AI model did not answer. Check Settings -> AI extraction (Test connection).")

    log("[2/3] Keeping only types with real examples from the documents")
    keep = {k: v for k, v in ents.items() if v["examples"]}
    if len(keep) > 1:
        try:
            listing = "\n".join(f"- {k}: {v['description']} (e.g. {', '.join(v['examples'][:3])})" for k, v in keep.items())
            groups = ask(cfg, MERGE_PROMPT, listing).get("groups") or []
            for g in groups:
                main = _canon(g.get("keep"))
                if main not in keep:
                    continue
                for other in g.get("same_as") or []:
                    o = _canon(other)
                    if o in keep and o != main:
                        k = keep.pop(o)
                        keep[main]["votes"] += k["votes"]
                        keep[main]["examples"] = list(dict.fromkeys(keep[main]["examples"] + k["examples"]))[:6]
                        keep[main].setdefault("merged", []).append(o)
        except Exception as e:                          # noqa: BLE001 - merging is a nicety
            log(f"  (merging near-duplicates skipped: {str(e)[:120]})")
    merged_away = {o for v in keep.values() for o in v.get("merged", [])}
    keep = {k: v for k, v in keep.items() if v["votes"] >= 2 or len(v["examples"]) >= 2}
    dropped = sorted(set(ents) - set(keep) - merged_away)
    ranked = sorted(keep.values(), key=lambda v: (-v["votes"], -len(v["examples"]), v["name"]))[:MAX_TYPES]
    names = {v["name"] for v in ranked}
    rel_keep = [r for r in sorted(rels.values(), key=lambda v: (-v["votes"], v["name"]))
                if r["examples"] and r["source"] in names | {""} and r["target"] in names | {""}][:20]

    log("[3/3] Comparing with the current ontology")
    cur = current()
    cur_names = {_canon(e["name"]) for e in cur["entity_types"]}
    for v in ranked:
        v["status"] = "already there" if v["name"] in cur_names else "new"
    unseen = [e["name"] for e in cur["entity_types"] if _canon(e["name"]) not in names]
    out = {"created": time.strftime("%Y-%m-%dT%H:%M:%S"), "model": cfg["model"], "provider": cfg["provider"],
           "where": destination(cfg), "passages": len(sample), "documents": len({c["source"] for c in sample}),
           "seconds": round(time.perf_counter() - t0, 1), "batches": len(batches), "failed_batches": failed,
           "entity_types": ranked, "relation_types": rel_keep,
           "dropped_without_examples": dropped, "current_not_seen": unseen, "current_source": cur["source"]}
    PROPOSALS.parent.mkdir(parents=True, exist_ok=True)
    PROPOSALS.write_text(json.dumps(out, indent=1), encoding="utf-8")
    log(f"Proposed {len(ranked)} entity types and {len(rel_keep)} link types. Review them on the Knowledge graph page.")
    return out


def proposal() -> dict | None:
    try:
        return json.loads(PROPOSALS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- approving

def apply(entity_types: list[dict], relation_types: list[dict], by: str) -> dict:
    """Make these the ontology (replacing the current one, recorded in the history). Each type: name,
    description, label (GLiNER), examples."""
    from graph.nlp_extract import schema
    from semantic import defstore
    from semantic.yamlio import write
    ents, seen = [], set()
    for e in entity_types:
        name = _canon(e.get("name"))
        if not NAME.match(name or "-") or name in seen:
            raise ValueError(f"'{e.get('name')}' is not a usable type name (PascalCase letters and digits).")
        seen.add(name)
        ents.append({"name": name, "description": str(e.get("description") or name)[:200],
                     "label": " ".join(str(e.get("label") or e.get("description") or name).lower().split())[:60],
                     "examples": [str(x) for x in (e.get("examples") or [])][:6]})
    if not ents:
        raise ValueError("Keep at least one entity type.")
    rels = []
    for r in relation_types:
        name = re.sub(r"[^A-Z0-9_]", "_", str(r.get("name") or "").upper()).strip("_")
        if not REL.match(name or "-"):
            continue
        rels.append({"name": name, "source": _canon(r.get("source")) if r.get("source") else "",
                     "target": _canon(r.get("target")) if r.get("target") else "",
                     "description": str(r.get("description") or name)[:200],
                     "examples": [str(x) for x in (r.get("examples") or [])][:3]})
    data = {"version": 1, "entity_types": ents, "relation_types": rels}
    note = f"ontology approved: {len(ents)} entity types, {len(rels)} link types"
    if defstore.enabled():
        with defstore.acting(by, "admin console", stack=(defstore.COMPANY,), write_layer=defstore.COMPANY):
            defstore.save_file("ontology.yaml", data, defstore.COMPANY, note)
    else:
        write(_store_file(), data, "Knowledge-graph ontology (approved types).")
    schema.cache_clear()
    return {"entity_types": len(ents), "relation_types": len(rels),
            "next": "Rebuild the knowledge graph so it uses the new types (Knowledge graph page, Build the graph)."}
