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


def raw_read(path: Path):
    return _yaml().load(path.read_text(encoding="utf-8"))


def _store_file(path: Path) -> str | None:
    """The layer files that live in the definitions store when it is on (semantic/defstore.py)."""
    from . import defstore
    from .config import LAYER_DIR
    if defstore.enabled() and path.name in defstore.SPEC and Path(path).parent.name == LAYER_DIR.name:
        return path.name
    return None


def read(path: Path):
    name = _store_file(path)
    if name:
        from . import defstore
        return defstore.compose(name)
    return raw_read(path)


def write(path: Path, data, header: str = ""):
    name = _store_file(path)
    if name:
        from . import defstore
        defstore.save_file(name, _plain(data))
        return
    raw_write(path, data, header)


def raw_write(path: Path, data, header: str = ""):
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


def load_model(path: Path = MODEL_FILE) -> SemanticModel:
    return SemanticModel.model_validate(_plain(read(path)))


def load_metrics(path: Path = METRICS_FILE) -> MetricSet:
    return MetricSet.model_validate(_plain(read(path)))


def load_enrichment(path: Path = ENRICHMENT_FILE) -> Enrichment:
    return Enrichment.model_validate(_plain(read(path)))


def load_verified(path: Path = VERIFIED_FILE) -> VerifiedSet:
    return VerifiedSet.model_validate(_plain(read(path)))
