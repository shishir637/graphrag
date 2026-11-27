"""Grounding check: is each sentence of an answer supported by the evidence it cites? No language model.

The connected AI writes the answer; before it replies it can send the answer with the passage ids and metric
results it used. For every sentence of the answer:

  support   the cited passages are split into sentences (metric rows become "column: value" lines); the
            best-matching evidence sentence is found with the re-ranker Context Layer already runs for search
            (bge-reranker-v2-m3, a cross-encoder that reads both texts together), scored 0..1
  facts     every number, percentage, date and name in the sentence must appear in the evidence: numbers are
            compared as numbers (11.1% matches 0.1109 and 11.1; 225,394.98 matches 225394.98 and ~225,395)

  supported      support >= 0.5 and every fact found
  weak           some support, or a name not found in the evidence
  not_supported  little support, or a number or date that is not in the evidence (a wrong number is the
                 costliest mistake, so it is never waved through)

Without passage ids, the answer's own words are searched (the result says so: nothing was cited).
"""
import math
import re

_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])|\n+")
_CITE = re.compile(r"\[(?:[a-f0-9]{8,16})(?:\s*,\s*[a-f0-9]{8,16})*\]|\((?:passage|source)s?:?[^)]*\)", re.I)
_NUM = re.compile(r"(?<![\w.])[-+]?\$?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s?%?")
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b(?:\d{1,2}\s)?(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?"
                   r"(?:\s\d{1,2})?,?\s\d{4}\b")
_NAME = re.compile(r"\b[A-Z][a-z]+(?:[-'][A-Z][a-z]+)?(?:\s+(?:(?:of|and|de|van)\s+)?(?:[A-Z][a-z]+|[A-Z]{2,})){1,3}\b")
_SKIP = re.compile(r"^(sources?|references?|citations?|note)\b", re.I)
_STOP = {"the", "and", "for", "with", "that", "this", "from", "are", "was", "were", "has", "have", "our", "your",
         "which", "what", "when", "there", "their", "they", "into", "than", "then", "also", "about"}
SUPPORTED, WEAK = 0.5, 0.15


def _sentences(text: str) -> list[str]:
    text = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s+", "", text, flags=re.M)          # list markers
    text = re.sub(r"[*_`#>|]+", " ", text)                                      # markdown
    out = []
    for s in _SENT.split(text):
        s = " ".join(s.split()).strip()
        if len(s.split()) >= 3:
            out.append(s)
    return out


_TIME = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
_ISO = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?\b")


def _numbers(text: str) -> list[tuple[str, float, bool]]:
    """Numbers in a text; clock times and ISO dates are not numbers (dates are checked as dates)."""
    text = _TIME.sub(" ", _ISO.sub(" ", text))
    out = []
    dates = {m.group(0) for m in _DATE.finditer(text)}
    for m in _NUM.finditer(text):
        raw = m.group(0).strip()
        if any(raw in d for d in dates) and re.fullmatch(r"\d{1,4}", raw.strip("%")):
            continue                                    # the day or year of a date: checked as a date
        pct = raw.endswith("%")
        try:
            v = float(raw.strip("%+$ ").replace(",", ""))
        except ValueError:
            continue
        out.append((raw, v, pct))
    return out


def _decimals(raw: str) -> int:
    core = raw.strip("%+$ ").replace(",", "")
    return len(core.split(".")[1]) if "." in core else 0


def _number_found(raw: str, v: float, pct: bool, pool: list[float]) -> bool:
    """A number in the answer matches the evidence if they agree to the answer's own precision
    (an answer that rounds 225394.98 to 225,395, or 0.1109 to 11.1%, is right)."""
    tol = 0.5 * 10 ** (-_decimals(raw)) + 1e-9
    cands = [(v, tol)]
    if pct:
        cands.append((v / 100, tol / 100))                # 11.1% vs a ratio 0.1109
    for c, t in cands:
        if any(abs(c - p) <= t for p in pool):
            return True
    return False


def _evidence_numbers(texts: list[str]) -> list[float]:
    vals = []
    for t in texts:
        for _, v, pct in _numbers(t):
            vals.append(v)
            if pct:
                vals.append(v / 100)
    return vals


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9\s]", " ", s.lower()).split())


def _words(s: str) -> set[str]:
    return {w for w in _norm(s).split() if len(w) > 2 and w not in _STOP}


def _lexical(a: str, b: str) -> float:
    wa, wb = _words(a), _words(b)
    return len(wa & wb) / max(1, len(wa)) if wa else 0.0


def _support(sentence: str, evidence: list[str]) -> tuple[float, str]:
    """Best evidence sentence and a 0..1 support score (re-ranker; word overlap when it is unavailable)."""
    if not evidence:
        return 0.0, ""
    ranked = sorted(evidence, key=lambda e: _lexical(sentence, e), reverse=True)[:8]
    try:
        import os
        if os.environ.get("CL_GROUNDING_RERANK", "1") == "0":
            raise RuntimeError("re-ranker off")
        from rag.models import rerank_scores
        scores = [1 / (1 + math.exp(-s)) for s in rerank_scores(sentence, ranked)]
        lex = [_lexical(sentence, e) for e in ranked]
        best = max(range(len(ranked)), key=lambda i: max(scores[i], lex[i] * 0.9))
        return round(max(scores[best], lex[best] * 0.9), 3), ranked[best]
    except Exception:                                   # noqa: BLE001 - model missing: fall back to overlap
        lex = [_lexical(sentence, e) for e in ranked]
        best = max(range(len(ranked)), key=lambda i: lex[i])
        return round(lex[best], 3), ranked[best]


def _date_found(d: str, raw_text: str, blob: str) -> bool:
    """'14 March 2026', 'Mar 14, 2026' and '2026-03-14' are the same date."""
    from datetime import datetime
    if _norm(d) in blob or d in raw_text:
        return True
    clean = d.replace(".", "").replace(",", "")
    for fmt in ("%d %B %Y", "%d %b %Y", "%B %d %Y", "%b %d %Y", "%Y-%m-%d"):
        try:
            iso = datetime.strptime(clean, fmt).date().isoformat()
        except ValueError:
            continue
        return iso in raw_text or iso.replace("-", "/") in raw_text
    for fmt in ("%B %Y", "%b %Y"):
        try:
            ym = datetime.strptime(clean, fmt).strftime("%Y-%m")
        except ValueError:
            continue
        return ym in raw_text
    return False


def _flatten(rows) -> list[str]:
    """Metric results (rows, or a whole query_metrics/ask result) -> 'column: value' lines."""
    out = []

    def walk(x, key=""):
        if isinstance(x, dict):
            if "rows" in x and isinstance(x["rows"], list):
                walk(x["rows"])
                if isinstance(x.get("answer"), str):
                    out.append(x["answer"])
                return
            line = "; ".join(f"{k}: {v}" for k, v in x.items() if not isinstance(v, (dict, list)))
            if line:
                out.append(line)
            for k, v in x.items():
                if isinstance(v, (dict, list)):
                    walk(v, k)
        elif isinstance(x, list):
            for y in x:
                walk(y, key)
        elif x is not None:
            out.append(f"{key}: {x}" if key else str(x))
    walk(rows)
    return out


def check(answer: str, passages: list[dict], metric_lines: list[str]) -> dict:
    ev_sents, by_sent = [], {}
    for p in passages:
        for s in _sentences(p["text"]) or [p["text"]]:
            ev_sents.append(s)
            by_sent[s] = p["id"]
    evidence = ev_sents + metric_lines
    blob = _norm(" ".join(p["text"] for p in passages) + " " + " ".join(metric_lines))
    pool = _evidence_numbers([p["text"] for p in passages] + metric_lines)
    raw_text = " ".join(p["text"] for p in passages) + " " + " ".join(metric_lines)
    results = []
    for s in _sentences(_CITE.sub("", answer)):
        if _SKIP.match(s):
            continue
        score, best = _support(s, evidence)
        missing_nums = [raw for raw, v, pct in _numbers(s) if not _number_found(raw, v, pct, pool)]
        missing_dates = [d for d in dict.fromkeys(m.group(0) for m in _DATE.finditer(s)) if not _date_found(d, raw_text, blob)]
        missing_names = [n for n in dict.fromkeys(m.group(0) for m in _NAME.finditer(s))
                         if _norm(n) not in blob and not s.startswith(n + " is")]
        nums = _numbers(s)
        if nums and not missing_nums and best and all(_number_found(raw, v, pct, _evidence_numbers([best]))
                                                     for raw, v, pct in nums):
            score = max(score, 0.6)                    # every number sits in the best evidence line itself
        if missing_nums or missing_dates or (missing_names and score < SUPPORTED):
            verdict = "not_supported"
        elif score >= SUPPORTED and not missing_names:
            verdict = "supported"
        elif score >= WEAK:
            verdict = "weak"
        else:
            verdict = "not_supported"
        r = {"sentence": s, "verdict": verdict, "support": score}
        if best:
            r["evidence"] = best[:300]
            if best in by_sent:
                r["passage_id"] = by_sent[best]
            else:
                r["from"] = "metric results"
        missing = missing_nums + missing_dates + missing_names
        if missing:
            r["not_in_evidence"] = missing
        results.append(r)
    counts = {k: sum(1 for r in results if r["verdict"] == k) for k in ("supported", "weak", "not_supported")}
    total = max(1, len(results))
    return {"grounded": counts["not_supported"] == 0 and counts["weak"] <= total // 4,
            "score": round((counts["supported"] + 0.5 * counts["weak"]) / total, 2), "counts": counts,
            "sentences": results}


def check_answer(answer: str, groups: list[str], passage_ids: list[str] | None = None,
                 metric_results=None) -> dict:
    from . import engine
    answer = (answer or "").strip()
    if not answer:
        raise ValueError("Give the answer text to check.")
    ids = [i.strip() for i in (passage_ids or []) if i and i.strip()]
    passages, unknown = [], []
    for i in dict.fromkeys(ids):
        p = engine.passage(i, groups)
        (passages.append(p) if p else unknown.append(i))
    metric_lines = _flatten(metric_results) if metric_results else []
    searched = False
    if not passages and not metric_lines:
        searched = True
        try:
            passages = [{"id": p["id"], "text": p["text"]} for p in engine.search(answer[:1000], groups, top_k=5)["passages"]]
        except Exception:                               # noqa: BLE001 - no index yet
            passages = []
    out = check(answer, passages, metric_lines)
    out["checked_against"] = {"passages": [p["id"] for p in passages], "metric_lines": len(metric_lines)}
    if unknown:
        out["unknown_passage_ids"] = unknown
    if searched:
        out["note"] = ("Nothing was cited, so the answer's own words were searched. Cite passage ids and pass the "
                       "metric results you used for a real check.")
    out["next"] = ("Fix or remove every not_supported sentence (a number not in the evidence is usually a "
                   "calculation or copying mistake), then reply. Weak sentences: soften them or cite better evidence."
                   if not out["grounded"] else "Grounded: every sentence is backed by the evidence. Reply with the citations.")
    return out
