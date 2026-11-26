"""Grounding check for an agent's draft answer (MCP check_answer): does the evidence support each sentence? No AI.

The draft is split into sentences. In each, the checkable parts are taken out: numbers (with units, lakh/crore,
percentages), dates, quoted phrases and names (capitalised word groups). A part is supported when it occurs in the
caller's evidence:
  - the passages given by id (fetch through the caller's groups), else passages found again for the question
    within the caller's groups,
  - the result rows of approved metrics for the question (when the caller may use data),
  - approved facts read from the documents the caller may see.
Numbers match by value (12,00,000 = 1200000 = 12 lakh = 1.2 million; 10% = 0.1), dates by day (1 April 2026 =
2026-04-01), names and quotes as normalised text (typo-tolerant). A sentence is supported when all its parts are,
partly supported when some are, unsupported when none are; a sentence with nothing checkable is "no_claims".
"""
import re

from rapidfuzz import fuzz

from facts.extract import DATE, norm_text, parse_date

NUM = re.compile(r"(?<![\w.])(?:INR|Rs\.?|₹|USD|\$|EUR|€|GBP|£)?\s?(\d[\d,]*(?:\.\d+)?)\s?(%|percent|lakh|lakhs|crore|crores|"
                 r"million|mn|billion|bn|k|thousand)?(?![\w])", re.I)
NAME = re.compile(r"\b[A-Z][a-zA-Z&.'\-]+(?:\s+(?:of|and|&|de|van|von)?\s?[A-Z][a-zA-Z&.'\-]+)*")
QUOTE = re.compile(r"[\"“]([^\"”]{4,200})[\"”]")
MULT = {"lakh": 1e5, "lakhs": 1e5, "crore": 1e7, "crores": 1e7, "million": 1e6, "mn": 1e6, "billion": 1e9, "bn": 1e9,
        "k": 1e3, "thousand": 1e3}
SKIP_NAMES = {"The", "This", "That", "These", "It", "In", "On", "At", "For", "And", "But", "If", "As", "A", "An", "Yes", "No",
              "According", "Based", "Per", "However", "Also", "There", "They", "We", "You", "I", "Our", "Their",
              "INR", "USD", "EUR", "GBP", "GST", "VAT", "Rs", "Q1", "Q2", "Q3", "Q4", "FY"}
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"“(])|\n+")


def numbers(text: str) -> list[float]:
    out = []
    for m in NUM.finditer(text):
        try:
            v = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        unit = (m.group(2) or "").lower()
        if unit in ("%", "percent"):
            out += [v, v / 100]
            continue
        out.append(v * MULT.get(unit, 1))
    return out


def dates(text: str) -> list[str]:
    return [d for d in (parse_date(m.group(0)) for m in re.finditer(DATE, text, re.I)) if d]


def claims(sentence: str) -> list[dict]:
    """The checkable parts of one sentence."""
    out, s = [], sentence
    for d_m in re.finditer(DATE, s, re.I):
        d = parse_date(d_m.group(0))
        if d:
            out.append({"type": "date", "text": d_m.group(0), "value": d})
    no_dates = re.sub(DATE, " ", s, flags=re.I)
    for m in NUM.finditer(no_dates):
        raw = m.group(0).strip()
        vals = numbers(raw)
        if vals and not (len(m.group(1)) == 4 and 1900 <= vals[0] <= 2100 and not m.group(2)):   # a bare year is a date part
            out.append({"type": "number", "text": raw, "value": vals[0]})
    for m in QUOTE.finditer(s):
        out.append({"type": "quote", "text": m.group(1), "value": norm_text(m.group(1))})
    for m in NAME.finditer(re.sub(QUOTE, " ", no_dates)):
        name = m.group(0).strip(" .")
        first = name.split()[0]
        if name in SKIP_NAMES or (first in SKIP_NAMES and len(name.split()) == 1):
            continue
        if m.start() == 0 and len(name.split()) == 1:
            continue                                       # the sentence's first word
        out.append({"type": "name", "text": name, "value": norm_text(name)})
    return out


class Evidence:
    def __init__(self, items: list[dict]):
        self.items = items                                 # {"id", "text", "kind"}
        self.text = [(i, norm_text(i["text"])) for i in items]
        self.nums = [(i, numbers(i["text"])) for i in items]
        self.dates = [(i, dates(i["text"]) + [x for x in re.findall(r"\d{4}-\d{2}-\d{2}", i["text"])]) for i in items]

    def find(self, c: dict) -> str | None:
        if c["type"] == "number":
            for i, ns in self.nums:
                if any(abs(n - c["value"]) <= max(1e-6, abs(c["value"]) * 1e-3) for n in ns):
                    return i["id"]
        elif c["type"] == "date":
            for i, ds in self.dates:
                if c["value"] in ds:
                    return i["id"]
        else:
            for i, t in self.text:
                if c["value"] in t:
                    return i["id"]
            if c["type"] == "name" and len(c["value"]) >= 5:
                for i, t in self.text:
                    if fuzz.partial_ratio(c["value"], t) >= 92:
                        return i["id"]
        return None


def gather(agent: dict, question: str, evidence_ids: list[str] | None) -> list[dict]:
    """The caller's evidence: given passages (checked against their groups) or a new search, approved metric rows
    and approved facts of documents they may see."""
    from . import engine
    items = []
    caps = set(agent["capabilities"])
    if evidence_ids:
        for pid in evidence_ids[:50]:
            p = engine.passage(pid.strip(), agent["groups"])
            if p:
                items.append({"id": p["id"], "text": p["text"], "kind": "passage", "source": p["source"]})
    elif caps & {"documents", "graph"} and question:
        for p in engine.search(question, agent["groups"], 8, use_graph="graph" in caps)["passages"]:
            items.append({"id": p["id"], "text": p["text"], "kind": "passage", "source": p["source"]})
    if "data" in caps and question:
        try:
            r = engine.metric_query(question=question, groups=None if agent.get("all_groups") else agent["groups"])
            for n, row in enumerate(r.get("rows") or []):
                items.append({"id": f"metric:{n}", "text": " ".join(f"{k} {v}" for k, v in row.items()), "kind": "metric"})
        except (ValueError, RuntimeError):
            pass
    try:
        from facts.store import facts_for
        for f in facts_for(agent["groups"]):
            items.append({"id": f"fact:{f['id']}", "text": f"{f['field'].replace('_', ' ')} {f['value']} {f['quote']}",
                          "kind": "fact", "source": f["document"]})
    except Exception:                                      # noqa: BLE001 - no facts
        pass
    return items


def check(agent: dict, question: str, answer: str, evidence_ids: list[str] | None = None) -> dict:
    ev = Evidence(gather(agent, question, evidence_ids))
    sentences = [s.strip() for s in _SENT.split(answer or "") if len(s.strip()) > 2]
    out, counts = [], {"supported": 0, "partly_supported": 0, "unsupported": 0, "no_claims": 0}
    for s in sentences:
        cl = claims(s)
        for c in cl:
            c["evidence"] = ev.find(c)
        if not cl:
            verdict = "no_claims"
        elif all(c["evidence"] for c in cl):
            verdict = "supported"
        elif any(c["evidence"] for c in cl):
            verdict = "partly_supported"
        else:
            verdict = "unsupported"
        counts[verdict] += 1
        out.append({"sentence": s, "verdict": verdict, "evidence": sorted({c["evidence"] for c in cl if c["evidence"]}),
                    "unsupported": [c["text"] for c in cl if not c["evidence"]],
                    "checked": [{k: c[k] for k in ("type", "text", "evidence")} for c in cl]})
    ok = counts["unsupported"] == 0 and counts["partly_supported"] == 0
    return {"grounded": ok, "summary": counts, "sentences": out, "evidence_used": len(ev.items),
            "guidance": "Remove or correct every unsupported number, date, name or quote, or say it is not in the "
                        "sources. 'no_claims' sentences have nothing checkable."}
