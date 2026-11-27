"""Lumora + India retrieval benchmark on the laptop (run when no app server holds the search index)."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from rag.retrieval_eval import run  # noqa: E402

qs = json.loads((ROOT / "test-data" / "lumora" / "lumora_eval_questions.json").read_text())
print("== Lumora (20 questions)", flush=True)
run(["hybrid", "hybrid+rerank", "+graph+coverage"], tag="laptop_lumora", groups=["public", "ops"], questions=qs)
print("== India document (54 questions)", flush=True)
run(["hybrid", "+graph+coverage"], tag="laptop_india", groups=["public"])
