"""Compare graph extraction modes on the 30 hand-labelled passages: speed and accuracy, side by side.

  python -m experiments.extract_compare                 # fast, accurate, and ai if it is set up
  python -m experiments.extract_compare --modes ai       # just one

Needs the documents prepared (outputs/2_chunks.json with the India set: the gold passages come from it).
"ai" uses the provider saved in Settings → AI extraction (or CL_AI_PROVIDER / CL_AI_MODEL / CL_AI_BASE_URL /
CL_AI_API_KEY). Its cache is cleared first, so the time is real.
Wall-clock time counts model loading for fast/accurate and parallel calls for ai: it is what a build feels like.
Output: printed table + outputs/gr_extract_compare.json
"""
import argparse
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main():
    from graph import extract_ai
    from graph.eval_extraction import GOLD, load, score
    from graph.nlp_extract import extract_all
    ap = argparse.ArgumentParser()
    ap.add_argument("--modes", default="fast,accurate,ai")
    ap.add_argument("--keep-cache", action="store_true", help="ai: reuse earlier answers (time is then not real)")
    a = ap.parse_args()
    gold = json.loads(GOLD.read_text(encoding="utf-8"))
    ids = [k for k in gold if not k.startswith("_")]
    have = {c["chunk_id"] for c in json.loads((ROOT / "outputs" / "2_chunks.json").read_text(encoding="utf-8"))}
    missing = [i for i in ids if i not in have]
    if len(missing) == len(ids):
        sys.exit("The gold passages are not in outputs/2_chunks.json: prepare the India documents first.")
    rows = {}
    for mode in [m.strip() for m in a.modes.split(",") if m.strip()]:
        if mode == "ai":
            bad = extract_ai.problems()
            if bad:
                print(f"ai: skipped ({' '.join(bad)})")
                continue
            if not a.keep_cache:
                shutil.rmtree(extract_ai.CACHE, ignore_errors=True)
        out = ROOT / "outputs" / f"gr_extract_compare_{mode}.jsonl"
        print(f"\n== {mode}", flush=True)
        t0 = time.perf_counter()
        extract_all(out, mode=mode, ids=ids, resume=False)
        wall = time.perf_counter() - t0
        method, recs = load(out)
        r = score(recs, gold)
        r.pop("per_chunk", None)
        r["wall_seconds"] = round(wall, 1)
        r["wall_seconds_per_passage"] = round(wall / max(1, len(ids) - len(missing)), 2)
        r["method"] = method
        if mode == "ai":
            u = json.loads((ROOT / "outputs" / "gr_ai_usage.json").read_text(encoding="utf-8"))
            r["ai"] = {k: u[k] for k in ("calls", "fell_back_to_spacy", "input_tokens", "output_tokens", "destination")}
        rows[mode] = r
    (ROOT / "outputs" / "gr_extract_compare.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(f"\n{len(ids) - len(missing)} hand-labelled passages")
    print(f"{'mode':<10}{'entity F1':>11}{'typed F1':>10}{'link recall':>13}{'typed link R':>14}{'typed link P':>14}{'s/passage':>11}{'total s':>9}")
    for m, r in rows.items():
        print(f"{m:<10}{r['entities']['f1']:>11.2f}{r['entities_typed']['f1']:>10.2f}{r['relations_loose']['recall']:>13.2f}"
              f"{r['relations_strict']['recall']:>14.2f}{r['relations_strict']['precision']:>14.2f}"
              f"{r['wall_seconds_per_passage']:>11.2f}{r['wall_seconds']:>9.1f}")
    if "ai" in rows:
        u = rows["ai"]["ai"]
        print(f"ai: {u['calls']} calls to {u['destination']}, {u['input_tokens']} input / {u['output_tokens']} output tokens"
              + (f", {u['fell_back_to_spacy']} fell back to spaCy" if u["fell_back_to_spacy"] else ""))
    print("  -> outputs/gr_extract_compare.json")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
