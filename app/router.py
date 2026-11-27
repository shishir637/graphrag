"""Where a question should be looked up, without a language model: a thin wrapper over app/planner.py (0.7),
kept for its existing callers. route(question) -> (route, reason); plan(question) -> (primary, secondary sources,
reason). See app/planner.py for the rules."""
from .planner import CONNECT, COUNTING, EXPLAIN, OVERVIEW, _names_metric, plan, route  # noqa: F401
