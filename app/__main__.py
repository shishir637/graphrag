import argparse
import os

from .main import serve

p = argparse.ArgumentParser(description="Run Context Layer")
p.add_argument("--host", default=os.environ.get("CL_HOST", "127.0.0.1"))
p.add_argument("--port", type=int, default=int(os.environ.get("CL_PORT", "8080")))
a = p.parse_args()
serve(a.host, a.port)
