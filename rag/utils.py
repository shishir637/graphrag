import json
import time
from contextlib import contextmanager
from pathlib import Path

from .config import OUT_DIR


def write_json(name: str, data) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / name
    tmp = path.with_suffix(path.suffix + ".tmp")          # readers (agents' questions) never see half a file
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    print(f"  -> wrote {path.relative_to(OUT_DIR.parent)}")
    return path


def read_json(name: str):
    path = OUT_DIR / name
    if not path.exists():
        raise SystemExit(f"{path} not found - run the earlier step first.")
    return json.loads(path.read_text(encoding="utf-8"))


@contextmanager
def timer(label: str, timings: dict):
    start = time.perf_counter()
    yield
    timings[label] = round(time.perf_counter() - start, 3)
