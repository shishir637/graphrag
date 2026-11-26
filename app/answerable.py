"""Is a question answerable from what this workspace knows? Decided before any evidence is gathered (rules, no AI).

Signals, all cheap:
  metric    the rule planner matches a defined metric
  entity    the question names something in the knowledge graph
  facts     it asks for a kind of fact the workspace has read from documents (notice period, invoice amount...)
  coverage  share of the question's content words that occur in the workspace's documents, metric names,
            synonyms and known values (typo-tolerant)
  unknown   capitalised names that occur nowhere in the workspace
Not answerable when no metric, entity or fact matches, coverage is low or a name is unknown: the answer is
found: false with the reason and clarifying questions (close metric names and terms), and nothing is guessed.
Mixed questions ("revenue last quarter and the leave policy") are split into parts with their own routes.
"""
import json
import re

from rapidfuzz import fuzz, process

from rag.wspath import PerFile

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
    from rag.config import OUT_DIR
    path = OUT_DIR / "2_chunks.json"
    try:
        m = path.stat().st_mtime
    except OSError:
        m = None
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
        for c in json.loads(path.read_text(encoding="utf-8")):
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
    from app.router import route
    q = question.strip()
    for sep in (r"\s*;\s*", r"\?\s+(?=\w)", r",?\s+and also\s+", r",?\s+and\s+(?=(?:what|who|how|which|when|where|why|is|are|does|do)\b)"):
        bits = [b.strip(" ?") for b in re.split(sep, q, maxsplit=1, flags=re.I)]
        if len(bits) == 2 and all(len(b.split()) >= 3 for b in bits):
            if route(bits[0])[0] != route(bits[1])[0]:
                return [b + "?" for b in bits]
    return [q]


def check(question: str, caps: list[str] | None = None) -> dict:
    from app.router import _names_metric
    q = question.strip()
    caps = set(caps or ["documents", "data", "graph"])
    parts = split_parts(q)
    signals = {"metric": False, "entity": [], "facts": [], "coverage": 0.0, "unknown": []}
    if "data" in caps:
        signals["metric"] = _names_metric(q)
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
    sv = _VOCAB.entry(__import__("rag.config", fromlist=["OUT_DIR"]).OUT_DIR / "2_chunks.json").get("sorted") or sorted(vocab)
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
