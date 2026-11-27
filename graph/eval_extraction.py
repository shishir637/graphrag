"""Score extractors against the hand-labelled gold chunks (graph_layer/gold_extraction.json).

  python -m graph.eval_extraction outputs/gr_extract_spacy.jsonl outputs/gr_extract_gliner.jsonl ...

Metrics per extractor (micro-averaged over the gold chunks):
  entities       precision / recall / F1 on names (fuzzy match), and "typed" = name AND type must match
  relations      strict = source, target and relation type match;
                 loose  = the two entities are linked at all (either direction, any type)
  speed          seconds per chunk

Name matching is lenient on purpose: case, punctuation, "the", and near-spellings
("Gumti" vs "Gumti Wildlife Sanctuary" counts when one is a whole-word part of the other).
Output: outputs/gr_extract_eval.json
"""
import json
import re
import statistics
import sys
from pathlib import Path

from rapidfuzz import fuzz

ROOT = Path(__file__).resolve().parent.parent
GOLD = ROOT / "graph_layer" / "gold_extraction.json"


def norm(s: str) -> str:
    s = s.lower().replace("&", "and")
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\b(the|of)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def same(a: str, b: str) -> bool:
    a, b = norm(a), norm(b)
    if not a or not b:
        return False
    if a == b or fuzz.ratio(a, b) >= 90:
        return True
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 4 and re.search(rf"\b{re.escape(short)}\b", long_) is not None and \
        len(short.split()) >= max(1, len(long_.split()) - 2)


def prf(tp_p, n_p, tp_g, n_g):
    p = tp_p / n_p if n_p else 0.0
    r = tp_g / n_g if n_g else 0.0
    return {"precision": round(p, 3), "recall": round(r, 3), "f1": round(2 * p * r / (p + r), 3) if p + r else 0.0,
            "predicted": n_p, "gold": n_g}


def score(records: dict, gold: dict) -> dict:
    ent = {"tp_p": 0, "n_p": 0, "tp_g": 0, "n_g": 0}
    ent_t = dict(ent)
    rel_s, rel_l = dict(ent), dict(ent)
    secs, per_chunk = [], {}
    for cid, g in gold.items():
        if cid.startswith("_"):
            continue
        r = records.get(cid, {"entities": [], "relations": [], "seconds": 0})
        secs.append(r.get("seconds", 0))
        # de-duplicate predictions by name
        pe = {}
        for e in r["entities"]:
            pe.setdefault(norm(e["name"]), e)
        pe = list(pe.values())
        ge = [{"name": n, "type": t} for n, t in g["entities"]]
        m_p = sum(any(same(p["name"], x["name"]) for x in ge) for p in pe)
        m_g = sum(any(same(p["name"], x["name"]) for p in pe) for x in ge)
        mt_p = sum(any(same(p["name"], x["name"]) and p["type"] == x["type"] for x in ge) for p in pe)
        mt_g = sum(any(same(p["name"], x["name"]) and p["type"] == x["type"] for p in pe) for x in ge)
        for acc, a, b in ((ent, m_p, m_g), (ent_t, mt_p, mt_g)):
            acc["tp_p"] += a; acc["n_p"] += len(pe); acc["tp_g"] += b; acc["n_g"] += len(ge)

        pr = {(norm(x["source"]), x["type"], norm(x["target"])): x for x in r["relations"]}
        pr = list(pr.values())
        gr = [{"source": s, "type": t, "target": o} for s, t, o in g["relations"]]

        def strict(p, x):
            return p["type"] == x["type"] and same(p["source"], x["source"]) and same(p["target"], x["target"])

        def loose(p, x):
            return (same(p["source"], x["source"]) and same(p["target"], x["target"])) or \
                   (same(p["source"], x["target"]) and same(p["target"], x["source"]))
        for acc, fn in ((rel_s, strict), (rel_l, loose)):
            acc["tp_p"] += sum(any(fn(p, x) for x in gr) for p in pr); acc["n_p"] += len(pr)
            acc["tp_g"] += sum(any(fn(p, x) for p in pr) for x in gr); acc["n_g"] += len(gr)
        per_chunk[cid] = {"entities_found": f"{m_g}/{len(ge)}", "relations_found": f"{sum(any(strict(p, x) for p in pr) for x in gr)}/{len(gr)}",
                          "predicted_entities": len(pe), "predicted_relations": len(pr)}
    return {
        "entities": prf(**ent), "entities_typed": prf(**ent_t),
        "relations_strict": prf(**rel_s), "relations_loose": prf(**rel_l),
        "seconds_per_chunk": round(statistics.mean(secs), 3) if secs else None, "per_chunk": per_chunk,
    }


def load(path: Path) -> tuple[str, dict]:
    recs = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return (recs[0]["method"] if recs else path.stem), {r["chunk_id"]: r for r in recs}


def main(paths):
    gold = json.loads(GOLD.read_text(encoding="utf-8"))
    results = {}
    for p in paths:
        method, recs = load(Path(p))
        results[method] = score(recs, gold)
    from rag.config import OUT_DIR
    (OUT_DIR / "gr_extract_eval.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    cols = ["entities", "entities_typed", "relations_strict", "relations_loose"]
    print(f"{'method':<10}" + "".join(f"{c:>26}" for c in cols) + f"{'s/chunk':>10}")
    for m, r in results.items():
        print(f"{m:<10}" + "".join(f"{'P%.2f R%.2f F%.2f' % (r[c]['precision'], r[c]['recall'], r[c]['f1']):>26}"
                                   for c in cols) + f"{r['seconds_per_chunk']:>10}")
    print("  -> outputs/gr_extract_eval.json")
    return results


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main(sys.argv[1:])
