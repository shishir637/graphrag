"""Can this question be answered from what this install knows? One rule-based pass before any evidence is gathered
(no AI model). Ported from dev app/planner.py; routing itself stays in app/router.py.

check(question, caps) -> {"answerable", "parts", "signals", "reason"?, "clarify"?}
  signals   metric      a defined metric is named (the metric planner, with counting words or a fuller name)
            entity      knowledge-graph entities named in the question
            coverage    share of the question's words found in the passages, metric names, labels, synonyms and values
            unknown     capitalised names (and ids) that nothing here mentions
  answerable  a metric or entity matched, or coverage >= 0.5 with no unknown names
  not answerable: the reason (what is not known) and clarifying questions (closest metrics, close spellings)
  parts     a mixed question ("revenue last quarter and the leave policy") split into parts with different routes

Off: CL_ANSWERABLE=0 (engine.ask then routes as before).
"""
import json
import re

from rapidfuzz import fuzz, process

from .router import COUNTING, route

STOP = set("""a an the of in on at to for and or is are was were be been by with from as that this these those which
what who whom whose how when where why do does did has have had it its their there than then into about between
me my our we you your i please tell show give list find get can could would should will shall may might must
much many any all some each per vs versus also both last next this year month week day days""".split())
COVERAGE_MIN = 0.5
_VOCAB: dict = {"key": None, "words": set(), "sorted": []}


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z][a-z0-9\-']{2,}", text.lower()) if w not in STOP]


def _layer_key():
    try:
        from semantic.answer import _layer_version
        return _layer_version()
    except Exception:                                      # noqa: BLE001
        return None


def vocabulary() -> tuple[set[str], list[str]]:
    """Words of the passages, metric names and labels, synonyms and known values (cached per build)."""
    from rag import config
    p = config.OUT_DIR / "2_chunks.json"
    try:
        m = (str(p), p.stat().st_mtime)
    except OSError:
        m = None
    key = (m, _layer_key())
    if _VOCAB["key"] == key:
        return _VOCAB["words"], _VOCAB["sorted"]
    words: set[str] = set()
    if m is not None:
        for c in json.loads(p.read_text(encoding="utf-8")):
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
    _VOCAB.update(key=key, words=words, sorted=sorted(words))
    return words, _VOCAB["sorted"]


def _known(w: str, vocab: set[str], sorted_vocab: list[str]) -> bool:
    if w in vocab or w.rstrip("s") in vocab or (w + "s") in vocab:
        return True
    if len(w) < 5:
        return False
    return bool(process.extractOne(w, sorted_vocab, scorer=fuzz.ratio, score_cutoff=88))


def _names_metric(q: str) -> bool:
    from datetime import date
    try:
        from semantic.rule_planner import planner
        p = planner().plan(q, date.today())
    except Exception:                                      # noqa: BLE001 - no data layer
        return False
    if not p.answerable:
        return False
    terms = [t for t, _ in p.matched.get("metrics", [])]
    return any(len(t.split()) >= 2 for t in terms) or bool(COUNTING.search(q))


def split_parts(question: str) -> list[str]:
    """'A and B' -> [A, B] when both halves are real questions with different routes; else [question]."""
    q = question.strip()
    for sep in (r"\s*;\s*", r"\?\s+(?=\w)", r",?\s+and also\s+",
                r",?\s+and\s+(?=(?:what|who|how|which|when|where|why|is|are|does|do)\b)"):
        bits = [b.strip(" ?") for b in re.split(sep, q, maxsplit=1, flags=re.I)]
        if len(bits) == 2 and all(len(b.split()) >= 3 for b in bits):
            if route(bits[0])[0] != route(bits[1])[0]:
                return [b + "?" for b in bits]
    return [q]


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


def check(question: str, caps: list[str] | None = None) -> dict:
    q = question.strip()
    caps = set(caps or ["documents", "data", "graph"])
    parts = split_parts(q)
    signals = {"metric": False, "entity": [], "coverage": 0.0, "unknown": []}
    if "data" in caps:
        signals["metric"] = _names_metric(q)
    try:
        from graph import search as GS
        if GS.ready() and caps & {"graph", "documents"}:
            signals["entity"] = [e["name"] for e in GS.link(q)][:5]
    except Exception:                                      # noqa: BLE001 - no graph
        pass
    vocab, sv = vocabulary()
    words = _words(q)
    if words and vocab:
        signals["coverage"] = round(sum(_known(w, vocab, sv) for w in words) / len(words), 2)
    names = [m.group(0) for m in re.finditer(r"(?<!^)(?<![.?!]\s)\b[A-Z][\w\-]{2,}(?:\s+[A-Z][\w\-]{2,})*", q)]
    signals["unknown"] = [n for n in names if not all(_known(w, vocab, sv) for w in _words(n))] if vocab else []
    matched = bool(signals["metric"] or signals["entity"])
    ok = matched or (signals["coverage"] >= COVERAGE_MIN and not signals["unknown"])
    out = {"answerable": ok, "parts": parts if len(parts) > 1 else [], "signals": signals}
    if not ok:
        if not vocab:
            out["reason"] = "Nothing has been prepared yet: no documents or data."
        elif signals["unknown"]:
            out["reason"] = f"Nothing here mentions {', '.join(signals['unknown'][:3])}."
        else:
            out["reason"] = "No metric or document here covers this question."
        out["clarify"] = clarify(q, sv)
    return out
