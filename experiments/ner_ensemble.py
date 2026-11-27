import json, re, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent; sys.path.insert(0, str(ROOT))
from graph.eval_extraction import score, norm, same
from graph.extract_trad import KEYWORD_TYPES, clean
gold = json.loads((ROOT / "graph_layer/gold_extraction.json").read_text())
sp = json.loads((ROOT / "experiments/out_ner_spacy_0.4.json").read_text())
gl = json.loads((ROOT / f"experiments/out_ner_gliner_{sys.argv[1] if len(sys.argv)>1 else '0.4'}.json").read_text())
STATES = json.loads((ROOT/"outputs/india_doc_manifest.json").read_text())["regions"] if (ROOT/"outputs/india_doc_manifest.json").exists() else []
def kw(name, typ):
    for p, t in KEYWORD_TYPES:
        if re.search(p, name, re.I): return t
    return typ
def run(use_spacy=True, use_kw=True, gaz=None, drop_spacy_only_types=()):
    recs = {}
    for cid in gl:
        ents = {}
        for e in gl[cid]["entities"]:
            n = clean(e["name"]); ents[norm(n)] = {"name": n, "type": e["type"]}
        if use_spacy:
            for e in sp[cid]["entities"]:
                n = clean(e["name"]); k = norm(n)
                if not k or any(same(n, x["name"]) for x in ents.values()): continue
                if e["type"] in drop_spacy_only_types: continue
                ents[k] = {"name": n, "type": e["type"]}
        for e in ents.values():
            if use_kw: e["type"] = kw(e["name"], e["type"])
            if gaz and e["name"] in gaz: e["type"] = gaz[e["name"]]
        recs[cid] = {"entities": list(ents.values()), "relations": [], "seconds": 0}
    s = score(recs, gold); return {k: (s[k]["precision"], s[k]["recall"], s[k]["f1"]) for k in ("entities", "entities_typed")}
print("gliner only        ", run(False, False))
print("gliner+kw          ", run(False, True))
print("gliner+spacy       ", run(True, False))
print("gliner+spacy+kw    ", run(True, True))
print("g+s+kw drop Place/Org-only", run(True, True, drop_spacy_only_types=("Place",)))
