"""Decide where a question should be looked up - without a language model.

  data         a defined business metric is asked for (metric names, labels, synonyms; see semantic/rule_planner)
               or a count/total over known data values ("how many customers in Chennai")
  overview     a big-picture question ("overview", "main themes", "summarise", "across all")
  connections  two or more known graph entities, or relational wording ("how is X related to Y", "between")
  documents    everything else: a fact to look up in the documents

The route only decides which evidence is gathered first; the calling agent reads the evidence and answers.
Callers can always force a route with mode=documents|metrics|graph.

plan() adds cheap secondary sources to the primary one (the evidence plan), still without a language model:
  data + explanation words ("why", "explain", "how is ... calculated", "policy")  -> also documents
  documents / connections + a question that names a defined metric                 -> also the metric result
so "why did net revenue drop in Kerala last quarter?" gets the number and the passages that explain it.
"""
import re

OVERVIEW = re.compile(r"\b(overview|summar(y|ise|ize)|main (themes?|topics?|points?)|big picture|in general|"
                      r"across (all|the)|key (themes?|trends?)|what are the (main|major|key)|landscape|outline)\b", re.I)
CONNECT = re.compile(r"\b(related|relationship|connected|connection|link(ed)?|between|in common|both|compare|"
                     r"comparison|differ(ence|s)?|versus|vs\.?|same as|which of)\b", re.I)
EXPLAIN = re.compile(r"\b(why|reason|reasons|explain|explanation|cause[sd]?|because|what (drove|caused|happened)|"
                     r"how (is|are|was|were) .{1,60} (calculated|defined|measured|counted)|polic(y|ies)|definition|"
                     r"rules?)\b", re.I)
COUNTING = re.compile(r"\b(how many|how much|number of|count of|count|total|sum of|average|avg|mean|median|"
                      r"top \d+|rank(ed|ing)?|trend|per (month|quarter|year)|by (month|quarter|year|region|city|channel))\b", re.I)


def route(question: str, today=None) -> tuple[str, str]:
    """Returns (route, reason)."""
    from datetime import date
    q = question.strip()
    # 1. metrics
    try:
        from semantic.rule_planner import planner
        p = planner().plan(q, today or date.today())
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


def _names_metric(q: str, today=None) -> bool:
    """The question names a defined metric (fuller name or counting words), whatever its first word."""
    from datetime import date
    try:
        from semantic.rule_planner import planner
        p = planner().plan(q, today or date.today())
    except Exception:                                          # noqa: BLE001 - no data layer
        return False
    if not p.answerable:
        return False
    terms = [t for t, _ in p.matched.get("metrics", [])]
    return any(len(t.split()) >= 2 for t in terms) or bool(COUNTING.search(q))


def plan(question: str, today=None) -> tuple[str, list[str], str]:
    """(primary route, secondary sources, reason). Secondary sources are gathered too when the caller may use them."""
    primary, reason = route(question, today)
    q = question.strip()
    extra: list[str] = []
    if primary == "data" and EXPLAIN.search(q):
        extra.append("documents")
    elif primary in ("documents", "connections") and _names_metric(q, today):
        extra.append("data")
    return primary, extra, reason
