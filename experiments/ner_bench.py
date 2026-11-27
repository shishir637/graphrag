"""Benchmark no-LLM entity extractors on the 30 gold chunks. Usage: python experiments/ner_bench.py METHOD [threshold]"""
import json, sys, time
from pathlib import Path
import yaml
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from graph.eval_extraction import score
gold = json.loads((ROOT / "graph_layer/gold_extraction.json").read_text())
schema = yaml.safe_load((ROOT / "graph_layer/schema.yaml").read_text())
chunks = {c["chunk_id"]: c for c in json.loads((ROOT / "outputs/2_chunks.json").read_text())}
ids = [k for k in gold if not k.startswith("_")]
method = sys.argv[1]; th = float(sys.argv[2]) if len(sys.argv) > 2 else 0.4
labels = {v["gliner_label"]: k for k, v in schema["entity_types"].items()}
recs = {}
if method.startswith("gliner2"):
    from gliner2 import GLiNER2
    name = {"gliner2": "fastino/gliner2-base-v1", "gliner2L": "fastino/gliner2-large-v1"}[method]
    m = GLiNER2.from_pretrained(name)
    descs = {k: v["description"] for k, v in schema["entity_types"].items()}
    for cid in ids:
        t = time.perf_counter(); text = chunks[cid]["text"]
        r = m.extract_entities(text, descs, threshold=th)
        ents = [{"name": n, "type": typ} for typ, ns in r["entities"].items() for n in ns]
        recs[cid] = {"entities": ents, "relations": [], "seconds": time.perf_counter() - t}
elif method.startswith("gliner"):
    from gliner import GLiNER
    name = {"gliner": "urchade/gliner_medium-v2.1", "glinerL": "urchade/gliner_large-v2.1", "glinerM": "knowledgator/gliner-multitask-large-v0.5"}[method]
    m = GLiNER.from_pretrained(name)
    for cid in ids:
        t = time.perf_counter(); text = chunks[cid]["text"]
        ents = [{"name": e["text"], "type": labels[e["label"]]} for e in m.predict_entities(text, list(labels), threshold=th)]
        recs[cid] = {"entities": ents, "relations": [], "seconds": time.perf_counter() - t}
elif method == "spacy":
    import spacy
    nlp = spacy.load("en_core_web_lg"); lm = schema["spacy_label_map"]
    for cid in ids:
        t = time.perf_counter(); doc = nlp(chunks[cid]["text"])
        ents = [{"name": e.text, "type": lm[e.label_]} for e in doc.ents if lm.get(e.label_)]
        recs[cid] = {"entities": ents, "relations": [], "seconds": time.perf_counter() - t}
s = score(recs, gold)
out = {k: s[k] for k in ("entities", "entities_typed", "seconds_per_chunk")}
print(method, th, json.dumps(out))
Path(ROOT / f"experiments/out_ner_{method}_{th}.json").write_text(json.dumps(recs))
