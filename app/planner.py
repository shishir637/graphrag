"""The question planner: one rule-based pass over a question, before any evidence is gathered (no AI model).

plan_question(question, caps) decides, in order:
  1. answerable?  signals (a defined metric, a graph entity, a kind of fact the workspace has read, how much of the
                  question's vocabulary the workspace knows, capitalised names it has never seen); when not
                  answerable: the reason and clarifying questions, and nothing is guessed
  2. route        data | overview | connections | documents (which evidence is gathered first)
  3. split        a mixed question ("revenue last quarter and the leave policy") becomes parts with their own routes
  4. secondary    cheap extra sources next to the primary one (the evidence plan): data + explanation words also
                  get documents; documents / connections naming a defined metric also get the metric result
The metric planner (semantic/rule_planner.py) runs once per question and its plan is shared by every step.

app/router.py (route, plan) and app/answerable.py (check, split_parts, vocabulary, clarify) are thin wrappers over
this module for their existing callers.

Routes:
  data         a defined business metric is asked for (metric names, labels, synonyms; see semantic/rule_planner)
               or a count/total over known data values ("how many customers in Chennai")
  overview     a big-picture question ("overview", "main themes", "summarise", "across all")
  connections  two or more known graph entities, or relational wording ("how is X related to Y", "between")
  documents    everything else: a fact to look up in the documents
Callers can always force a route with mode=documents|metrics|graph.
"""
import json  # noqa: F401 - kept for callers that relied on it
import re

from rapidfuzz import fuzz, process

from rag.wspath import PerFile

OVERVIEW = re.compile(r"\b(overview|summar(y|ise|ize)|main (themes?|topics?|points?)|big picture|in general|"
                      r"across (all|the)|key (themes?|trends?)|what are the (main|major|key)|landscape|outline)\b", re.I)
CONNECT = re.compile(r"\b(related|relationship|connected|connection|link(ed)?|between|in common|both|compare|"
                     r"comparison|differ(ence|s)?|versus|vs\.?|same as|which of)\b", re.I)
EXPLAIN = re.compile(r"\b(why|reason|reasons|explain|explanation|cause[sd]?|because|what (drove|caused|happened)|"
                     r"how (is|are|was|were) .{1,60} (calculated|defined|measured|counted)|polic(y|ies)|definition|"
                     r"rules?)\b", re.I)
COUNTING = re.compile(r"\b(how many|how much|number of|count of|count|total|sum of|average|avg|mean|median|"
                      r"top \d+|rank(ed|ing)?|trend|per (month|quarter|year)|by (month|quarter|year|region|city|channel))\b", re.I)


_NO_PLAN = object()


def _metric_plan(q: str, today=None):
    """The metric planner's plan for the question (None when there is no data layer). Computed once per question."""
    from datetime import date
    try:
        from semantic.rule_planner import planner
        return planner().plan(q, today or date.today())
    except Exception:                                          # noqa: BLE001 - no data layer
        return None


def route(question: str, today=None, _mp=_NO_PLAN) -> tuple[str, str]:
    """Returns (route, reason)."""
    q = question.strip()
    # 1. metrics
    try:
        p = _metric_plan(q, today) if _mp is _NO_PLAN else _mp
        if p is None:
            raise LookupError("no data layer")
        terms = [t for t, _ in p.matched.get("metrics", [])] if p.answerable else []
        # one bare noun ("department", "tickets") is not enough: it needs counting words or a fuller metric name
        strong = any(len(t.split()) >= 2 for t in terms) or COUNTING.search(q) or (
            p.query and (p.query.dimensions or p.query.filters or p.query.start_date))
        hm = re.search(r"\bhow many((?:\s+[\w-]+){1,4})", q, re.I)
        if hm and p.answerable:                     # "how many days of leave": the counted thing must be the metric's
            from semantic.rule_planner import stem
            counted = {stem(w) for t in terms for w in t.split()}
            if not {stem(w) for w in hm.group(1).split()} & counted:
                strong = False
        if p.answerable and strong and not re.search(r"\b(who|whom|whose|why)\b", q, re.I):
            return "data", "names a defined metric: " + ", ".join(p.query.metrics)
    except Exception:                                          # noqa: BLE001 - no data layer: fall through
        pass
    # 2. overview
    if OVERVIEW.search(q):
        return "overview", "asks for a big-picture summary"
    # 3. connections
    try:
        from graph import search as GS
        if GS.ready():
            ents = GS.link(q)
            if len(ents) >= 2:
                return "connections", "names " + " and ".join(e["name"] for e in ents[:3])
            if ents and CONNECT.search(q):
                return "connections", "asks how things connect"
    except Exception:                                          # noqa: BLE001
        pass
    return "documents", "a fact to look up"


def _names_metric(q: str, today=None, _mp=_NO_PLAN) -> bool:
    """The question names a defined metric (fuller name or counting words), whatever its first word."""
    p = _metric_plan(q, today) if _mp is _NO_PLAN else _mp
    if p is None or not p.answerable:
        return False
    terms = [t for t, _ in p.matched.get("metrics", [])]
    return any(len(t.split()) >= 2 for t in terms) or bool(COUNTING.search(q))


def plan(question: str, today=None, _mp=_NO_PLAN) -> tuple[str, list[str], str]:
    """(primary route, secondary sources, reason). Secondary sources are gathered too when the caller may use them."""
    q = question.strip()
    mp = _metric_plan(q, today) if _mp is _NO_PLAN else _mp
    primary, reason = route(q, today, mp)
    extra: list[str] = []
    if primary == "data" and EXPLAIN.search(q):
        extra.append("documents")
    elif primary in ("documents", "connections") and _names_metric(q, today, mp):
        extra.append("data")
    return primary, extra, reason


# ------------------------------------------------------------------ answerable?

STOP = set("""a an the of in on at to for and or is are was were be been by with from as that this these those which
what who whom whose how when where why do does did has have had it its their there than then into about between
me my our we you your i please tell show give list find get can could would should will shall may might must
much many any all some each per vs versus also both last next this year month week day days""".split())
FACT_WORDS = {"contract": r"\b(contract|agreement|party|parties|notice|term|renew|governing law|jurisdiction|"
                          r"liabilit|annual fee|payment terms|effective date)",
              "invoice": r"\b(invoice|vendor|gst|vat|tax|bill)",
              "meeting": r"\b(meeting|decision|decided|action item|attendee|due date|follow[- ]?up)"}
_VOCAB = PerFile(32)
COVERAGE_MIN = 0.5


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z][a-z0-9\-']{2,}", text.lower()) if w not in STOP]


def vocabulary() -> set[str]:
    """Words of the workspace's passages, metric names and labels, synonyms and known values (cached per build)."""
    from rag import passages
    from rag.config import OUT_DIR
    path = OUT_DIR / "_vocabulary"                       # cache key per workspace (no file)
    m = passages.stamp()
    from semantic.answer import _layer_version
    try:
        lv = _layer_version()
    except Exception:                                      # noqa: BLE001
        lv = None
    e = _VOCAB.entry(path)
    if e.get("mtime") == (m, lv):
        return e["words"]
    words: set[str] = set()
    if m is not None:
        for c in passages.all():
            words.update(_words(c["text"] + " " + " ".join(c.get("headings") or [])))
    try:
        from semantic.enrich import load_store
        from semantic.yamlio import load_metrics, load_model
        for x in load_metrics().metrics:
            words.update(_words(f"{x.name.replace('_', ' ')} {x.label} {x.description}"))
        for ent in load_model().entities:
            words.update(_words(f"{ent.name.replace('_', ' ')} {ent.label}"))
            for d in ent.dimensions:
                words.update(_words(f"{d.name.replace('_', ' ')} {d.label}"))
        st = load_store()
        for t, tgt in st["synonyms"]:
            words.update(_words(f"{t} {tgt.replace('_', ' ')}"))
        for vals in st["values"].values():
            for v in vals:
                words.update(_words(str(v)))
    except Exception:                                      # noqa: BLE001 - no data layer
        pass
    e.update(mtime=(m, lv), words=words, sorted=sorted(words))
    return words


def _known(w: str, vocab: set[str], sorted_vocab: list[str]) -> bool:
    if w in vocab or w.rstrip("s") in vocab or (w + "s") in vocab:
        return True
    if len(w) < 5:
        return False
    hit = process.extractOne(w, sorted_vocab, scorer=fuzz.ratio, score_cutoff=88)
    return bool(hit)


def split_parts(question: str) -> list[str]:
    """'A and B' -> [A, B] when both halves are real questions with different routes; else [question]."""
    q = question.strip()
    for sep in (r"\s*;\s*", r"\?\s+(?=\w)", r",?\s+and also\s+", r",?\s+and\s+(?=(?:what|who|how|which|when|where|why|is|are|does|do)\b)"):
        bits = [b.strip(" ?") for b in re.split(sep, q, maxsplit=1, flags=re.I)]
        if len(bits) == 2 and all(len(b.split()) >= 3 for b in bits):
            if route(bits[0])[0] != route(bits[1])[0]:
                return [b + "?" for b in bits]
    return [q]


def check(question: str, caps: list[str] | None = None, _mp=_NO_PLAN) -> dict:
    q = question.strip()
    caps = set(caps or ["documents", "data", "graph"])
    parts = split_parts(q)
    signals = {"metric": False, "entity": [], "facts": [], "coverage": 0.0, "unknown": []}
    if "data" in caps:
        signals["metric"] = _names_metric(q, None, _mp)
    try:
        from graph import search as GS
        if GS.ready() and caps & {"graph", "documents"}:
            signals["entity"] = [e["name"] for e in GS.link(q)][:5]
    except Exception:                                      # noqa: BLE001 - no graph
        pass
    try:
        from facts.store import facts_for
        have = {f["kind"] for f in facts_for(None)}
        signals["facts"] = [k for k, pat in FACT_WORDS.items() if k in have and re.search(pat, q, re.I)]
    except Exception:                                      # noqa: BLE001 - no facts
        pass
    vocab = vocabulary()
    sv = _VOCAB.entry(__import__("rag.config", fromlist=["OUT_DIR"]).OUT_DIR / "_vocabulary").get("sorted") or sorted(vocab)
    words = _words(q)
    if words and vocab:
        known = [w for w in words if _known(w, vocab, sv)]
        signals["coverage"] = round(len(known) / len(words), 2)
    names = [m.group(0) for m in re.finditer(r"(?<!^)(?<![.?!]\s)\b[A-Z][\w\-]{2,}(?:\s+[A-Z][\w\-]{2,})*", q)]   # ids too
    signals["unknown"] = [n for n in names if not all(_known(w, vocab, sv) for w in _words(n))] if vocab else []
    matched = bool(signals["metric"] or signals["entity"] or signals["facts"])
    ok = matched or (signals["coverage"] >= COVERAGE_MIN and not signals["unknown"])
    out = {"answerable": ok, "parts": parts if len(parts) > 1 else [], "signals": signals}
    if not ok:
        if not vocab:
            out["reason"] = "This workspace has no documents or data yet."
        elif signals["unknown"]:
            out["reason"] = f"Nothing in this workspace mentions {', '.join(signals['unknown'][:3])}."
        else:
            out["reason"] = "No metric, document or fact in this workspace covers this question."
        out["clarify"] = clarify(q, sv)
    return out


def plan_question(question: str, caps: list[str] | None = None, today=None) -> dict:
    """The whole plan for one question in one pass: answerable? (signals, reason, clarifying questions), route,
    parts of a mixed question, and secondary sources. The metric planner runs once and is shared."""
    q = question.strip()
    mp = _metric_plan(q, today)
    out = check(q, caps, mp)
    primary, extra, reason = plan(q, today, mp)
    out.update(question=q, route=primary, route_reason=reason, secondary=extra)
    return out


def clarify(question: str, sorted_vocab: list[str]) -> list[str]:
    """Clarifying questions: the closest metrics, and close spellings of unknown words."""
    out = []
    try:
        from semantic.yamlio import load_metrics
        labels = {m.label: m.name for m in load_metrics().metrics if m.status == "approved"}
        for lab, score, _ in process.extract(question, list(labels), scorer=fuzz.token_set_ratio, limit=2):
            if score >= 50:
                out.append(f"Do you mean the metric '{lab}'?")
    except Exception:                                      # noqa: BLE001
        pass
    for w in _words(question):
        if w not in sorted_vocab and len(w) >= 5:
            hit = process.extractOne(w, sorted_vocab, scorer=fuzz.ratio, score_cutoff=75)
            if hit and hit[0] != w:
                out.append(f"Did you mean '{hit[0]}' instead of '{w}'?")
    if not out:
        out.append("Which document, metric or team is this about?")
    return out[:4]
