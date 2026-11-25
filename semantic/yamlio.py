"""Read and write the layer's YAML with ruamel.yaml (keeps comments and key order when people edit
the files), always validating through the Pydantic schema."""
from io import StringIO
from pathlib import Path

from ruamel.yaml import YAML

from .config import ENRICHMENT_FILE, METRICS_FILE, MODEL_FILE, VERIFIED_FILE
from .schema import Enrichment, MetricSet, SemanticModel, VerifiedSet


def _yaml() -> YAML:
    y = YAML()
    y.indent(mapping=2, sequence=4, offset=2)
    y.width = 110
    return y


def read(path: Path):
    return _yaml().load(path.read_text(encoding="utf-8"))


def write(path: Path, data, header: str = ""):
    buf = StringIO()
    _yaml().dump(data, buf)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(f"# {line}\n" if line else "#\n" for line in header.splitlines()) + buf.getvalue()
    path.write_text(text, encoding="utf-8")


def _plain(obj):
    """ruamel's CommentedMap/Seq -> plain dict/list for Pydantic."""
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_plain(v) for v in obj]
    return obj


def _workspace() -> bool:
    """Inside a workspace (the app), definitions come from Postgres; scripts and tests without one read the files."""
    try:
        from .repo import in_workspace
        return in_workspace()
    except ImportError:
        return False


def load_model(path: Path | None = None) -> SemanticModel:
    if path is None and _workspace():
        from . import repo
        return repo.load_model()
    return SemanticModel.model_validate(_plain(read(path or MODEL_FILE)))


def load_metrics(path: Path | None = None) -> MetricSet:
    if path is None and _workspace():
        from . import repo
        return repo.load_metrics()
    return MetricSet.model_validate(_plain(read(path or METRICS_FILE)))


def load_enrichment(path: Path | None = None) -> Enrichment:
    if path is None and _workspace():
        from . import repo
        return repo.load_enrichment()
    return Enrichment.model_validate(_plain(read(path or ENRICHMENT_FILE)))


def load_verified(path: Path | None = None) -> VerifiedSet:
    if path is None and _workspace():
        from . import repo
        return repo.load_verified()
    return VerifiedSet.model_validate(_plain(read(path or VERIFIED_FILE)))


def load_instructions(path: Path | None = None) -> str:
    if path is None and _workspace():
        from . import repo
        return repo.load_instructions()
    from .config import INSTRUCTIONS_FILE
    f = path or INSTRUCTIONS_FILE
    return f.read_text(encoding="utf-8") if f.exists() else ""
