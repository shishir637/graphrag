"""Small RAG system - command line entry point.

  python rag.py ingest                     # steps 1-4: parse -> chunk -> embed -> index (Qdrant)
  python rag.py parse | chunk | embed | index   # run one step on its own
  python rag.py ask "your question" [--groups hr,public] [--sources file.pdf]
  python rag.py chat                       # interactive question loop
  python rag.py draft-questions -n 20      # let Claude draft an eval set from your chunks (review it!)
  python rag.py eval                       # RAGAS evaluation over eval_questions.json

Semantic layer (structured data in data/; see SEMANTIC_LAYER.md):
  python rag.py sl-data                    # generate the sample sales tables + finance report
  python rag.py sl-profile                 # phase 1: profile tables, suggest joins
  python rag.py sl-model [--no-llm]        # phase 2: draft model.yaml from the profile
  python rag.py sl-metrics [--draft]       # phase 3: validate + reconcile metrics.yaml (--draft: Claude proposes more)
  python rag.py sl-enrich                  # phase 4: value lists, synonyms, verified queries -> DuckDB + Qdrant
  python rag.py sl-sql '{"metrics": ["net_revenue"], "dimensions": ["regions.region_name"]}'
  python rag.py sl-ask "net revenue by region last quarter"
  python rag.py ask "..." --route auto|docs|data   # auto: Claude decides documents vs. data
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
    for sp in (a,):
        sp.add_argument("--groups", help="comma-separated access groups of the asking user")
        sp.add_argument("--sources", help="comma-separated file names to restrict the search to")
    d = sub.add_parser("draft-questions")
    d.add_argument("-n", type=int, default=20)
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
        from rag.evaluate import draft_questions; draft_questions(args.n)
    elif args.cmd == "eval":
        from rag.evaluate import run_eval; run_eval()


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
