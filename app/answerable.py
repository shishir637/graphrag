"""Is a question answerable from what this workspace knows? A thin wrapper over app/planner.py (0.7), kept for its
existing callers: check(question, caps) -> {"answerable", "parts", "signals", "reason"?, "clarify"?}. See
app/planner.py for the signals and rules (no AI)."""
from .planner import (COVERAGE_MIN, FACT_WORDS, STOP, _VOCAB, _known, _words, check, clarify,  # noqa: F401
                      split_parts, vocabulary)
