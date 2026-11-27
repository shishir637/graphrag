"""Entity and relation extraction without a language model (replaces graph/extract_llm.py in the product).

Entities
  fast      spaCy en_core_web_lg named entities (MIT), ~0.03 s per passage on a laptop CPU
  accurate  GLiNER zero-shot NER (Apache-2.0) with the entity types of graph_layer/schema.yaml,
            plus spaCy's spans for recall; ~1.3 s per passage on a laptop CPU
  Types are refined with head-word rules from the schema ("... National Park" -> Park).

Relations (no model has to understand the sentence)
  co-occurrence   two entities named in the same sentence are linked (RELATED_TO), with that sentence as
                  evidence; repeated pairs weigh more. Microsoft's LazyGraphRAG indexes the same way.
  section topic   the entity a section is about (its heading) is linked to the entities named in it
  typed           optional (relations=typed): GLiNER2 relation extraction with the schema's relation types

Output: one JSON line per passage, the same record format the graph builder already reads:
  {"chunk_id", "method", "entities": [{"name", "type", "description"}],
   "relations": [{"source", "target", "type", "description", "strength"}], "seconds"}

  python -m graph.nlp_extract [--mode fast|accurate] [--relations cooc|typed] [--chunks ids.json] [--out f.jsonl]
"""
import argparse
import itertools
import json
import os
import re
import sys
import time
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_FILE = ROOT / "graph_layer" / "schema.yaml"
CHUNKS = ROOT / "outputs" / "2_chunks.json"
SPACY_MODEL = "en_core_web_lg"
GLINER_MODEL = "urchade/gliner_medium-v2.1"          # Apache-2.0
GLINER2_MODEL = "fastino/gliner2-base-v1"            # Apache-2.0
GLINER_THRESHOLD = 0.4
GLINER2_REL_THRESHOLD = 0.3

GENERIC_SCHEMA = {   # used when a deployment has no graph_layer/schema.yaml
    "entity_types": {
        "Person": {"description": "named person", "gliner_label": "person"},
        "Organisation": {"description": "company, agency, team, government body", "gliner_label": "organisation"},
        "Place": {"description": "country, city, region or address", "gliner_label": "location"},
        "Product": {"description": "product, service, system or tool", "gliner_label": "product or system"},
        "Event": {"description": "event, project, incident or meeting", "gliner_label": "event or project"},
        "Document": {"description": "policy, contract, law or standard", "gliner_label": "policy, law or standard"},
    },
    "spacy_label_map": {"PERSON": "Person", "ORG": "Organisation", "GPE": "Place", "LOC": "Place", "FAC": "Place",
                        "PRODUCT": "Product", "EVENT": "Event", "LAW": "Document", "NORP": "Organisation",
                        "WORK_OF_ART": None, "LANGUAGE": None},
    "relation_types": {"RELATED_TO": {"description": "any link"}},
    "head_words": [],
}
SKIP = re.compile(r"^(\W+|\d[\d,.\s%]*|the|a|an|this|that|it|they|he|she|we|i)$", re.I)
GENERIC = re.compile(r"^[a-z][a-z ]*$")         # "outage", "owning team": ordinary words, not a named thing


def named(n: str) -> bool:
    """A name of a thing: has a capital, a digit, '-' or '_' (api-gateway, warehouse_build, INC-2026-031), and
    does not run across two table cells or sentences ("Mobile. ios-app")."""
    return not GENERIC.match(n) and ". " not in n


@lru_cache
def schema() -> dict:
    s = yaml.safe_load(SCHEMA_FILE.read_text(encoding="utf-8")) if SCHEMA_FILE.exists() else {}
    out = dict(GENERIC_SCHEMA)
    out.update({k: v for k, v in (s or {}).items() if v})
    try:                                               # an approved ontology replaces the hand-written types
        from graph.ontology import approved, as_schema
        o = approved()
    except Exception:                                   # noqa: BLE001 - no store: the hand-written list
        o = None
    if o:
        out.update(as_schema(o))
        types = set(out["entity_types"])
        out["spacy_label_map"] = {k: (v if v in types else _closest(v, types)) for k, v in
                                  (out.get("spacy_label_map") or {}).items()}
        out["head_words"] = [r for r in out.get("head_words") or [] if r.get("type") in types]
        out["gazetteer"] = {k: v for k, v in (out.get("gazetteer") or {}).items() if k in types}
        out["verb_rules"] = {k: v for k, v in (out.get("verb_rules") or {}).items()
                             if (v or {}).get("type") in out["relation_types"]}
    return out


def _closest(typ, types: set) -> str | None:
    """spaCy's labels after the ontology changed: keep the type if it is still there, else a type of the same
    family by name (PERSON -> a Person-like type), else none (the name is then skipped by fast mode)."""
    if not typ:
        return None
    fam = {"Person": ("person", "employee", "people", "staff", "member"),
           "Organisation": ("organisation", "organization", "company", "team", "customer", "vendor", "department"),
           "Place": ("place", "location", "city", "region", "country", "office"),
           "GeoFeature": ("place", "location", "region"), "Landmark": ("place", "location", "building", "office"),
           "Product": ("product", "service", "system", "tool", "application"),
           "Event": ("event", "incident", "project", "meeting"), "EthnicGroup": (), "Language": ("language",)}
    for word in fam.get(typ, (typ.lower(),)):
        for t in sorted(types):
            if word in t.lower():
                return t
    return None


def head_word_rules() -> list[tuple[re.Pattern, str]]:
    """schema.yaml head_words: [{pattern: 'national park|sanctuary', type: Park}, ...]; plus built-in defaults
    for common head words, applied only when the type exists in the schema."""
    rules = [(re.compile(r["pattern"], re.I), r["type"]) for r in schema().get("head_words") or []]
    types = set(schema()["entity_types"])
    defaults = [
        (r"\b(sanctuary|national park|tiger reserve|biosphere reserve)\b", "Park"),
        (r"\b(district|division)\b", "District"),
        (r"\b(dynasty|empire|sultanate|kingdom)\b", "Dynasty"),
        (r"\b(university|institute|college|school|academy|laboratory|hospital)\b", "Institution"),
        (r"\b(falls|river|lake|hills?|mountains?|ranges?|valley|plateau|ghats|islands?|sea|ocean|bay|gulf|peak)\b", "GeoFeature"),
        (r"\b(temple|fort|monastery|cathedral|church|mosque|palace|stadium|airport|museum|memorial)\b", "Landmark"),
        (r"\b(festival|mela|utsav)\b", "Festival"),
        (r"\b(inc|ltd|llc|plc|corp|corporation|company|bank|council|department|authority|ministry|party|board|agency)\b", "Organisation"),
    ]
    rules += [(re.compile(p, re.I), t) for p, t in defaults if t in types]
    gaz = schema().get("gazetteer") or {}          # {Type: [names]}: names that always get that type
    for t, names in gaz.items():
        if names:
            rules.insert(0, (re.compile(r"^(" + "|".join(re.escape(n) for n in names) + r")$", re.I), t))
    return rules


def refine(name: str, typ: str, rules) -> str:
    for pat, t in rules:
        if pat.search(name):
            return t
    return typ


def clean(name: str) -> str:
    name = next((x for x in name.strip().splitlines() if x.strip()), "")   # never across lines
    name = re.sub(r"^[\w ]{1,30}:\s+", "", name.strip()) if re.match(r"^[a-z_ ]{1,30}:\s", name.strip()) else name
    name = re.sub(r"^(the|a|an)\s+", "", name.strip(), flags=re.I)
    name = re.sub(r"['’]s$", "", name)
    return re.sub(r"[\s,;:.()\[\]]+$", "", name).strip()


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s.lower())).strip()


@lru_cache
def nlp():
    import spacy
    return spacy.load(SPACY_MODEL, disable=["lemmatizer"])


@lru_cache
def gliner():
    from gliner import GLiNER
    return GLiNER.from_pretrained(GLINER_MODEL)


@lru_cache
def gliner2():
    from gliner2 import GLiNER2
    return GLiNER2.from_pretrained(GLINER2_MODEL)


class Extractor:
    def __init__(self, mode: str = "accurate", relations: str = "cooc"):
        self.mode, self.relations = mode, relations
        s = schema()
        self.labels = {v.get("gliner_label") or k.lower(): k for k, v in s["entity_types"].items()}
        self.spacy_map = s.get("spacy_label_map") or {}
        self.rules = head_word_rules()
        self.rel_types = {k: v.get("description", k) for k, v in (s.get("relation_types") or {}).items() if k != "RELATED_TO"}

    def entities(self, text: str, doc) -> list[dict]:
        ents: dict[str, dict] = {}
        if self.mode == "accurate":
            for e in gliner().predict_entities(text, list(self.labels), threshold=GLINER_THRESHOLD):
                n = clean(e["text"])
                if n and not SKIP.match(n) and named(n):
                    ents.setdefault(norm(n), {"name": n, "type": self.labels[e["label"]], "score": round(e["score"], 3)})
        for e in doc.ents:                       # spaCy spans: all of them in fast mode, the missing ones otherwise
            t = self.spacy_map.get(e.label_)
            n = clean(e.text)
            if not t or not n or SKIP.match(n) or len(n) < 2 or not named(n):
                continue
            k = norm(n)
            if k in ents or any(k in x or x in k for x in ents if len(x) > 3 and len(k) > 3):
                continue
            ents[k] = {"name": n, "type": t, "score": None}
        for e in ents.values():
            e["type"] = refine(e["name"], e["type"], self.rules)
        return list(ents.values())

    def run(self, chunk: dict) -> dict:
        t0 = time.perf_counter()
        text = chunk["text"]
        doc = nlp()(text)
        ents = self.entities(text, doc)
        names = [e["name"] for e in ents]
        pats = {n: re.compile(r"(?<!\w)" + re.escape(n) + r"(?!\w)", re.I) for n in names}
        rels = []
        for sent in doc.sents:
            here = [n for n in names if pats[n].search(sent.text)]
            for a, b in itertools.combinations(dict.fromkeys(here), 2):
                rels.append({"source": a, "target": b, "type": "RELATED_TO", "strength": 5,
                             "description": sent.text.strip()[:300]})
        for t in self.topics(chunk.get("headings") or []):
            if norm(t) not in {norm(n) for n in names}:
                ents.append({"name": t, "type": refine(t, self._topic_type(t, ents), self.rules), "score": None,
                             "topic": True})
            for n in names:
                if norm(n) != norm(t):
                    rels.append({"source": t, "target": n, "type": "MENTIONS", "strength": 2,
                                 "description": f"'{n}' is mentioned in the section about {t}."})
        if self.relations == "typed" and self.rel_types:
            r = gliner2().extract_relations(text, self.rel_types, threshold=GLINER2_REL_THRESHOLD)
            for key, pairs in (r.get("relation_extraction") or {}).items():
                typ = key.split(":")[0].strip()
                for p in pairs:
                    s, o = (p.get("head"), p.get("tail")) if isinstance(p, dict) else p
                    s, o = (s.get("text") if isinstance(s, dict) else s), (o.get("text") if isinstance(o, dict) else o)
                    if s and o and norm(s) != norm(o):
                        s, o = clean(s), clean(o)
                        for n in (s, o):
                            if norm(n) not in {norm(e["name"]) for e in ents}:
                                ents.append({"name": n, "type": refine(n, "Place" if "Place" in self.labels.values() else
                                                                        next(iter(self.labels.values())), self.rules), "score": None})
                        rels.append({"source": s, "target": o, "type": typ, "strength": 8,
                                     "description": f"{s} {typ.lower().replace('_', ' ')} {o}"})
        return {"chunk_id": chunk["chunk_id"], "method": f"nlp-{self.mode}-{self.relations}",
                "entities": [{"name": e["name"], "type": e["type"], "description": ""} for e in ents],
                "relations": rels, "seconds": round(time.perf_counter() - t0, 3)}

    _topic_cache: dict = {}

    def topics(self, headings: list[str]) -> list[str]:
        """Headings that name something (a place, organisation, person, product), found with spaCy NER on the
        heading text: "Kerala" is a topic, "Geography" is not. Cached: headings repeat across passages."""
        out = []
        for h in headings:
            h = clean(re.sub(r"^\d+(\.\d+)*\s*", "", h or ""))
            if not h or len(h) > 80:
                continue
            if h not in self._topic_cache:
                d = nlp()(h)
                ents = [clean(e.text) for e in d.ents if self.spacy_map.get(e.label_)]
                self._topic_cache[h] = ents[0] if len(ents) == 1 and len(ents[0]) >= 0.6 * len(h) else None
            if self._topic_cache[h] and self._topic_cache[h] not in out:
                out.append(self._topic_cache[h])
        return out

    def _topic_type(self, t, ents):
        for e in ents:
            if norm(e["name"]) in norm(t) or norm(t) in norm(e["name"]):
                return e["type"]
        return "Place" if "Place" in self.labels.values() else next(iter(self.labels.values()))


def ontology_fp() -> str:
    """Fingerprint of the types in use: results found with other types are redone after the ontology changes."""
    import hashlib
    s = schema()
    return hashlib.sha1(json.dumps([s["entity_types"], s.get("relation_types")], sort_keys=True,
                                   default=str).encode("utf-8")).hexdigest()[:10]


def _th(chunk: dict) -> str:
    import hashlib
    return hashlib.sha1(chunk["text"].encode("utf-8")).hexdigest()[:12]


def extract_all(out: Path, mode: str = "accurate", relations: str = "cooc", ids: list[str] | None = None,
                resume: bool = True, compact: bool | None = None) -> Path:
    """ids: only (re)extract these passages. compact: drop results for passages that were deleted or changed
    (default: when all passages are processed; uploads pass ids AND compact=True)."""
    all_chunks = json.loads(CHUNKS.read_text(encoding="utf-8"))
    chunks = all_chunks
    if ids:
        keep = set(ids)
        chunks = [c for c in all_chunks if c["chunk_id"] in keep]
    if compact is None:
        compact = not ids
    done = set()
    if resume and out.exists():
        # keep only results for passages that still exist with the same text: a deleted or edited document
        # must not leave its old entities in the graph (records from before 0.3 have no hash: kept)
        want = {c["chunk_id"]: _th(c) for c in all_chunks}
        fp = ontology_fp()
        kept, dropped, retyped = {}, 0, 0
        for line in out.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if compact and (r["chunk_id"] not in want or r.get("th", want[r["chunk_id"]]) != want[r["chunk_id"]]):
                dropped += 1
                continue
            if not ids and r.get("ov") != fp and mode != "ai":   # full build after an ontology change: find again
                retyped += 1
                continue
            kept[r["chunk_id"]] = line
        if retyped:
            print(f"  the ontology changed: {retyped} passages will be read again with the new types", flush=True)
        if dropped or retyped:
            tmp = out.with_name(out.name + ".tmp")
            tmp.write_text("".join(x + "\n" for x in kept.values()), encoding="utf-8")
            os.replace(tmp, out)
            print(f"  removed {dropped} passages that were deleted or changed", flush=True)
        done = set(kept)
    else:
        out.write_text("", encoding="utf-8")
    todo = [c for c in chunks if c["chunk_id"] not in done]
    t0 = time.perf_counter()
    if mode == "ai":
        return _extract_ai(out, todo, t0)
    ex = Extractor(mode, relations)
    fp_now = ontology_fp()
    with out.open("a", encoding="utf-8") as f:
        for i, c in enumerate(todo, 1):
            f.write(json.dumps(ex.run(c) | {"th": _th(c), "ov": fp_now}, ensure_ascii=False) + "\n")
            if i % 50 == 0 or i == len(todo):
                f.flush()
                print(f"  {i}/{len(todo)} passages ({time.perf_counter() - t0:.0f}s)", flush=True)
    return out


def _extract_ai(out: Path, todo: list[dict], t0: float) -> Path:
    """Graph mode "ai": one call per passage to the provider chosen in Settings, in parallel."""
    from concurrent.futures import ThreadPoolExecutor
    from graph.extract_ai import AIExtractor, destination, is_local
    ex = AIExtractor()
    where = destination(ex.cfg)
    print(f"  AI extraction with {ex.cfg['model']} at {where}"
          f" ({'on your network' if is_local(ex.cfg) else 'passages are sent to this service'}),"
          f" {ex.cfg['workers']} at a time", flush=True)
    with out.open("a", encoding="utf-8") as f, ThreadPoolExecutor(ex.cfg["workers"]) as pool:
        for i, (c, rec) in enumerate(zip(todo, pool.map(ex.run, todo)), 1):
            f.write(json.dumps(rec | {"th": _th(c)}, ensure_ascii=False) + "\n")
            if i % 25 == 0 or i == len(todo):
                f.flush()
                print(f"  {i}/{len(todo)} passages ({time.perf_counter() - t0:.0f}s)", flush=True)
    u = ex.usage
    summary = {"model": ex.method, "destination": where, "passages": len(todo), "calls": u["calls"],
               "from_cache": u["cached"], "fell_back_to_spacy": u["fallbacks"], "input_tokens": u["input_tokens"],
               "output_tokens": u["output_tokens"], "seconds": round(time.perf_counter() - t0, 1),
               "errors": ex.errors[:5]}
    (out.parent / "gr_ai_usage.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(f"  AI calls: {u['calls']} new, {u['cached']} from cache, {u['input_tokens']} input / "
          f"{u['output_tokens']} output tokens", flush=True)
    if u["fallbacks"]:
        print(f"  {u['fallbacks']} passages fell back to spaCy (first error: {ex.errors[0] if ex.errors else '?'})",
              flush=True)
        if todo and u["fallbacks"] == len(todo) - u["cached"] and u["fallbacks"] >= 3:
            raise RuntimeError("Every AI call failed: " + (ex.errors[0] if ex.errors else "unknown error")
                               + ". Check Settings → AI extraction (Test connection).")
    return out


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["fast", "accurate", "ai"], default="accurate")
    ap.add_argument("--relations", choices=["cooc", "typed"], default="cooc")
    ap.add_argument("--chunks")
    ap.add_argument("--out")
    ap.add_argument("--fresh", action="store_true")
    a = ap.parse_args()
    ids = json.loads(Path(a.chunks).read_text()) if a.chunks else None
    out = Path(a.out or ROOT / "outputs" / f"gr_extract_{a.mode}.jsonl")
    extract_all(out, a.mode, a.relations, ids, resume=not a.fresh)
