"""Rule-based document kinds and typed facts, with provenance (no AI model).

kind_of(name, text) -> contract | invoice | meeting | policy | other   (keyword scores over headings and text;
                       optional local GLiNER zero-shot when CL_FACTS_ZERO_SHOT=1 and the rules say "other")
extract(kind, chunk) -> [Fact]: every fact has the passage id, the exact quote and its span in the passage text,
                       the extractor and a confidence, so it can be checked against its source.

Fields per kind (one record per document unless noted):
  contract  parties, party_1, party_2, effective_date, term_months, notice_days, annual_fee, currency,
            payment_terms_days, governing_law, auto_renew, liability
  invoice   invoice_number, invoice_date, vendor, amount, tax, currency
  meeting   meeting_date, attendees, decision (one record each), action (owner, task, due_date; one record each)
"""
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import date

MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                      "september", "october", "november", "december"], 1)}
MONTHS |= {k[:3]: v for k, v in MONTHS.items()} | {"sept": 9}
_MON = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
DATE = (rf"(?:\d{{1,2}}(?:st|nd|rd|th)?\s+{_MON}\.?,?\s+\d{{4}}|{_MON}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}"
        r"|\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})")
MONEY = r"(?:(?P<cur>INR|Rs\.?|₹|USD|US\$|\$|EUR|€|GBP|£)\s?)?(?P<num>\d[\d,]*(?:\.\d+)?)(?:\s?(?P<unit>lakh|lakhs|crore|crores|million|bn|billion|k))?"
CUR = {"rs": "INR", "rs.": "INR", "₹": "INR", "inr": "INR", "usd": "USD", "us$": "USD", "$": "USD", "eur": "EUR",
       "€": "EUR", "gbp": "GBP", "£": "GBP"}
WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
           "eleven": 11, "twelve": 12, "fifteen": 15, "eighteen": 18, "twenty": 20, "twenty-four": 24, "thirty": 30,
           "sixty": 60, "ninety": 90}

NUMW = r"(?:\d+|" + "|".join(sorted(WORDNUM, key=len, reverse=True)) + r")"     # 15 or fifteen, never other words

KIND_WORDS = {
    "contract": [r"\bagreement\b", r"\bcontract\b", r"\bpart(?:y|ies)\b", r"governing law", r"\bterminat", r"\bindemn",
                 r"\bliabilit", r"\brenew", r"hereinafter", r"\bwhereas\b"],
    "invoice": [r"\binvoice\b", r"\bbill to\b", r"\bbill from\b", r"amount due", r"\bgst\b|\bvat\b|\btax\b",
                r"invoice (?:no|number|#)", r"payment terms"],
    "meeting": [r"\bminutes\b", r"\battendees\b", r"action items?", r"\bdecisions?\b", r"\bagenda\b", r"\bmeeting\b"],
    "policy": [r"\bpolicy\b", r"\bplaybook\b", r"\brule\b", r"\bmust\b", r"\bshall not\b", r"\brequire[sd]?\b",
               r"\bguideline", r"\bprocedure\b"],
}


@dataclass
class Fact:
    kind: str
    record: str               # which record of the document (the document itself, or #n for repeated items)
    field: str
    value: str                # as written
    norm: object              # normalised value (number, ISO date, list, bool)
    chunk_id: str
    quote: str                # the exact text the value was read from
    start: int                # span of the quote in the passage text
    end: int
    extractor: str = "rules"
    confidence: float = 0.9
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


# ------------------------------------------------------------------ normalisation

def parse_date(s: str) -> str | None:
    s = s.strip().lower().replace(",", " ")
    s = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", s)
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    try:
        if m:
            return date(int(m[1]), int(m[2]), int(m[3])).isoformat()
        m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
        if m:                                              # day/month/year (Indian and European order)
            return date(int(m[3]), int(m[2]), int(m[1])).isoformat()
        parts = s.replace(".", " ").split()
        if len(parts) == 3:
            if parts[0].isdigit():
                d, mo, y = parts
            else:
                mo, d, y = parts
            return date(int(y), MONTHS[mo[:4] if mo[:4] in MONTHS else mo[:3]], int(d)).isoformat()
    except (ValueError, KeyError):
        return None
    return None


def parse_number(num: str, unit: str | None = None) -> float | None:
    try:
        v = float(num.replace(",", ""))
    except ValueError:
        return None
    mult = {"lakh": 1e5, "lakhs": 1e5, "crore": 1e7, "crores": 1e7, "million": 1e6, "bn": 1e9, "billion": 1e9, "k": 1e3}
    return v * mult.get((unit or "").lower(), 1)


def duration(n: str, unit: str) -> tuple[int, str] | None:
    k = int(n) if n.isdigit() else WORDNUM.get(n.lower())
    if k is None:
        return None
    unit = unit.lower().rstrip("s")
    return k, unit


def norm_text(s: str) -> str:
    """For quote checks and claim matching: lower case, one space, plain quotes and dashes, no thousands commas."""
    s = s.lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"').replace("–", "-").replace("—", "-")
    s = re.sub(r"(?<=\d),(?=\d)", "", s)
    return re.sub(r"\s+", " ", s).strip()


# ------------------------------------------------------------------ document kind

def kind_of(name: str, text: str) -> tuple[str, dict]:
    t = (name + "\n" + text[:20000]).lower()
    scores = {k: sum(1 for p in ps if re.search(p, t)) for k, ps in KIND_WORDS.items()}
    if re.search(r"\b(master services|service level|services|non-disclosure|lease|employment|logistics)? ?agreement\b", t[:600]):
        scores["contract"] += 3
    if re.search(r"\btax invoice\b|\binvoice (?:no|number)", t[:800]):
        scores["invoice"] += 3
    if re.search(r"\b(minutes|meeting)\b", t[:300]) and "attendees" in t:
        scores["meeting"] += 3
    best = max(scores, key=scores.get)
    kind = best if scores[best] >= 3 else "other"
    if kind == "other" and os.environ.get("CL_FACTS_ZERO_SHOT") == "1":
        kind = _zero_shot(text) or "other"
    return kind, scores


def _zero_shot(text: str) -> str | None:
    """Optional, local: GLiNER looks for kind-specific things in the first part of the document."""
    try:
        from graph.nlp_extract import gliner
        labels = {"contract party": "contract", "invoice number": "invoice", "action item": "meeting",
                  "policy rule": "policy"}
        found = [labels[e["label"]] for e in gliner().predict_entities(text[:2000], list(labels), threshold=0.5)]
        return max(set(found), key=found.count) if found else None
    except Exception:                                      # noqa: BLE001 - optional
        return None


# ------------------------------------------------------------------ extractors

def _f(kind, field_, value, norm, chunk, m, group=0, conf=0.9, record="", extra=None) -> Fact:
    s, e = m.span(group)
    return Fact(kind, record, field_, value, norm, chunk["chunk_id"], chunk["text"][s:e], s, e, "rules", conf, extra or {})


def contract(chunk: dict) -> list[Fact]:
    t, out = chunk["text"], []
    m = re.search(r"between\s+(?P<a>[A-Z][\w&.,'\- ]{2,80}?)\s*(?:\((?:the\s+)?[\"“]?\w+[\"”]?\))?\s+and\s+"
                  r"(?P<b>[A-Z][\w&.,'\- ]{2,80}?)\s*(?:\((?:the\s+)?[\"“]?\w+[\"”]?\))?(?=[.;,\n])", t)
    if m:
        a, b = m["a"].strip(" ,."), m["b"].strip(" ,.")
        out += [_f("contract", "parties", f"{a}; {b}", [a, b], chunk, m),
                _f("contract", "party_1", a, a, chunk, m, "a"), _f("contract", "party_2", b, b, chunk, m, "b")]
    m = re.search(rf"(?:made|entered into|dated|effective(?: as of| from)?|commences? on)\s+(?:on\s+|as of\s+)?(?P<d>{DATE})", t, re.I)
    if m and parse_date(m["d"]):
        out.append(_f("contract", "effective_date", m["d"], parse_date(m["d"]), chunk, m, "d"))
    m = re.search(r"(?:runs for|term of|period of|for a term of|valid for)\s+(?P<n>" + NUMW +
                  r")\s*(?:\(\d+\)\s*)?(?P<u>months?|years?)", t, re.I)
    if m and duration(m["n"], m["u"]):
        n, u = duration(m["n"], m["u"])
        out.append(_f("contract", "term_months", m.group(0), n * (12 if u == "year" else 1), chunk, m))
    m = re.search(r"\b(?P<n>" + NUMW + r")\s*(?:\(\d+\)\s*)?days?['’]?\s+(?:prior\s+)?(?:written\s+)?notice", t, re.I)
    if m and duration(m["n"], "days"):
        out.append(_f("contract", "notice_days", m.group(0), duration(m["n"], "days")[0], chunk, m))
    m = re.search(rf"(?:annual|yearly|monthly)?\s*fee\s+of\s+{MONEY}", t, re.I)
    if m and parse_number(m["num"], m["unit"]):
        per = re.search(r"\b(annual|yearly|monthly)\b", m.group(0), re.I)
        fee = parse_number(m["num"], m["unit"]) * (12 if per and per[1].lower() == "monthly" else 1)
        out.append(_f("contract", "annual_fee", m.group(0).strip(), fee, chunk, m,
                      extra={"currency": CUR.get((m["cur"] or "").lower(), m["cur"] or "")}))
        if m["cur"]:
            out.append(_f("contract", "currency", m["cur"], CUR.get(m["cur"].lower(), m["cur"]), chunk, m, "cur", 0.85))
    m = re.search(r"within\s+(?P<n>" + NUMW + r")\s*(?:\(\d+\)\s*)?days\s+(?:of|from|after)\s+(?:receipt|the invoice|invoice|the date)",
                  t, re.I)
    if m and duration(m["n"], "days"):
        out.append(_f("contract", "payment_terms_days", m.group(0), duration(m["n"], "days")[0], chunk, m))
    m = re.search(r"governed by(?: and construed in accordance with)? the laws? of\s+(?:the\s+)?(?P<l>[A-Z][\w ]{1,40}?)(?=[,.;\n]| and )", t)
    if m:
        out.append(_f("contract", "governing_law", m["l"].strip(), m["l"].strip(), chunk, m, "l"))
    m = re.search(r"renews? automatically|automatically renew", t, re.I)
    if m:
        out.append(_f("contract", "auto_renew", m.group(0), True, chunk, m, conf=0.85))
    m = re.search(r"liable for all losses|unlimited liability|liability shall be unlimited|uncapped|"
                  r"liability[^.]{0,60}shall not exceed[^.]{0,80}", t, re.I)
    if m:
        capped = "not exceed" in m.group(0).lower()
        out.append(_f("contract", "liability", m.group(0), "capped" if capped else "uncapped", chunk, m, conf=0.8))
    return out


def invoice(chunk: dict) -> list[Fact]:
    t, out = chunk["text"], []
    m = re.search(r"invoice\s*(?:no\.?|number|#)\s*[:\-]?\s*(?P<v>[A-Z0-9][\w\-/]{2,30})", t, re.I)
    if m:
        out.append(_f("invoice", "invoice_number", m["v"], m["v"], chunk, m, "v"))
    m = re.search(rf"(?:invoice\s+)?date\s*[:\-]\s*(?P<d>{DATE})", t, re.I)
    if m and parse_date(m["d"]):
        out.append(_f("invoice", "invoice_date", m["d"], parse_date(m["d"]), chunk, m, "d"))
    m = re.search(r"(?:bill(?:ed)? from|from|vendor|supplier)\s*:\s*(?P<v>[A-Za-z0-9][^\n]{2,80})", t, re.I)
    if m:
        out.append(_f("invoice", "vendor", m["v"].strip(), m["v"].strip(), chunk, m, "v"))
    m = re.search(rf"(?:amount|subtotal|total)(?:\s*\(excl\.?\s*(?:gst|vat|tax)\))?\s*[:\-]\s*{MONEY}", t, re.I)
    if m and parse_number(m["num"], m["unit"]):
        out.append(_f("invoice", "amount", m.group(0), parse_number(m["num"], m["unit"]), chunk, m,
                      extra={"currency": CUR.get((m["cur"] or "").lower(), m["cur"] or "")}))
        if m["cur"]:
            out.append(_f("invoice", "currency", m["cur"], CUR.get(m["cur"].lower(), m["cur"]), chunk, m, "cur", 0.85))
    m = re.search(rf"(?:gst|vat|tax)(?:\s*\d+%)?\s*[:\-]\s*{MONEY}", t, re.I)
    if m and parse_number(m["num"], m["unit"]):
        out.append(_f("invoice", "tax", m.group(0), parse_number(m["num"], m["unit"]), chunk, m))
    return out


def meeting(chunk: dict) -> list[Fact]:
    t, out = chunk["text"], []
    m = re.search(rf"(?:meeting|minutes|review)[^\n]{{0,60}}?[—\-–,:]\s*(?P<d>{DATE})|date\s*:\s*(?P<d2>{DATE})", t, re.I)
    if m:
        g = "d" if m["d"] else "d2"
        if parse_date(m[g]):
            out.append(_f("meeting", "meeting_date", m[g], parse_date(m[g]), chunk, m, g))
    m = re.search(r"attendees\s*:\s*(?P<v>[^\n]+)", t, re.I)
    if m:
        people = [re.sub(r"\s*\(.*?\)", "", p).strip() for p in re.split(r",|;| and ", m["v"]) if p.strip()]
        out.append(_f("meeting", "attendees", m["v"].strip(), people, chunk, m, "v"))
    for i, m in enumerate(re.finditer(r"(?im)^\s*(?:[-*•]\s*)?decision\s*[:\-]\s*(?P<v>.+)$", t), 1):
        out.append(_f("meeting", "decision", m["v"].strip(), m["v"].strip(), chunk, m, "v", record=f"#d{i}"))
    for i, m in enumerate(re.finditer(rf"(?im)^\s*(?:[-*•]\s*)?action(?: item)?\s*[:\-]\s*(?P<v>(?P<o>[A-Z][\w.'\-]+(?: [A-Z][\w.'\-]+)?)"
                                      rf"\s+(?:to|will)\s+(?P<task>.+?)(?:\s+by\s+(?P<due>{DATE}))?)\.?\s*$", t), 1):
        out.append(_f("meeting", "action", m["v"].strip(), {"owner": m["o"], "task": m["task"].strip(),
                                                             "due_date": parse_date(m["due"]) if m["due"] else None},
                      chunk, m, "v", record=f"#a{i}"))
    return out


EXTRACTORS = {"contract": contract, "invoice": invoice, "meeting": meeting}


def extract(kind: str, chunk: dict) -> list[Fact]:
    fn = EXTRACTORS.get(kind)
    return fn(chunk) if fn else []


# ------------------------------------------------------------------ doc card (extractive, no AI model)

_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")


def card(name: str, kind: str, chunks: list[dict], facts: list[dict]) -> dict:
    heads = [h for c in chunks for h in (c.get("headings") or [])]
    first = (chunks[0]["text"] if chunks else "").strip()
    title = heads[0] if heads else (first.splitlines()[0].strip("# ").strip()[:120] if first else name)
    body = " ".join(l.strip() for c in chunks[:3] for l in c["text"].splitlines()
                    if l.strip() and not l.lstrip().startswith("#") and len(l.split()) > 4)
    sents = [s.strip() for s in _SENT.split(body) if len(s.split()) > 4]
    key = {}
    for f in facts:
        if f["record"] in ("", None) and f["field"] not in key:
            key[f["field"]] = f["norm"] if not isinstance(f["norm"], list) else ", ".join(map(str, f["norm"]))
    return {"name": name, "kind": kind, "title": title, "summary": " ".join(sents[:2])[:500],
            "sections": list(dict.fromkeys(heads))[:12], "key_facts": key,
            "decisions": [f["value"] for f in facts if f["field"] == "decision"][:10],
            "actions": [f["norm"] for f in facts if f["field"] == "action"][:10]}
