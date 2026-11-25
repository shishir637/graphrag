"""Learn from questions the layer could not serve (no AI model).

engine.ask records gaps (table sl_gaps): a counting question no metric matched, or a question with no evidence.
Weekly (app/scheduler.py) this job looks at the words of repeated gaps and proposes synonyms as drafts, e.g.
"turnover" asked 5 times with no metric -> synonym turnover => net_revenue, when the word closely resembles a
metric's name or label or a dimension. An owner approves or retires each suggestion.
"""
import re

from rapidfuzz import fuzz, process

MIN_COUNT = 3
MIN_SCORE = 85
STOP = set("the a an of in on at to for and or is are was were be by with from as that this which what who how "
           "many much total number count sum average per by our we do does did show me give list all".split())


def record(question: str, reason: str):
    """Note a question the layer could not serve (called by engine.ask; never raises)."""
    try:
        from app import core
        t = core.current_tenant()
        if not t:
            return
        q = re.sub(r"\s+", " ", question.strip().lower())[:300]
        core.db().execute(
            "INSERT INTO sl_gaps (tenant_id, question, reason, first_seen, last_seen) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (tenant_id, question, reason) DO UPDATE SET count = sl_gaps.count + 1, last_seen = excluded.last_seen",
            (t, q, reason, core.now(), core.now()))
    except Exception:                                      # noqa: BLE001 - learning must never break answering
        pass


def run() -> dict:
    from app import core
    from . import repo
    from .yamlio import load_enrichment, load_metrics, load_model
    rows = core.db().execute("SELECT question, count FROM sl_gaps WHERE tenant_id = ? AND reason = 'no_metric'",
                             (core.current_tenant(),)).fetchall()
    words: dict[str, int] = {}
    for r in rows:
        for w in set(re.findall(r"[a-z][a-z-]{3,}", r["question"])) - STOP:
            words[w] = words.get(w, 0) + r["count"]
    targets: dict[str, str] = {}
    for m in load_metrics().metrics:
        if m.status == "approved":
            targets[m.label.lower()] = m.name
            targets[m.name.replace("_", " ")] = m.name
    for e in load_model().entities:
        for d in e.dimensions:
            targets[(d.label or d.name).lower()] = f"{e.name}.{d.name}"
    taken = {s.term.lower() for s in load_enrichment().synonyms} | set(targets)
    proposed = []
    for w, n in sorted(words.items(), key=lambda x: -x[1]):
        if n < MIN_COUNT or w in taken:
            continue
        hit = process.extractOne(w, list(targets), scorer=fuzz.WRatio)
        if hit and hit[1] >= MIN_SCORE:
            target = targets[hit[0]]
            name = f"{w}=>{target}"
            if repo.get("synonym", name) is None:
                repo.put("synonym", name, {"term": w, "target": target}, status="draft", source="learned", by="learn",
                         note=f"asked {n} times without a match; looks like '{hit[0]}'")
                proposed.append({"term": w, "target": target, "asked": n})
    core.log_event("system", "learn", "semantic.suggest", f"{len(proposed)} synonyms", {"proposed": proposed})
    print(f"  proposed {len(proposed)} synonym drafts from {len(rows)} unanswered questions")
    return {"proposed": proposed}
