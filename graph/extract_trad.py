"""Traditional (non-LLM) entity and relation extraction.

Two extractors, both writing the same record format so they can be scored side by side
(and later against Claude):

  spacy  : spaCy's pretrained statistical NER (OntoNotes labels, mapped to our types)
           + rule-based relations over the dependency parse (graph_layer/schema.yaml: verb_rules,
             copula_rules) + a "list" rule for "<Name> (<Place>)" / "<Name>, <Place>" lines.
  gliner : GLiNER zero-shot NER with OUR entity types as labels (no training data needed)
           + the same spaCy dependency rules for relations.

Both also apply a simple "heading context" rule: phrases like "the state" / "the city" resolve to
the region in the chunk's heading path - a cheap stand-in for coreference resolution.

Runs in the separate NLP environment (spaCy/GLiNER pin library versions that would break the
RAG environment), so this module imports nothing from rag/.

  python -m graph.extract_trad --method spacy|gliner [--chunks ids.json] [--out file.jsonl]

Record per chunk (one JSON line):
  {"chunk_id", "method", "entities": [{"name","type","label","start","end","score"}],
   "relations": [{"source","type","target","rule","evidence"}], "seconds"}
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = yaml.safe_load((ROOT / "graph_layer" / "schema.yaml").read_text(encoding="utf-8"))
CHUNKS = ROOT / "outputs" / "2_chunks.json"
MANIFEST = ROOT / "outputs" / "india_doc_manifest.json"
SPACY_MODEL = "en_core_web_lg"
GLINER_MODEL = "urchade/gliner_medium-v2.1"      # Apache-2.0
GLINER_THRESHOLD = 0.4

STATES = set()
if MANIFEST.exists():
    STATES = {r for r in json.loads(MANIFEST.read_text(encoding="utf-8"))["regions"] if not r.startswith("India")}
STATES |= {"Punjab", "Bengal", "Jammu and Kashmir"}
COUNTRIES = {"India", "China", "Nepal", "Bhutan", "Bangladesh", "Pakistan", "Myanmar", "Burma", "Sri Lanka", "Tibet",
             "England", "Canada", "US", "United States", "Afghanistan", "Japan", "Portugal", "France", "Britain"}
ANAPHORS = {"the state": "State", "the territory": "State", "the union territory": "State", "the region": "State"}


# ---------- shared helpers ----------
def region_of(headings: list[str]) -> str | None:
    return next((h for h in headings if h in STATES), None)


KEYWORD_TYPES = [   # head-word rules: the classic way to type names that NER labels too coarsely
    (r"\b(sanctuary|national park|tiger reserve|biosphere reserve)\b", "Park"),
    (r"\b(district|division)\b", "District"),
    (r"\b(dynasty|empire|sultanate|kingdom|nayaks|rajput)\b", "Dynasty"),
    (r"\b(university|institute|college|school|academy|vidyapeeth|vidyalaya|polytechnic|laboratory|centre)\b", "Institution"),
    (r"\b(falls|waterfalls?|river|lake|hills?|mountains?|ranges?|pass|valley|plateau|ghats|island|islands|sea|ocean|bay|gulf|strait|kunda)\b", "GeoFeature"),
    (r"\b(temple|fort|monastery|cathedral|church|mosque|palace|stadium|airport|museum|cemetery|memorial|highway|nh[ -]?\d+)\b", "Landmark"),
    (r"\b(festival|mela|utsav)\b", "Festival"),
    (r"\b(army|council|department|authority|corporation|government|party|board|organisation|organization|court|service|press)\b", "Organisation"),
]


def refine_type(name: str, typ: str) -> str:
    """Gazetteer + head-word refinement of the model's label."""
    if name in STATES:
        return "State"
    if name in COUNTRIES:
        return "Country"
    for pattern, t in KEYWORD_TYPES:
        if re.search(pattern, name, re.I):
            return t
    return typ


def clean(name: str) -> str:
    name = re.sub(r"^(the|a|an)\s+", "", name.strip(), flags=re.I)
    return re.sub(r"[\s,;:.]+$", "", name)


class Spans:
    """Character spans of entities in one text, to map parse tokens onto entities."""

    def __init__(self, ents):
        self.ents = sorted(ents, key=lambda e: e["start"])

    def at(self, start: int, end: int):
        for e in self.ents:
            if e["start"] <= start and end <= e["end"] or (start <= e["start"] < end):
                return e
        return None


def _subtree_span(tok):
    return tok.left_edge.idx, tok.right_edge.idx + len(tok.right_edge)


def _entity_for(tok, spans: Spans, region):
    """Entity covered by this token (or the noun phrase it heads); anaphors map to the region."""
    e = spans.at(tok.idx, tok.idx + len(tok))
    if e:
        return e["name"]
    s, t = _subtree_span(tok)
    phrase = tok.doc.text[s:t].lower()
    for a in ANAPHORS:
        if phrase.startswith(a) and region:
            return region
    for child in tok.children:                       # "the city of X", "the capital, X"
        if child.dep_ in ("prep", "appos", "compound"):
            for gc in [child, *child.children]:
                e = spans.at(gc.idx, gc.idx + len(gc))
                if e:
                    return e["name"]
    # fallback: a proper-noun phrase the NER missed ("the Mlechchha dynasty", "Hyderabad State")
    words = [t for t in tok.lefts if t.dep_ in ("compound", "amod", "nmod") and t.pos_ == "PROPN"]
    if tok.pos_ == "PROPN" or words:
        first = min([tok, *words], key=lambda t: t.i)
        name = clean(tok.doc[first.i: tok.i + 1].text)
        if len(name) > 2 and name.lower() not in ANAPHORS:
            return name
    return None


def dependency_relations(doc, spans: Spans, region) -> list[dict]:
    """Rule-based relations from the dependency parse (rules live in schema.yaml)."""
    verb_rules, copula_rules = SCHEMA["verb_rules"], SCHEMA["copula_rules"]
    out = []

    def add(src, typ, tgt, rule, sent):
        if src and tgt and src != tgt:
            out.append({"source": src, "type": typ, "target": tgt, "rule": rule, "evidence": sent.text[:200]})

    for sent in doc.sents:
        for tok in sent:
            # 1. verb rules: "<subj> ruled <obj>", "<X> was born in <Y>", "<river> flows through <Y>"
            if tok.pos_ in ("VERB", "AUX") and tok.lemma_.lower() in verb_rules:
                rule = verb_rules[tok.lemma_.lower()]
                passive = any(c.dep_ in ("nsubjpass", "auxpass") for c in tok.children)
                subj = next((c for c in tok.children if c.dep_ in ("nsubj", "nsubjpass")), None)
                if subj is None and tok.dep_ == "conj":       # "defeated X and conquered Y": share the subject
                    subj = next((c for c in tok.head.children if c.dep_ in ("nsubj", "nsubjpass")), None)
                if tok.dep_ in ("acl", "relcl") and tok.head.pos_ in ("NOUN", "PROPN") and \
                        (subj is None or subj.tag_ in ("WDT", "WP")):
                    subj = tok.head                      # "Hemis, located in Ladakh" / "Kerala, which was ruled by"
                objs = [c for c in tok.children if c.dep_ in ("dobj", "attr")]
                agent = next((gc for c in tok.children if c.dep_ == "agent" for gc in c.children if gc.dep_ == "pobj"), None)
                preps = rule.get("preps")
                for c in tok.children:
                    if c.dep_ == "prep" and (preps is None or c.text.lower() in preps):
                        objs += [gc for gc in c.children if gc.dep_ == "pobj"]
                s_ent = _entity_for(subj, spans, region) if subj is not None else None
                if passive and agent is not None:        # "Kerala was ruled by the Cheras, the Cholas and ..."
                    s_ent = s_ent or region               # "its being ruled by" -> the chunk's region
                    for a_tok in [agent, *agent.conjuncts]:
                        add(_entity_for(a_tok, spans, region), rule["type"], s_ent, f"verb:{tok.lemma_}(passive)", sent)
                    continue
                if rule.get("reverse"):                  # "Sikkim produces cardamom": the place is the subject
                    s_ent = s_ent or region                 # "... is grown" with no subject -> the chunk's region
                for o in objs:
                    for conj in [o, *o.conjuncts]:       # "ruled A, B and C"
                        add(s_ent, rule["type"], _entity_for(conj, spans, region), f"verb:{tok.lemma_}", sent)

            # 2. copula rules: "<X> is the capital of <Y>"
            if tok.lemma_ == "be":
                subj = next((c for c in tok.children if c.dep_ == "nsubj"), None)
                attr = next((c for c in tok.children if c.dep_ == "attr"), None)
                if subj is None or attr is None:
                    continue
                head = attr.lemma_.lower()
                compound = " ".join(t.lemma_.lower() for t in attr.lefts if t.dep_ in ("compound", "amod")) + " " + head
                typ = copula_rules.get(head) or copula_rules.get(compound.strip())
                if not typ:
                    continue
                of = next((gc for c in attr.children if c.dep_ == "prep" and c.text.lower() == "of"
                           for gc in c.children if gc.dep_ == "pobj"), None)
                add(_entity_for(subj, spans, region), typ, _entity_for(of, spans, region) if of is not None else None,
                    f"copula:{head}", sent)
    return out


def list_relations(text: str, ents: list[dict]) -> list[dict]:
    """Enumeration lines such as 'Panimur Falls (Assam)' or 'Gomarda Wildlife Sanctuary, Raigarh, 1975'."""
    out, by_start = [], sorted(ents, key=lambda e: e["start"])
    pos = 0
    for line in text.split("\n"):
        line_ents = [e for e in by_start if pos <= e["start"] < pos + len(line)]
        pos += len(line) + 1
        if len(line) > 160 or len(line_ents) < 2:
            continue
        first = line_ents[0]
        places = [e for e in line_ents[1:] if e["type"] in ("State", "City", "District", "Place", "Country", "GeoFeature")]
        if first["type"] in ("GeoFeature", "Park", "Landmark", "Institution", "City", "District") and places:
            out.append({"source": first["name"], "type": "LOCATED_IN", "target": places[0]["name"],
                        "rule": "list-line", "evidence": line[:200]})
    return out


# ---------- extractors ----------
class SpacyExtractor:
    method = "spacy"

    def __init__(self):
        import spacy
        self.nlp = spacy.load(SPACY_MODEL)
        self.label_map = SCHEMA["spacy_label_map"]

    def entities(self, doc):
        ents = []
        for e in doc.ents:
            typ = self.label_map.get(e.label_)
            if not typ:
                continue
            name = clean(e.text)
            if len(name) < 2:
                continue
            ents.append({"name": name, "type": refine_type(name, typ), "label": e.label_,
                         "start": e.start_char, "end": e.end_char, "score": None})
        return ents

    def run(self, chunk):
        doc = self.nlp(chunk["text"])
        ents = self.entities(doc)
        rels = dependency_relations(doc, Spans(ents), region_of(chunk["headings"]))
        return ents, rels + list_relations(chunk["text"], ents)


class GlinerExtractor:
    method = "gliner"

    def __init__(self):
        import spacy
        from gliner import GLiNER
        self.model = GLiNER.from_pretrained(GLINER_MODEL)
        self.nlp = spacy.load(SPACY_MODEL, disable=["ner"])        # parser only, for relations
        self.labels = {v["gliner_label"]: k for k, v in SCHEMA["entity_types"].items()}

    def entities(self, text):
        ents = []
        # GLiNER's encoder sees ~384 words at a time; feed it sentence windows
        for m in re.finditer(r"[^\n]+", text):
            seg, off = m.group(0), m.start()
            for i in range(0, len(seg), 1500):
                part = seg[i:i + 1500]
                for p in self.model.predict_entities(part, list(self.labels), threshold=GLINER_THRESHOLD):
                    name = clean(p["text"])
                    if len(name) < 2:
                        continue
                    typ = refine_type(name, self.labels[p["label"]])
                    ents.append({"name": name, "type": typ, "label": p["label"], "start": off + i + p["start"],
                                 "end": off + i + p["end"], "score": round(float(p["score"]), 3)})
        return ents

    def run(self, chunk):
        ents = self.entities(chunk["text"])
        doc = self.nlp(chunk["text"])
        rels = dependency_relations(doc, Spans(ents), region_of(chunk["headings"]))
        return ents, rels + list_relations(chunk["text"], ents)


EXTRACTORS = {"spacy": SpacyExtractor, "gliner": GlinerExtractor}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--method", choices=list(EXTRACTORS), required=True)
    ap.add_argument("--chunks", help="JSON list of chunk ids (default: all chunks)")
    ap.add_argument("--out", help="output .jsonl (default outputs/gr_extract_<method>.jsonl)")
    a = ap.parse_args()
    chunks = json.loads(CHUNKS.read_text(encoding="utf-8"))
    if a.chunks:
        wanted = json.loads(Path(a.chunks).read_text(encoding="utf-8"))
        chunks = [c for c in chunks if c["chunk_id"] in set(wanted)]
    out = Path(a.out) if a.out else ROOT / "outputs" / f"gr_extract_{a.method}.jsonl"
    t0 = time.perf_counter()
    ex = EXTRACTORS[a.method]()
    print(f"  loaded {a.method} in {time.perf_counter() - t0:.1f}s; {len(chunks)} chunks", flush=True)
    with out.open("w", encoding="utf-8") as f:
        for i, c in enumerate(chunks, 1):
            t = time.perf_counter()
            ents, rels = ex.run(c)
            f.write(json.dumps({"chunk_id": c["chunk_id"], "source": c["source"], "method": a.method,
                                "entities": ents, "relations": rels,
                                "seconds": round(time.perf_counter() - t, 3)}, ensure_ascii=False) + "\n")
            if i % 25 == 0 or i == len(chunks):
                print(f"    {i}/{len(chunks)} ({time.perf_counter() - t0:.0f}s)", flush=True)
    print(f"  -> {out}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
