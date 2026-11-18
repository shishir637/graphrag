"""Small RAG system - command line entry point.

  python rag.py ingest                     # steps 1-4: parse -> chunk -> embed -> index (Qdrant)
  python rag.py parse | chunk | embed | index   # run one step on its own
  python rag.py ask "your question" [--groups hr,public] [--sources file.pdf]
  python rag.py chat                       # interactive question loop
  python rag.py draft-questions -n 20      # let Claude draft an eval set from your chunks (review it!)
  python rag.py eval                       # RAGAS evaluation over eval_questions.json
  python rag.py ui [--port 8000]           # visual interface: every step, explained, in the browser
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
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("ingest", "parse", "chunk", "embed", "index", "chat", "eval"):
        sub.add_parser(name)
    a = sub.add_parser("ask")
    a.add_argument("question")
    for sp in (a,):
        sp.add_argument("--groups", help="comma-separated access groups of the asking user")
        sp.add_argument("--sources", help="comma-separated file names to restrict the search to")
    d = sub.add_parser("draft-questions")
    d.add_argument("-n", type=int, default=20)
    u = sub.add_parser("ui")
    u.add_argument("--port", type=int, default=8000)
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
        from rag import store
        if store._client is not None:      # close Qdrant cleanly (avoids a noisy error at exit)
            store._client.close()


def _run(args):

    if args.cmd == "ask":
        from rag.generate import ask
        r = ask(args.question, _csv(args.groups), _csv(args.sources))
        _print_answer(r)
    elif args.cmd == "chat":
        from rag.generate import ask
        while True:
            try:
                q = input("\nQuestion (empty to quit): ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not q:
                break
            _print_answer(ask(q))
    elif args.cmd == "draft-questions":
        from rag.evaluate import draft_questions; draft_questions(args.n)
    elif args.cmd == "eval":
        from rag.evaluate import run_eval; run_eval()
    elif args.cmd == "ui":
        from ui.server import serve; serve(port=args.port)


def _print_answer(r):
    print("\n" + r["answer"])
    for c in r["contexts"]:
        if c["chunk_id"] in r["citations"]:
            pages = f" p.{','.join(map(str, c['pages']))}" if c["pages"] else ""
            print(f"  [{c['chunk_id']}] {c['source']}{pages}")
    print(f"  ({r['timings_s']['total']}s total, found={r['found']})")


if __name__ == "__main__":
    sys.exit(main())
