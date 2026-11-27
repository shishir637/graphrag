"""Map Lumora gold answer snippets to passage ids (after chunking); write experiments/lumora_eval_questions.json."""
import json, re, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
chunks = json.loads((ROOT / "outputs/2_chunks.json").read_text())
gold = json.loads((ROOT / "experiments/lumora/lumora_retrieval_gold.json").read_text())
norm = lambda s: re.sub(r"\s+", " ", s.lower())
out, problems = [], []
for g in gold:
    ids = []
    for sn in g["snippets"]:
        hit = [c["chunk_id"] for c in chunks if norm(sn) in norm(c["text"])]
        if not hit: problems.append((g["question"], sn))
        ids += [h for h in hit[:1] if h not in ids]
    out.append({"question": g["question"], "type": g["type"], "gold_chunk_ids": ids, "snippets": g["snippets"]})
(ROOT / "experiments/lumora_eval_questions.json").write_text(json.dumps(out, indent=1))
print(len(out), "questions;", sum(len(o["gold_chunk_ids"]) for o in out), "gold passages; missing:", problems)
