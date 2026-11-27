"""python -m app: the web app, REST and MCP server (CL_ROLE=all or api), or the worker (CL_ROLE=worker)."""
import argparse
import os

from .worker import role

if role() == "worker":
    from .worker import main
    main()
    raise SystemExit(0)

from .main import serve  # noqa: E402

p = argparse.ArgumentParser(description="Run Context Layer")
p.add_argument("--host", default=os.environ.get("CL_HOST", "127.0.0.1"))
p.add_argument("--port", type=int, default=int(os.environ.get("CL_PORT", "8080")))
a = p.parse_args()
serve(a.host, a.port)
