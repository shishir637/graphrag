"""Small RAG system - command line entry point.

  python rag.py ingest                     # steps 1-4: parse -> chunk -> embed -> index (Qdrant)
  python rag.py parse | chunk | embed | index   # run one step on its own
  python rag.py ask "your question" [--groups hr,public] [--sources file.pdf]
  python rag.py chat                       # interactive question loop
  python rag.py draft-questions -n 20      # let Claude draft an eval set from your chunks (review it!)
  python rag.py eval                       # RAGAS evaluation over eval_questions.json
  python rag.py graph-build                # GraphRAG: LLM extraction -> entity resolution -> Leiden -> reports -> AGE
  python rag.py graph-ask "q" [--mode auto|local|global|hybrid]
  python rag.py graph-eval [--modes rag,hybrid,local,global]   # does the graph help? per question type
  python rag.py doctor                     # check that every package loads on this PC
  python rag.py ui [--port 8000]           # console (/console), pipeline explorer (/) and MCP endpoint (/mcp)
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
    for name in ("ingest", "parse", "chunk", "embed", "index", "chat", "eval", "doctor"):
        sub.add_parser(name)
    a = sub.add_parser("ask")
    a.add_argument("question")
    for sp in (a,):
        sp.add_argument("--groups", help="comma-separated access groups of the asking user")
        sp.add_argument("--sources", help="comma-separated file names to restrict the search to")
    d = sub.add_parser("draft-questions")
    d.add_argument("-n", type=int, default=20)
    gb = sub.add_parser("graph-build", help="GraphRAG: extract -> resolve -> communities -> summaries -> store")
    gb.add_argument("--steps", help="comma-separated subset: extract,resolve,communities,summarize,store")
    ga = sub.add_parser("graph-ask")
    ga.add_argument("question")
    ga.add_argument("--mode", choices=["auto", "local", "global", "hybrid"], default="auto")
    ga.add_argument("--groups"); ga.add_argument("--sources")
    ge = sub.add_parser("graph-eval", help="plain RAG vs graph modes on the same questions, per question type")
    ge.add_argument("--modes", default="rag,hybrid,local")
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


def _doctor():
    """Import every compiled dependency and report which ones this PC refuses to load."""
    import importlib
    checks = [("torch", "embeddings, GLiNER2, PDF layout"), ("torchvision", "PDF layout"), ("transformers", "models"),
              ("grpc", "Qdrant client"), ("qdrant_client", "search index"), ("fastembed", "keyword search, re-ranker"),
              ("onnxruntime", "re-ranker"), ("docling", "parsing"), ("pyarrow", "parsing"), ("cv2", "parsing"),
              ("gliner2", "local extraction"), ("psycopg", "Apache AGE"), ("rapidfuzz", "entity resolution"),
              ("networkx", "communities"), ("mcp", "MCP server"), ("cryptography", "MCP server"),
              ("rpds", "MCP server"), ("fastapi", "web UI"), ("uvicorn", "web UI")]
    if sys.platform == "win32":
        checks.append(("win32api", "MCP server (pywin32)"))
    bad = 0
    for mod, used_for in checks:
        try:
            m = importlib.import_module(mod)
            print(f"  OK       {mod:<14} {getattr(m, '__version__', ''):<14} {used_for}")
        except ModuleNotFoundError as e:
            bad += 1
            print(f"  MISSING  {mod:<14} {'':<14} {used_for}: not installed ({e.name})")
        except Exception as e:                                  # noqa: BLE001
            bad += 1
            print(f"  BLOCKED  {mod:<14} {'':<14} {used_for}: {str(e)[:90]}")
    print("\nAll good." if not bad else f"\n{bad} problem(s). MISSING: run pip install -r requirements.txt. BLOCKED with "
          "'Application Control policy': Windows refuses that version; try an older one (README, Windows notes).")


def _run(args):
    if args.cmd == "doctor":
        return _doctor()

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
    elif args.cmd == "graph-build":
        from rag.graph.build import build_graph
        build_graph(*([_csv(args.steps)] if args.steps else []))
    elif args.cmd == "graph-ask":
        from rag.graph.search import ask_graph
        r = ask_graph(args.question, args.mode, _csv(args.groups), _csv(args.sources))
        print(f"  mode: {r['mode']}")
        _print_answer(r)
    elif args.cmd == "graph-eval":
        from rag.graph.evaluate import run_graph_eval
        run_graph_eval(tuple(_csv(args.modes)))
    elif args.cmd == "ui":
        from ui.server import serve; serve(port=args.port)


def _print_answer(r):
    if r["answer"] is None:      # no LLM configured: show what retrieval found
        print("\n(no LLM configured, so no written answer. Set ANTHROPIC_API_KEY, or GRAPH_LLM=openai for a local "
              "model.) Best passages:")
        for i, c in enumerate(r["contexts"], 1):
            print(f"\n  [{i}] {c['source']}#{c['chunk_id']}\n      " + " ".join(c["text"].split())[:300])
        print(f"\n  ({r['timings_s']['total']}s total)")
        return
    print("\n" + r["answer"])
    for c in r["contexts"]:
        if c["chunk_id"] in r["citations"]:
            pages = f" p.{','.join(map(str, c['pages']))}" if c["pages"] else ""
            print(f"  [{c['chunk_id']}] {c['source']}{pages}")
    print(f"  ({r['timings_s']['total']}s total, found={r['found']})")


if __name__ == "__main__":
    sys.exit(main())
