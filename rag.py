"""Small RAG system - command line entry point.

  python rag.py ingest                     # steps 1-4: parse -> chunk -> embed -> index (Qdrant)
  python rag.py parse | chunk | embed | index   # run one step on its own
  python rag.py ask "your question" [--groups hr,public] [--sources file.pdf]
  python rag.py chat                       # interactive question loop
  python rag.py draft-questions -n 20      # let Claude draft an eval set from your chunks (review it!)
  python rag.py draft-questions -n 60 --mix   # large docs: spread over regions, 4 question types, gold chunk ids
  python rag.py eval                       # RAGAS evaluation over eval_questions.json

Semantic layer (structured data in data/; see SEMANTIC_LAYER.md):
  python rag.py sl-data                    # generate the sample sales tables + finance report
  python rag.py sl-profile                 # phase 1: profile tables, suggest joins
  python rag.py sl-model [--no-llm]        # phase 2: draft model.yaml from the profile
  python rag.py sl-metrics [--draft]       # phase 3: validate + reconcile metrics.yaml (--draft: Claude proposes more)
  python rag.py sl-enrich                  # phase 4: value lists, synonyms, verified queries -> DuckDB + Qdrant
  python rag.py sl-sql '{"metrics": ["net_revenue"], "dimensions": ["regions.region_name"]}'
  python rag.py sl-ask "net revenue by region last quarter"
  python rag.py sl-eval [--planner claude-haiku-5-5] [--tag name]   # gold-set evaluation (semantic_layer/eval_questions.yaml)
  python rag.py ask "..." --route auto|docs|data   # auto: Claude decides documents vs. data

Graph RAG (Apache AGE in Docker: docker compose -f docker-compose.graph.yml up -d; see GRAPH_RAG.md):
  python rag.py gr-extract --regions "Kerala,Tamil Nadu" [--model claude-haiku-5-5]   # entities + relations per chunk
  python rag.py gr-build outputs/gr_extract_south.jsonl   # merge duplicates, tables, communities -> AGE + Qdrant
  python rag.py gr-ask "question" [--mode auto|vector|local|global|data]
  python rag.py gr-eval questions|run|score                # graph vs vector RAG comparison
"""
import argparse
import os
import sys
from pathlib import Path


def _load_dotenv():
    """Read KEY=value lines from .env next to this file (e.g. ANTHROPIC_API_KEY) without overriding the shell."""
    env = Path(__file__).with_name(".env")
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()


def _csv(v):
    return [x.strip() for x in v.split(",") if x.strip()] if v else None


def main():
    for stream in (sys.stdout, sys.stderr):        # Windows consoles/pipes default to cp1252 (no ₹ etc.)
        stream.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("ingest", "parse", "chunk", "embed", "index", "chat", "eval",
                 "sl-data", "sl-profile", "sl-enrich"):
        sub.add_parser(name)
    a = sub.add_parser("ask")
    a.add_argument("question")
    a.add_argument("--route", choices=["auto", "docs", "data"], default="auto")
    sub.add_parser("sl-model").add_argument("--no-llm", action="store_true")
    sub.add_parser("sl-metrics").add_argument("--draft", action="store_true")
    sub.add_parser("sl-sql").add_argument("query", help="SemanticQuery as JSON")
    sub.add_parser("sl-ask").add_argument("question")
    se = sub.add_parser("sl-eval")
    se.add_argument("--planner", help="model for routing/planning/summary, e.g. claude-haiku-5-5")
    se.add_argument("--tag", default="baseline")
    for sp in (a,):
        sp.add_argument("--groups", help="comma-separated access groups of the asking user")
        sp.add_argument("--sources", help="comma-separated file names to restrict the search to")
    ge = sub.add_parser("gr-extract")
    ge.add_argument("--regions")
    ge.add_argument("--model", default="claude-haiku-5-5")
    ge.add_argument("--workers", type=int, default=8)
    ge.add_argument("--out", default="outputs/gr_extract.jsonl")
    sub.add_parser("gr-build").add_argument("paths", nargs="+")
    ga = sub.add_parser("gr-ask")
    ga.add_argument("question")
    ga.add_argument("--mode", default="auto", choices=["auto", "vector", "local", "global", "data"])
    sub.add_parser("gr-eval").add_argument("stage", choices=["questions", "run", "score"])
    d = sub.add_parser("draft-questions")
    d.add_argument("-n", type=int, default=20)
    d.add_argument("--mix", action="store_true", help="large documents: questions per region, 4 question types")
    args = p.parse_args()

    if args.cmd in ("ingest", "parse"):
        print("[1] Parsing"); from rag.parse import parse_all; parse_all()
    if args.cmd in ("ingest", "chunk"):
        print("[2] Chunking"); from rag.chunk import chunk_all; chunk_all()
    if args.cmd in ("ingest", "embed"):
        print("[3] Embedding"); from rag.embed import embed_all; embed_all()
    if args.cmd in ("ingest", "index"):
        print("[4] Indexing in Qdrant"); from rag.store import build_index; build_index()

    try:
        _run(args)
    finally:
        store = sys.modules.get("rag.store")    # only if this command opened Qdrant
        if store is not None and store._client is not None:
            store._client.close()               # close Qdrant cleanly (avoids a noisy error at exit)


def _run(args):

    if args.cmd == "ask":
        _answer(args.question, args.route, args.groups, args.sources)
    elif args.cmd == "sl-data":
        from semantic.sample_data import generate; generate()
    elif args.cmd == "sl-profile":
        from semantic.profile import profile_all; profile_all()
    elif args.cmd == "sl-model":
        from semantic.model import draft_model; draft_model(use_llm=not args.no_llm)
    elif args.cmd == "sl-metrics":
        from semantic.metrics import check_metrics, draft_metrics
        if args.draft:
            draft_metrics()
        check_metrics()
    elif args.cmd == "sl-enrich":
        from semantic.enrich import build_enrichment; build_enrichment()
    elif args.cmd == "sl-sql":
        from semantic.compiler import run_json; run_json(args.query)
    elif args.cmd == "sl-eval":
        from semantic.evaluate import run as sl_eval; sl_eval(args.tag, args.planner)
    elif args.cmd == "sl-ask":
        from semantic.answer import answer_data_question, print_data_answer
        print_data_answer(answer_data_question(args.question))
    elif args.cmd == "chat":
        while True:
            try:
                q = input("\nQuestion (empty to quit): ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not q:
                break
            _answer(q, "auto")
    elif args.cmd == "draft-questions":
        from rag.evaluate import draft_mixed_questions, draft_questions
        draft_mixed_questions(args.n) if args.mix else draft_questions(args.n)
    elif args.cmd == "eval":
        from rag.evaluate import run_eval; run_eval()
    elif args.cmd == "gr-extract":
        sys.argv = ["extract_llm", "--model", args.model, "--workers", str(args.workers), "--out", args.out] +                    (["--regions", args.regions] if args.regions else [])
        from graph.extract_llm import main as gr_extract; gr_extract()
    elif args.cmd == "gr-build":
        from graph.build import build; build(args.paths)
    elif args.cmd == "gr-ask":
        from graph.query import answer, print_answer; print_answer(answer(args.question, args.mode))
    elif args.cmd == "gr-eval":
        from graph import evaluate as ge
        {"questions": ge.make_questions, "run": ge.run, "score": ge.score}[args.stage]()


def _answer(question, route="auto", groups=None, sources=None):
    """Route to the semantic layer (numbers from tables) or to RAG (documents)."""
    if route == "auto":
        from semantic.answer import route_question
        route = route_question(question)
        print(f"  [router] -> {route}")
    if route == "data":
        from semantic.answer import answer_data_question, print_data_answer
        print_data_answer(answer_data_question(question))
    else:
        from rag.generate import ask
        _print_answer(ask(question, _csv(groups), _csv(sources)))


def _print_answer(r):
    print("\n" + r["answer"])
    for c in r["contexts"]:
        if c["chunk_id"] in r["citations"]:
            pages = f" p.{','.join(map(str, c['pages']))}" if c["pages"] else ""
            print(f"  [{c['chunk_id']}] {c['source']}{pages}")
    print(f"  ({r['timings_s']['total']}s total, found={r['found']})")


if __name__ == "__main__":
    sys.exit(main())
