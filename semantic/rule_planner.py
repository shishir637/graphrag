"""Turn a plain-English data question into a SemanticQuery with rules only (no language model).

Agents that connect over MCP normally fill in the structured request themselves (`query_metrics` takes
metric and dimension names). This planner serves people typing in the Playground, and agents that send
a sentence instead of names. It uses only the reviewed layer:

  metrics     every metric label, name and synonym is a "term"; a term matches when all its words appear in
              the question (any order, plural/verb forms and typos tolerated); the most specific terms win
              and several metrics can be asked for at once
  dimensions  "by X", "per X", "for each X", "X-wise", "which X", "top N X", and time grains ("monthly");
              when a word fits several tables ("channel", "product"), the one that can be joined to the
              metric's own table is used
  filters     known values of the category columns (value lists), value synonyms ("Bangalore"), typos;
              again the column that can be joined to the metric wins
  dates       semantic/dates.py (fiscal years, "last quarter", "since 1 September 2026" ...)
  ranking     "top 3", "most", "lowest", "which region had the fewest"
  splits      a metric that cannot be split by a dimension (fan-out) is swapped for a metric of the same
              kind counted on that dimension's table (net revenue by category -> product revenue)
  refusals    a quantity the layer does not define ("margin", "attrition rate", "headcount of the sales
              team") is refused with the list of what exists, instead of answered with a near miss
"""
import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from .compiler import CompileError, compile_query
from .schema import Filter, SemanticQuery

GRAINS = [  # phrase -> grain suffix
    (r"\b(by|per|each)\s+fiscal\s+year\b|\bfiscal[- ]year[- ]wise\b|\bby\s+fy\b", "fiscal_year"),
    (r"\b(by|per|each)\s+fiscal\s+quarter\b", "fiscal_quarter"),
    (r"\bmonthly\b|\b(by|per|each|every)\s+month\b|\bmonth[- ]wise\b|\bmonth by month\b|\bmonth over month\b", "month"),
    (r"\bquarterly\b|\b(by|per|each|every)\s+quarter\b|\bquarter[- ]wise\b", "quarter"),
    (r"\byearly\b|\bannual(ly)?\b|\b(by|per|each|every)\s+year\b|\byear[- ]wise\b|\byear over year\b", "year"),
    (r"\bweekly\b|\b(by|per|each|every)\s+week\b", "week"),
    (r"\bdaily\b|\b(by|per|each|every)\s+day\b", "day"),
]
STOP = set("the a an of in for by per on to and or with what is was were are how many much our we did do does "
           "show me give list all which who where when this that from since between there it its have has had "
           "get got be been than then please tell".split())
AGG_WORDS = {"total", "sum", "average", "number", "overall"}
RATIO_WORDS = {"rate", "ratio", "margin"}
# Quantities that, when asked for and not part of the matched metric, mean "not answerable"
QUANTITY = re.compile(r"\b(headcount|head count|salar(y|ies)|payroll|budget|spend(ing)?|costs?|expenses?|profit|"
                      r"margin|churn|attrition|retention|inventory|stock levels?|tax(es)?|market share|"
                      r"nps|satisfaction|rate|ratio|percentage|growth)\b", re.I)
# A metric word used as a modifier ("sales team", "revenue manager") is not a request for that metric
MODIFIER_NOUNS = r"(team|teams|person|people|rep|reps|manager|managers|staff|department|target|targets|budget|forecast)"
WORD = re.compile(r"[A-Za-z][A-Za-z0-9&'.]*|\d+")
TIME_WORDS = {"last", "this", "next", "during", "has", "had", "since", "before", "after", "year", "month", "quarter",
              "fy", "fiscal", "today", "yesterday"}


def singular(w: str) -> str:
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith(("sses", "ches", "shes", "xes")):
        return w[:-2]
    if w.endswith("s") and not w.endswith(("ss", "us", "is")) and len(w) > 3:
        return w[:-1]
    return w


def stem(w: str) -> str:
    w = singular(w.lower().strip("'.-"))
    w = {"sold": "sell", "sale": "sell", "bought": "buy", "hired": "hire", "opened": "open", "issued": "issue",
         "invoiced": "invoice", "billed": "invoice", "bill": "invoice",
         "averaged": "average", "avg": "average", "mean": "average", "typical": "average",
         "no": "number", "count": "number", "quantity": "number", "amt": "amount",
         # generic business wording: same measure, different words
         "value": "amount", "worth": "amount", "time": "hour", "duration": "hour", "hr": "hour",
         "share": "rate", "percentage": "rate", "proportion": "rate", "percent": "rate", "fraction": "rate",
         "client": "customer", "staff": "employee", "headcount": "employee"}.get(w, w)
    for suf in ("ing", "ed"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            return w[: -len(suf)]
    return w


@dataclass
class Plan:
    answerable: bool
    query: SemanticQuery | None = None
    assumptions: list[str] = field(default_factory=list)
    reason_if_not_answerable: str = ""
    matched: dict = field(default_factory=dict)


class RulePlanner:
    def __init__(self, model, metrics, enrichment, values: dict[str, list[str]]):
        self.model, self.metrics = model, metrics
        self.values = values
        self.approved = [m for m in metrics.metrics if m.status == "approved"] or list(metrics.metrics)
        approved_names = {m.name for m in self.approved}
        self.metric_terms: dict[str, str] = {}                      # term -> metric
        for m in self.approved:
            for t in {m.name.replace("_", " "), m.label}:
                self.metric_terms[t.lower()] = m.name
        self.dim_terms: dict[str, list[str]] = {}                   # term -> [entity.dimension]
        self.value_terms: dict[str, list[tuple[str, str]]] = {}     # term -> [(entity.dimension, value)]
        for e in model.entities:
            for d in e.dimensions:
                ref = f"{e.name}.{d.name}"
                if d.type == "key":
                    continue
                names = {d.name.replace("_", " "), (d.label or "").lower()}
                if d.name.endswith("_name") or d.name == "name":
                    base = e.name.split("__")[-1]
                    names |= {d.name[:-5].replace("_", " ") if d.name != "name" else "", base.replace("_", " "),
                              singular(base).replace("_", " "), *(base.split("_")[-1:]), singular(base.split("_")[-1])}
                for t in names:
                    if t:
                        self.dim_terms.setdefault(t.lower(), []).append(ref)
        for ref, vals in values.items():
            for v in vals:
                if v and len(str(v)) >= 2:
                    self.value_terms.setdefault(str(v).lower(), []).append((ref, str(v)))
        for s in enrichment.synonyms:
            t = s.term.lower()
            if "=" in s.target:
                ref, v = s.target.split("=", 1)
                self.value_terms.setdefault(t, []).insert(0, (ref, v))
            elif "." in s.target:
                self.dim_terms.setdefault(t, []).insert(0, s.target)
            elif s.target in approved_names:
                self.metric_terms[t] = s.target
        self.term_words = {t: [stem(w) for w in WORD.findall(t) if w.lower() not in STOP] for t in self.metric_terms}
        self.time_dims = {}
        for e in model.entities:
            for d in e.dimensions:
                if d.type == "time":
                    self.time_dims.setdefault(e.name, d.name)

    # ---------------------------------------------------------------- helpers
    def _compiles(self, metrics: list[str], dims: list[str], filters: list[str] = ()) -> bool:
        try:
            compile_query(SemanticQuery(metrics=metrics, dimensions=dims,
                                        filters=[Filter(dimension=f, values=["x"]) for f in filters]),
                          self.model, self.metrics)
            return True
        except (CompileError, KeyError, ValueError):
            return False

    def _pick_ref(self, refs: list[str], metrics: list[str], dims: list[str], as_filter: bool = False) -> str | None:
        """Among columns sharing a word, the one on a metric's own table, else the first that joins."""
        own = {self.metrics.get(m).entity for m in metrics}
        ordered = sorted(dict.fromkeys(refs), key=lambda r: r.split(".")[0] not in own)
        for r in ordered:
            ok = self._compiles(metrics, dims, [r]) if as_filter else self._compiles(metrics, dims + [r])
            if ok:
                return r
        return ordered[0] if ordered else None      # none joins safely: kept, so the metric can be swapped

    def _place(self, words: list[str], stems: list[str], exclude: set[int]):
        """Positions of a term's words in the question (each word once, not in `exclude`), as compact as possible.
        Returns (positions, how) or None."""
        def occ(w):
            exact = [i for i, s in enumerate(stems) if s == w and i not in exclude]
            if exact or len(w) < 5:
                return exact, "exact"
            return [i for i, s in enumerate(stems) if len(s) >= 5 and i not in exclude and fuzz.ratio(s, w) >= 86], "typo"
        options = [occ(w) for w in words]
        if any(not o[0] for o in options):
            return None
        how = "typo" if any(o[1] == "typo" for o in options) else "exact"
        best = None
        for anchor in options[0][0]:
            pos = [anchor]
            for o, _ in options[1:]:
                free = [i for i in o if i not in pos]
                if not free:
                    break
                pos.append(min(free, key=lambda i: abs(i - anchor)))
            else:
                spread = max(pos) - min(pos) + 1
                if best is None or spread < best[1]:
                    best = (pos, spread)
        if best is None or best[1] > len(words) + 3:
            return None
        return best[0], how

    def _match_metrics(self, stems: list[str]) -> list[tuple]:
        """Terms whose every word is in the question (any order, close together). Best first, non-overlapping."""
        cands = []
        for term, words in self.term_words.items():
            placed = self._place(words, stems, set()) if words else None
            if placed:
                content = sum(w not in AGG_WORDS for w in words)
                spread = max(placed[0]) - min(placed[0]) + 1
                ratio = 1.0 if set(words) & RATIO_WORDS else 0.0          # "rate" is the most specific word there is
                cands.append((len(words) + 0.5 * content + ratio - 0.05 * (spread - len(words)), term, words))
        cands.sort(key=lambda c: -c[0])
        chosen, used = [], set()
        for sc, term, words in cands:
            metric = self.metric_terms[term]
            if any(x[3] == metric for x in chosen):
                continue
            placed = self._place(words, stems, used)
            if not placed:
                continue
            chosen.append((sc, placed[0], term, metric, placed[1]))
            used |= set(placed[0])
        return sorted(chosen, key=lambda c: min(c[1]))

    def _count_metric_for(self, stems: list[str]):
        """'How many <things>': the one approved count metric on the table those things live in."""
        counts = {}
        for m in self.approved:
            if re.match(r"^count\(", (m.expression or "").strip(), re.I):
                counts.setdefault(m.entity, []).append(m)
        for i, s_ in enumerate(stems):
            for ent, ms in counts.items():
                base = stem(ent.split("__")[-1].split("_")[-1])
                if s_ == base and len(ms) == 1:
                    return i, f"{ent} count", ms[0].name
        return None

    def _grain(self, q: str):
        for pat, g in GRAINS:
            if re.search(pat, q, re.I):
                return g
        return None

    def _find_values(self, low: list[str], skip: set[int]):
        """Longest word n-grams that are known column values (exact, then typo-tolerant for 5+ letters)."""
        hits, used = [], set(skip)
        for n in range(min(4, len(low)), 0, -1):
            for i in range(len(low) - n + 1):
                span = set(range(i, i + n))
                if span & used:
                    continue
                gram = " ".join(low[i:i + n])
                if gram in STOP or len(gram) < 2:
                    continue
                refs, how = self.value_terms.get(gram), "exact"
                if refs is None and len(gram) >= 5:
                    best = max(((t, fuzz.ratio(gram, t)) for t in self.value_terms if abs(len(t) - len(gram)) <= 2 and len(t) >= 5),
                               key=lambda x: x[1], default=(None, 0))
                    if best[1] >= 86:
                        refs, how = self.value_terms[best[0]], f"typo:{best[0]}"
                if refs:
                    used |= span
                    hits.append((i, gram, refs, how))
        return sorted(hits)

    def _dim_candidates(self, phrase: str) -> list[str]:
        phrase = phrase.strip()
        if not phrase or phrase in STOP:
            return []
        words = phrase.split()
        variants = [phrase, " ".join(singular(w) for w in words)]
        variants += [" ".join(words[-k:]) for k in range(len(words) - 1, 0, -1)]      # head noun last: "customer region"
        variants += [" ".join(singular(w) for w in words[-k:]) for k in range(len(words) - 1, 0, -1)]
        variants += [" ".join(words[:k]) for k in range(len(words) - 1, 0, -1)]
        for v in variants:
            if v in self.dim_terms:
                return self.dim_terms[v]
        for v in variants[:2]:
            if len(v) >= 5:
                best = max(((t, fuzz.ratio(v, t)) for t in self.dim_terms if len(t) >= 5), key=lambda x: x[1], default=(None, 0))
                if best[1] >= 86:
                    return self.dim_terms[best[0]]
        return []

    # ---------------------------------------------------------------- planning
    def plan(self, question: str, today) -> Plan:
        from .dates import resolve
        q = question.strip()
        ql = q.lower()
        toks = WORD.findall(q)
        low = [t.lower() for t in toks]
        stems = [stem(t) for t in toks]
        assumptions: list[str] = []

        # 1. grouping phrases: positions right after by/per/each..., "which X", "top N X", "X-wise"
        dim_spans: list[list[int]] = []
        superlative_spans: list[list[int]] = []
        for i, t in enumerate(low):
            lead = None
            if t in ("by", "per", "each", "every", "across", "which"):
                lead = i + 1
            elif t == "top" and i + 2 < len(low) and low[i + 1].isdigit():
                lead = i + 2
            elif t in ("best", "worst", "lowest", "highest"):
                lead = i + 1 + (i + 1 < len(low) and low[i + 1] in ("selling", "performing"))
            if lead is not None and lead < len(low):
                if low[lead].isdigit():
                    lead += 1
                span = list(range(lead, min(lead + 3, len(low))))
                cut = next((k for k, j in enumerate(span) if low[j] in STOP | TIME_WORDS or low[j].isdigit()), len(span))
                if span[:cut]:
                    dim_spans.append(span[:cut])
                    if t in ("best", "worst", "lowest", "highest"):
                        superlative_spans.append(span[:cut])
        for m in re.finditer(r"\b([a-z]+)[- ]wise\b", ql):
            if m.group(1) in low:
                dim_spans.append([low.index(m.group(1))])

        # 2. metrics
        hits = self._match_metrics(stems)
        hits = [h for h in hits if len(h[1]) > 1 or not re.search(rf"\b{re.escape(low[h[1][0]])}\s+{MODIFIER_NOUNS}\b", ql)]
        # a metric word inside a grouping phrase is a dimension if it resolves to one ("employees in each department")
        in_dim = {j for sp in dim_spans if self._dim_candidates(" ".join(low[j] for j in sp)) for j in sp}
        hits = [h for h in hits if not set(h[1]) <= in_dim] or hits[:1]
        if not hits and re.search(r"\b(how many|number of|count of|count)\b", ql):
            counted = self._count_metric_for(stems)
            if counted:
                hits = [(1.0, [counted[0]], counted[1], counted[2], "count")]
        metric_names = [h[3] for h in hits]
        if not metric_names:
            return Plan(False, reason_if_not_answerable="No approved metric matches this question. Available: "
                        + ", ".join(m.label for m in self.approved))
        covered = {stems[j] for h in hits for j in h[1]}
        value_words = {w for _, gram, _, _ in self._find_values(low, {j for h in hits for j in h[1]}) for w in gram.split()}
        for qm in QUANTITY.finditer(ql):
            if qm.group(0) in value_words:          # "Growth" is a tier here, not a growth rate
                continue
            if stem(qm.group(0).split()[-1]) not in covered:
                return Plan(False, reason_if_not_answerable=f"'{qm.group(0)}' is not a defined metric. Available: "
                            + ", ".join(m.label for m in self.approved))
        for h in hits:
            if h[4] == "typo":
                assumptions.append(f"read the question as '{h[2]}'")
        metric_pos = {j for h in hits for j in h[1]}

        # 3. dimensions
        dims: list[str] = []
        verbatim = {j for h in hits for j in range(min(h[1]), max(h[1]) + 1) if h[2] in ql}
        for span in dim_spans:
            if span in superlative_spans and set(span) & metric_pos:     # "lowest average CSAT": the metric
                continue
            if set(span) <= metric_pos and set(span) <= verbatim:       # "revenue per customer" is one metric name
                continue
            refs = self._dim_candidates(" ".join(low[j] for j in span))
            if refs:
                ref = self._pick_ref(refs, metric_names, dims)
                if ref and ref not in dims:
                    dims.append(ref)
        grain = self._grain(ql)
        if grain:
            tc = (self.metrics.get(metric_names[0]).time_column or "").split(".")
            base = self.metrics.get(metric_names[0]).entity
            ent, col = (tc if len(tc) == 2 else (base, self.time_dims.get(base)))
            tdim = next((d.name for d in self.model.entity(ent).dimensions if d.column == col and d.type == "time"),
                        self.time_dims.get(ent))
            if tdim:
                dims = [d for d in dims if self.model.entity(d.split(".")[0]).dimension(d.split(".")[1]).type != "time"]
                dims.append(f"{ent}.{tdim}__{grain}")

        # 4. filters from values (a value inside a grouping phrase is skipped)
        dim_pos = {j for sp in dim_spans for j in sp}
        filters: dict[str, list[str]] = {}
        for i, gram, refs, how in self._find_values(low, metric_pos):
            choices = [r for r, _ in refs]
            if i in dim_pos and any(r in dims for r in choices):
                continue
            ref = self._pick_ref(choices, metric_names, dims, as_filter=True)
            if ref is None:
                continue
            val = next(v for r, v in refs if r == ref)
            filters.setdefault(ref, [])
            if val not in filters[ref]:
                filters[ref].append(val)
            if gram != val.lower():
                assumptions.append(f"'{gram}' means {ref.split('.')[-1]} = {val}")

        for mt in re.finditer(r"\b([a-z]+)\s+(team|department|division|unit|group)\b", ql):
            phrase = mt.group(0)
            if mt.group(1) in STOP | {"each", "every", "per", "by", "which", "my", "your", "their", "same", "whole"} \
                    or any(mt.group(2) in d for d in dims):
                continue
            if not any(phrase.startswith(v.lower()) or v.lower() in phrase for vals in filters.values() for v in vals) \
                    and not any(mt.group(1) in d for d in dims):
                return Plan(False, reason_if_not_answerable=f"could not find '{phrase}' in the data, so the number "
                            "cannot be limited to it")

        # 5. dates
        hints = resolve(q, today)
        start = end = None
        if hints:
            start, end = hints[0]["start_date"], hints[0]["end_date"]
            if len(hints) > 1:
                assumptions.append(f"several periods mentioned; used \"{hints[0]['phrase']}\"")

        # 6. ranking
        order_by, limit = [], None
        m_top = re.search(r"\b(?:top|best|which|first|bottom)\s+(\d+)\b", ql) or re.search(r"\b(\d+)\s+(?:best|top)\b", ql)
        asc = re.search(r"\b(lowest|least|fewest|smallest|bottom|worst|minimum)\b", ql)
        desc = re.search(r"\b(top|most|highest|largest|biggest|best|maximum|leading)\b", ql)
        if dims and (m_top or asc or desc):
            order_by = [("" if asc else "-") + metric_names[0]]
            if m_top:
                limit = int(m_top.group(1))
            elif re.search(r"\b(which|what)\b", ql):
                limit = 1
            elif any(low[sp[-1]] == singular(low[sp[-1]]) and self._dim_candidates(" ".join(low[j] for j in sp))
                     for sp in superlative_spans):
                limit = 1                                  # "lowest-rated product": the one product

        query = SemanticQuery(metrics=metric_names, dimensions=dims,
                              filters=[Filter(dimension=k, op="=" if len(v) == 1 else "in", values=v) for k, v in filters.items()],
                              start_date=start, end_date=end, order_by=order_by, limit=limit)
        query, notes = self._make_compilable(query)
        assumptions += notes
        if query is None:
            return Plan(False, reason_if_not_answerable="; ".join(notes) or "the request does not compile")
        return Plan(True, query, assumptions, matched={"metrics": [(h[2], h[3]) for h in hits], "dimensions": dims,
                                                      "values": list(filters)})

    def _make_compilable(self, q: SemanticQuery):
        """Fix fan-out splits by swapping a metric for one of the same kind counted on a joinable table."""
        try:
            compile_query(q, self.model, self.metrics)
            return q, []
        except CompileError as e:
            err = str(e)
        notes, mapping = [], {}
        for name in q.metrics:
            m = self.metrics.get(name)
            content = {stem(w) for w in WORD.findall(m.label) if w.lower() not in STOP} - AGG_WORDS - {"net", "gross"}
            options = [x for x in self.approved if x.name != name
                       and content & ({stem(w) for w in WORD.findall(x.label) if w.lower() not in STOP} - AGG_WORDS)]
            for alt in options:
                trial = q.model_copy(update={"metrics": [alt.name if n == name else n for n in q.metrics]})
                try:
                    compile_query(trial, self.model, self.metrics)
                    mapping[name] = alt.name
                    notes.append(f"{m.label} cannot be split that way; used {alt.label} instead")
                    break
                except CompileError:
                    continue
        if not mapping:
            return None, [f"cannot answer: {err}"]
        q2 = q.model_copy(update={"metrics": [mapping.get(n, n) for n in q.metrics],
                                  "order_by": [("-" if o.startswith("-") else "") + mapping.get(o.lstrip("-"), o.lstrip("-"))
                                               for o in q.order_by]})
        try:
            compile_query(q2, self.model, self.metrics)
            return q2, notes
        except CompileError as e2:
            return None, [f"cannot answer: {e2}"]


_cache: dict = {}


def planner() -> RulePlanner:
    from .answer import _layer_version
    from .enrich import load_store
    from .yamlio import load_enrichment, load_metrics, load_model
    v = _layer_version()
    if _cache.get("v") != v:
        try:
            store = load_store()
        except SystemExit:                       # catalog not built yet (fresh install): build it, no model needed
            from .enrich import build_enrichment
            build_enrichment(embed=False)
            store = load_store()
        _cache.update(v=v, p=RulePlanner(load_model(), load_metrics(), load_enrichment(), store["values"]))
    return _cache["p"]
