import json, re, sys, time
from pathlib import Path
import yaml
ROOT = Path(__file__).resolve().parent.parent; sys.path.insert(0, str(ROOT))
from graph.eval_extraction import score
gold = json.loads((ROOT / "graph_layer/gold_extraction.json").read_text())
schema = yaml.safe_load((ROOT / "graph_layer/schema.yaml").read_text())
chunks = {c["chunk_id"]: c for c in json.loads((ROOT / "outputs/2_chunks.json").read_text())}
ids = [k for k in gold if not k.startswith("_")]
th = float(sys.argv[1]) if len(sys.argv) > 1 else 0.5
from gliner2 import GLiNER2
m = GLiNER2.from_pretrained("fastino/gliner2-base-v1")
NAT = sys.argv[2] == "nat" if len(sys.argv) > 2 else False
rtypes = {k: v["description"] for k, v in schema["relation_types"].items() if k != "RELATED_TO"}
nat = {k.lower().replace("_", " "): k for k in rtypes}
if NAT: rtypes = list(nat)
recs = {}
for cid in ids:
    t = time.perf_counter(); text = chunks[cid]["text"]
    r = m.extract_relations(text, rtypes, threshold=th)
    rels = []
    for typ, pairs in (r.get("relation_extraction") or {}).items():
        typ = nat.get(typ, typ.split(":")[0].strip())
        for p in pairs:
            if isinstance(p, dict): s, o = p.get("head") or p.get("source"), p.get("tail") or p.get("target")
            else: s, o = p
            if isinstance(s, dict): s = s.get("text")
            if isinstance(o, dict): o = o.get("text")
            if s and o: rels.append({"source": s, "type": typ, "target": o})
    recs[cid] = {"entities": [], "relations": rels, "seconds": time.perf_counter() - t}
print(json.dumps([x for x in recs.values() if x["relations"]][:1])[:500])
s = score(recs, gold)
print("gliner2-rel", th, {k: (s[k]["precision"], s[k]["recall"], s[k]["f1"]) for k in ("relations_strict", "relations_loose")}, s["seconds_per_chunk"])
Path(ROOT / f"experiments/out_rel_gliner2_{th}.json").write_text(json.dumps(recs))
