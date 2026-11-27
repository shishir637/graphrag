"""Retrieval benchmark: does the right passage come back, and how high? No judge model, no API key.

Gold set: eval_questions.json - question, type, gold_chunk_ids (the passages that hold the answer).
Unanswerable questions have no gold passages and are skipped here.

Configurations (each one adds a step to the one before):
  dense             BGE-M3 vectors only
  bm25              keywords only
  hybrid            dense + BM25, reciprocal rank fusion
  hybrid+rerank     + bge-reranker-v2-m3 (the v0.1 product)
  +graph            + knowledge-graph candidates before re-ranking
  +graph+coverage   + one passage per question entity (the new default)

Metrics (averaged over questions; also per question type):
  recall@k   share of a question's gold passages in the top k
  all@5      every gold passage in the top 5 (what a multi-part or comparison question needs)
  mrr        1 / rank of the first gold passage
  ndcg@10    rank-weighted gain of the gold passages in the top 10

  python -m rag.retrieval_eval [--configs a,b] [--tag name]
Output: outputs/9_retrieval_eval_<tag>.json
"""
import argparse
import json
import math
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

from qdrant_client import models

from .config import COLLECTION, ROOT
from .utils import write_json

GOLD = ROOT / "eval_questions.json"
CONFIGS = ["dense", "bm25", "hybrid", "hybrid+rerank", "+graph", "+graph+coverage"]
K = 10


def _single(question: str, using: str, groups) -> list[str]:
    from .models import embed_dense, embed_sparse
    from .retrieve import build_filter
    from .store import active_collection, client
    q = embed_dense([question])[0] if using == "dense" else models.SparseVector(**embed_sparse([question], query=True)[0])
    pts = client().query_points(active_collection(), query=q, using=using, query_filter=build_filter(groups, None),
                                limit=K, with_payload=True).points
    return [p.payload["chunk_id"] for p in pts]


def ranked_ids(question: str, config: str, groups) -> list[str]:
    from .retrieve import retrieve
    if config in ("dense", "bm25"):
        return _single(question, config, groups)
    if config == "hybrid":
        r = retrieve(question, groups, save=False, top_k=K, use_graph=False, use_rerank=False, coverage=False)
        return r["stage_ids"]["rrf"][:K]
    if config == "hybrid+rerank":
        r = retrieve(question, groups, save=False, top_k=K, use_graph=False, use_rerank=True, coverage=False)
    elif config == "+graph":
        r = retrieve(question, groups, save=False, top_k=K, use_graph=True, use_rerank=True, coverage=False)
    else:
        r = retrieve(question, groups, save=False, top_k=5, use_graph=True, use_rerank=True, coverage=True)
        top5 = r["stage_ids"]["final"]
        rest = [c for c in r["stage_ids"]["reranked"] if c not in top5]
        return (top5 + rest)[:K]
    return r["stage_ids"]["final"][:K]


def score(ranked: list[str], gold: set[str]) -> dict:
    out = {f"recall@{k}": len(gold & set(ranked[:k])) / len(gold) for k in (1, 3, 5, 10)}
    out["all@5"] = float(gold <= set(ranked[:5]))
    first = next((i for i, c in enumerate(ranked) if c in gold), None)
    out["mrr"] = 0.0 if first is None else 1 / (first + 1)
    dcg = sum(1 / math.log2(i + 2) for i, c in enumerate(ranked[:K]) if c in gold)
    idcg = sum(1 / math.log2(i + 2) for i in range(min(len(gold), K)))
    out["ndcg@10"] = dcg / idcg
    return out


def run(configs=None, tag: str = "latest", groups=None, questions=None) -> dict:
    gold_qs = [q for q in (questions or json.loads(GOLD.read_text(encoding="utf-8"))) if q.get("gold_chunk_ids")]
    configs = configs or CONFIGS
    results, per_q = {}, defaultdict(dict)
    for cfg in configs:
        t0 = time.perf_counter()
        rows = []
        for q in gold_qs:
            ranked = ranked_ids(q["question"], cfg, groups or ["public"])
            s = score(ranked, set(q["gold_chunk_ids"]))
            rows.append((q.get("type", "all"), s))
            per_q[q["question"]][cfg] = {"ranked": ranked[:5], **{k: round(v, 3) for k, v in s.items()}}
        secs = (time.perf_counter() - t0) / max(1, len(gold_qs))
        summary = {m: round(statistics.mean(s[m] for _, s in rows), 3) for m in rows[0][1]}
        by_type = defaultdict(list)
        for t, s in rows:
            by_type[t].append(s)
        summary["by_type"] = {t: {m: round(statistics.mean(x[m] for x in v), 3) for m in ("recall@5", "all@5", "mrr")}
                              | {"n": len(v)} for t, v in by_type.items()}
        summary["seconds_per_question"] = round(secs, 2)
        results[cfg] = summary
        print(f"  {cfg:<18} R@1 {summary['recall@1']:.3f}  R@5 {summary['recall@5']:.3f}  all@5 {summary['all@5']:.3f}"
              f"  MRR {summary['mrr']:.3f}  nDCG@10 {summary['ndcg@10']:.3f}  ({secs:.1f}s/q)", flush=True)
    out = {"questions": len(gold_qs), "summary": results, "per_question": per_q}
    write_json(f"9_retrieval_eval_{tag}.json", out)
    return out


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--configs")
    ap.add_argument("--tag", default="latest")
    a = ap.parse_args()
    run(a.configs.split(",") if a.configs else None, a.tag)
