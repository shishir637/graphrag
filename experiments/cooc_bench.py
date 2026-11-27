import json, re, sys, itertools
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent; sys.path.insert(0, str(ROOT))
from graph.eval_extraction import score, norm, same
gold = json.loads((ROOT / "graph_layer/gold_extraction.json").read_text())
chunks = {c["chunk_id"]: c for c in json.loads((ROOT / "outputs/2_chunks.json").read_text())}
sp = json.loads((ROOT / "experiments/out_ner_spacy_0.4.json").read_text())
gl = json.loads((ROOT / "experiments/out_ner_gliner_0.4.json").read_text())
rel = json.loads((ROOT / "experiments/out_rel_gliner2_0.3.json").read_text())
import spacy; nlp = spacy.load("en_core_web_lg", disable=["ner", "lemmatizer"])
def ents_of(cid, use_sp=True):
    e = {norm(x["name"]): x for x in gl[cid]["entities"]}
    if use_sp:
        for x in sp[cid]["entities"]:
            if not any(same(x["name"], y["name"]) for y in e.values()): e[norm(x["name"])] = x
    return list(e.values())
def cooc(cid, window="sentence", use_sp=True, region=True):
    text = chunks[cid]["text"]; es = ents_of(cid, use_sp); out = []
    units = [s.text for s in nlp(text).sents] if window == "sentence" else [text]
    for u in units:
        here = [e["name"] for e in es if re.search(r"(?<!\w)" + re.escape(e["name"]) + r"(?!\w)", u)]
        for a, b in itertools.combinations(dict.fromkeys(here), 2):
            out.append({"source": a, "type": "RELATED_TO", "target": b})
    if region:  # heading entity (section topic) links to every entity in the chunk
        hs = chunks[cid].get("headings") or []
        for h in hs[:2]:
            for e in es:
                if norm(e["name"]) != norm(h): out.append({"source": e["name"], "type": "RELATED_TO", "target": h})
    return out
for cfg in [("sentence", False, False), ("sentence", True, False), ("sentence", True, True), ("chunk", True, False)]:
    recs = {cid: {"entities": [], "relations": cooc(cid, *cfg) + rel[cid]["relations"], "seconds": 0} for cid in gl}
    s = score(recs, gold)
    print(cfg, {k: (s[k]["precision"], s[k]["recall"], s[k]["f1"]) for k in ("relations_strict", "relations_loose")})
