"""Evaluate Graph RAG against the vector RAG on the same questions.

  python -m graph.evaluate questions   # build outputs/gr_eval_questions.json
  python -m graph.evaluate run         # answer every question with both systems (resumable)
  python -m graph.evaluate score       # judge + metrics -> outputs/gr_5_eval_compare.json

Question types
  single / multi_part / comparison : existing eval questions about the sample regions (gold chunks known)
  multi_hop    : drafted from real 2-hop paths in the graph (A -r1-> B -r2-> C, two different source
                 chunks), so the gold path and gold chunks are known
  global       : big-picture questions across the regions (no single reference answer)

Metrics (deterministic ones first)
  gold_chunk_hit   all gold chunks are among the passages the answer was built from
  path_coverage    share of the gold path's entities that appear in the context the system used
  correctness      judge (Claude) compares the answer with the reference: 1 / 0.5 / 0
  groundedness     judge: is every claim supported by the passages shown to the answering model
  global           pairwise judge, both orders (position bias cancels): comprehensiveness, diversity, directness
  router_accuracy  did the router pick the graph for multi-hop and global questions
"""
import json
import random
import statistics
import sys
import time
from typing import Literal

from pydantic import BaseModel, Field

from rag.utils import write_json

from . import store
from .config import ANSWER_MODEL, OUT_DIR, ROOT

REGIONS = ["Kerala", "Tamil Nadu", "Karnataka", "Andhra Pradesh", "Telangana", "Puducherry"]
QFILE = OUT_DIR / "gr_eval_questions.json"
AFILE = OUT_DIR / "gr_eval_answers.jsonl"
GLOBAL_QUESTIONS = [
    "Which dynasties and empires shaped the history of the South Indian states, and which states did each rule?",
    "What are the main agricultural products and industries across the South Indian states?",
    "What major festivals and cultural traditions are found across South India?",
    "Which languages are spoken across the South Indian states and union territories?",
    "What are the most important educational and research institutions across South India?",
    "How did the borders and political status of the South Indian states change after Indian independence?",
]


def _claude():
    from rag.llm import claude
    return claude()


# ---------- questions ----------
class _HopQ(BaseModel):
    path_index: int
    question: str
    reference: str


class _HopQs(BaseModel):
    items: list[_HopQ]


def make_questions(n_hop: int = 12):
    existing = json.loads((ROOT / "eval_questions.json").read_text(encoding="utf-8"))
    qs = [q | {"origin": "existing"} for q in existing
          if q["type"] != "unanswerable" and q["regions"] and set(q["regions"]) <= set(REGIONS)]

    rows = store.cypher(
        "MATCH (a)-[r1]->(b)-[r2]->(c) WHERE a.id <> c.id AND size(r1.chunk_ids) > 0 AND size(r2.chunk_ids) > 0 "
        "AND NOT label(r1) = 'RELATED_TO' AND NOT label(r2) = 'RELATED_TO' AND b.degree < 40 "
        "RETURN a.name, label(r1), r1.description, r1.chunk_ids, b.name, label(r2), r2.description, r2.chunk_ids, c.name "
        "LIMIT 20000", columns=tuple("abcdefghi"))
    paths = [r for r in rows if not set(r[3]) & set(r[7])]          # the two hops come from different chunks
    rng = random.Random(5)
    rng.shuffle(paths)
    pick, seen_b = [], {}
    for p in paths:                                # spread over middle entities: at most 2 paths each
        if seen_b.get(p[4], 0) < 2 and p[5] != "PART_OF":
            pick.append(p)
            seen_b[p[4]] = seen_b.get(p[4], 0) + 1
        if len(pick) == n_hop * 2:
            break
    listing = "\n".join(f"{i}. {a} -[{t1}]-> {b} ({d1})  THEN  {b} -[{t2}]-> {c} ({d2})"
                        for i, (a, t1, d1, c1, b, t2, d2, c2, c) in enumerate(pick))
    resp = _claude().messages.parse(
        model=ANSWER_MODEL, max_tokens=8000, output_format=_HopQs,
        messages=[{"role": "user", "content":
                   "Each line is a two-step chain of facts from a document about India. Write a natural question "
                   "that can only be answered by combining BOTH steps - do not name the middle entity in the "
                   "question (e.g. 'In which state is the city where X was born?'). The reference answer states "
                   f"the final answer and the middle step. Pick the {n_hop} chains that make the most natural "
                   f"questions; skip trivial or odd ones.\n\n{listing}"}])
    for it in resp.parsed_output.items[:n_hop]:
        a, t1, d1, c1, b, t2, d2, c2, c = pick[it.path_index]
        qs.append({"question": it.question, "reference": it.reference, "type": "multi_hop", "origin": "graph_path",
                   "gold_path": [a, b, c], "gold_chunk_ids": [c1[0], c2[0]], "regions": REGIONS})
    for q in GLOBAL_QUESTIONS:
        qs.append({"question": q, "reference": "", "type": "global", "origin": "handwritten", "gold_chunk_ids": [],
                   "regions": REGIONS})
    QFILE.write_text(json.dumps(qs, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  {len(qs)} questions: " + ", ".join(f"{t} {sum(q['type'] == t for q in qs)}"
                                                 for t in sorted({q['type'] for q in qs})))


# ---------- run ----------
def run():
    from .query import answer
    qs = json.loads(QFILE.read_text(encoding="utf-8"))
    done = set()
    if AFILE.exists():
        done = {(json.loads(l)["i"], json.loads(l)["system"]) for l in AFILE.read_text(encoding="utf-8").splitlines()
                if json.loads(l)["mode"] != "error"}                 # failed answers are retried
    with AFILE.open("a", encoding="utf-8") as f:
        for i, q in enumerate(qs):
            for system, mode in (("vector", "vector"), ("graph", "auto")):
                if (i, system) in done:
                    continue
                t = time.perf_counter()
                try:
                    r = answer(q["question"], mode, headings=REGIONS)
                except Exception as e:                                # keep going; record the failure
                    r = {"mode": "error", "answer": f"ERROR: {e}", "found": False, "contexts": [], "timings_s": {}}
                rec = {"i": i, "system": system, "mode": r.get("mode"), "answer": r["answer"], "found": r.get("found"),
                       "contexts": [{"chunk_id": c["chunk_id"], "text": c["text"]} for c in r.get("contexts", [])],
                       "graph_relations": r.get("graph", {}).get("relations", []),
                       "graph_seeds": [s["name"] for s in r.get("graph", {}).get("seeds", [])],
                       "graph_info": {k: v for k, v in r.get("graph", {}).items() if k not in ("relations", "seeds")},
                       "usage": r.get("usage", {}), "timings_s": r.get("timings_s", {}),
                       "seconds": round(time.perf_counter() - t, 2)}
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
                print(f"   {i + 1}/{len(qs)} {q['type']:<11} {system:<6} -> {rec['mode']:<7} {rec['seconds']}s", flush=True)


# ---------- score ----------
class _Judge(BaseModel):
    correctness: Literal["correct", "partial", "incorrect"]
    grounded: bool = Field(description="every claim in the answer is supported by the passages")
    reason: str


class _Pair(BaseModel):
    comprehensiveness: Literal["A", "B", "tie"]
    diversity: Literal["A", "B", "tie"]
    directness: Literal["A", "B", "tie"]
    reason: str


def _judge(q, a) -> dict:
    ctx = "\n\n".join(f"[{c['chunk_id']}] {c['text'][:2500]}" for c in a["contexts"]) or "(none)"
    r = _claude().messages.parse(
        model=ANSWER_MODEL, max_tokens=2000, output_config={"effort": "low"}, output_format=_Judge,
        messages=[{"role": "user", "content":
                   f"Question: {q['question']}\nReference answer: {q['reference']}\n\nCandidate answer: {a['answer']}\n\n"
                   f"Passages the candidate was given:\n{ctx}\n\n"
                   "correctness: does the candidate state the reference's key facts (extra correct detail is fine)? "
                   "grounded: is every factual claim in the candidate supported by the passages?"}])
    return r.parsed_output.model_dump()


def _pairwise(question, a_text, b_text) -> dict:
    r = _claude().messages.parse(
        model=ANSWER_MODEL, max_tokens=2000, output_config={"effort": "low"}, output_format=_Pair,
        messages=[{"role": "user", "content":
                   f"Question: {question}\n\n<answer_A>\n{a_text}\n</answer_A>\n\n<answer_B>\n{b_text}\n</answer_B>\n\n"
                   "Which answer is better on each criterion? comprehensiveness: covers more of what the question "
                   "asks, across more states. diversity: more varied perspectives/examples. directness: answers the "
                   "question clearly and specifically."}])
    return r.parsed_output.model_dump()


def score():
    from concurrent.futures import ThreadPoolExecutor
    qs = json.loads(QFILE.read_text(encoding="utf-8"))
    ans: dict = {}
    for line in AFILE.read_text(encoding="utf-8").splitlines():
        a = json.loads(line)
        prev = ans.get((a["i"], a["system"]))
        if prev is None or prev["mode"] == "error" or a["mode"] != "error":   # a real answer beats an error record
            ans[(a["i"], a["system"])] = a
    per_q, jobs = [], []
    for i, q in enumerate(qs):
        row = {"i": i, "type": q["type"], "question": q["question"], "reference": q["reference"], "systems": {}}
        for system in ("vector", "graph"):
            a = ans.get((i, system))
            if not a:
                continue
            ctx_ids = {c["chunk_id"] for c in a["contexts"]}
            gold = set(q.get("gold_chunk_ids") or [])
            ctx_text = " ".join(c["text"] for c in a["contexts"]).lower() + " " + \
                " ".join(f"{e['source']} {e['target']}" for e in a.get("graph_relations", [])).lower()
            m = {"mode": a["mode"], "answer": a["answer"], "found": a["found"], "seconds": a["seconds"],
                 "tokens": sum(a.get("usage", {}).values()),
                 "gold_chunk_hit": (gold <= ctx_ids) if gold else None}
            if q.get("gold_path"):
                m["path_coverage"] = round(sum(n.lower() in ctx_text for n in q["gold_path"]) / len(q["gold_path"]), 2)
            row["systems"][system] = m
            if q["type"] != "global":
                jobs.append((row, system, q, a))
        per_q.append(row)

    with ThreadPoolExecutor(4) as pool:
        for (row, system, _, _), j in zip(jobs, pool.map(lambda x: _judge(x[2], x[3]), jobs)):
            row["systems"][system] |= {"correctness": {"correct": 1, "partial": 0.5, "incorrect": 0}[j["correctness"]],
                                       "grounded": j["grounded"], "judge_reason": j["reason"]}

    glob = []
    for row in per_q:
        if row["type"] != "global" or len(row["systems"]) < 2:
            continue
        v, g = row["systems"]["vector"]["answer"], row["systems"]["graph"]["answer"]
        first, second = _pairwise(row["question"], v, g), _pairwise(row["question"], g, v)   # both orders
        wins = {}
        for crit in ("comprehensiveness", "diversity", "directness"):
            pts = {"vector": 0, "graph": 0}
            for res, a_sys, b_sys in ((first, "vector", "graph"), (second, "graph", "vector")):
                w = res[crit]
                if w == "A":
                    pts[a_sys] += 1
                elif w == "B":
                    pts[b_sys] += 1
            wins[crit] = "graph" if pts["graph"] > pts["vector"] else "vector" if pts["vector"] > pts["graph"] else "tie"
        row["pairwise"] = wins
        glob.append(wins)

    def avg(vals):
        vals = [v for v in vals if v is not None]
        return round(statistics.mean(vals), 3) if vals else None

    types = sorted({r["type"] for r in per_q if r["type"] != "global"})
    summary = {"n_questions": len(per_q), "by_type": {}}
    for t in types + ["all_non_global"]:
        rows = [r for r in per_q if (r["type"] == t if t != "all_non_global" else r["type"] != "global")]
        summary["by_type"][t] = {"n": len(rows), **{s: {
            "correctness": avg([r["systems"].get(s, {}).get("correctness") for r in rows]),
            "grounded": avg([float(r["systems"][s]["grounded"]) for r in rows if "grounded" in r["systems"].get(s, {})]),
            "gold_chunk_hit": avg([float(r["systems"][s]["gold_chunk_hit"]) for r in rows
                                   if r["systems"].get(s, {}).get("gold_chunk_hit") is not None]),
            "path_coverage": avg([r["systems"].get(s, {}).get("path_coverage") for r in rows]),
            "median_seconds": avg([statistics.median([r["systems"][s]["seconds"] for r in rows if s in r["systems"]])]),
            "avg_tokens": avg([r["systems"].get(s, {}).get("tokens") for r in rows]),
        } for s in ("vector", "graph")}}
    summary["global_pairwise_wins"] = {crit: {w: sum(g[crit] == w for g in glob) for w in ("graph", "vector", "tie")}
                                       for crit in ("comprehensiveness", "diversity", "directness")}
    summary["router"] = {t: dict(__import__("collections").Counter(r["systems"].get("graph", {}).get("mode")
                                                                    for r in per_q if r["type"] == t))
                         for t in sorted({r["type"] for r in per_q})}
    graph = json.loads((OUT_DIR / "gr_3_graph.json").read_text(encoding="utf-8"))
    summary["graph"] = {k: graph[k] for k in ("entities", "relations", "chunks", "isolated_entities", "from_tables")}
    write_json("gr_5_eval_compare.json", {"summary": summary, "per_question": per_q})
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    {"questions": make_questions, "run": run, "score": score}[sys.argv[1]]()
