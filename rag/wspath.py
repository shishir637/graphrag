"""Folders that belong to the current workspace.

`WorkspaceDir("documents", default)` behaves like a pathlib.Path but is resolved each time it is used: in the first
workspace ("default"), on the command line and in tests it is the usual folder (./documents, ./outputs, ...), so an
existing install keeps its files where they are; in any other workspace it is <CL_FILES_DIR>/<workspace id>/<name>.
Joining (`/`) stays lazy, so module-level constants such as `OUT_DIR / "2_chunks.json"` follow the workspace too.

CL_FILES_DIR: where the other workspaces' files live (default ./var/workspaces; a Docker volume in production).
"""
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def files_root() -> Path:
    return Path(os.environ.get("CL_FILES_DIR") or (ROOT / "var" / "workspaces"))


def current_workspace() -> str | None:
    """The workspace whose files are used, or None for the first workspace (the usual folders)."""
    try:
        from app.db import DEFAULT, TENANT
    except ImportError:                                      # engines used without the app
        return None
    t = TENANT.get()
    return None if not t or t == DEFAULT else t


def workspace_key() -> str:
    """For caches and index names: '' in the first workspace, else a safe form of the workspace id."""
    t = current_workspace()
    return re.sub(r"[^a-z0-9_]", "_", t.lower()) if t else ""


class WorkspaceDir(os.PathLike):
    __slots__ = ("_name", "_default", "_parts")

    def __init__(self, name: str, default: Path, parts: tuple = ()):
        self._name, self._default, self._parts = name, Path(default), tuple(parts)

    def resolve_now(self) -> Path:
        t = current_workspace()
        base = files_root() / t / self._name if t else self._default
        return base.joinpath(*self._parts) if self._parts else base

    def __truediv__(self, other) -> "WorkspaceDir":
        return WorkspaceDir(self._name, self._default, self._parts + (str(other),))

    def __fspath__(self) -> str:
        return str(self.resolve_now())

    def __str__(self) -> str:
        return str(self.resolve_now())

    def __repr__(self) -> str:
        return f"WorkspaceDir({self._name!r}, parts={self._parts!r}) -> {self.resolve_now()}"

    def __eq__(self, other) -> bool:
        return Path(self) == Path(other) if isinstance(other, (str, os.PathLike)) else NotImplemented

    def __hash__(self):
        return hash((self._name, self._parts))

    def __getattr__(self, attr):
        return getattr(self.resolve_now(), attr)


EMPTY_LAYER = {"model.yaml": {"version": 1, "entities": [], "joins": []},
               "metrics.yaml": {"version": 1, "metrics": []},
               "enrichment.yaml": {"version": 1, "synonyms": [], "value_list_dimensions": []},
               "verified_queries.yaml": {"version": 1, "queries": []}}


def seed_workspace(tid: str):
    """A new workspace starts empty: its folders and an empty semantic layer (no sample company)."""
    from app.db import tenant_scope
    from semantic.yamlio import raw_write
    with tenant_scope(tid):
        from rag import config as RC
        from semantic import config as SC
        for d in (RC.DOCS_DIR, RC.OUT_DIR, SC.DATA_DIR, SC.LAYER_DIR, SC.STORE_DIR, SC.OUT_DIR):
            Path(d).mkdir(parents=True, exist_ok=True)
        for name, data in EMPTY_LAYER.items():
            p = Path(SC.LAYER_DIR / name)
            if not p.exists():
                raw_write(p, data, "Empty semantic layer of a new workspace.")
        ins = Path(SC.LAYER_DIR / "instructions.md")
        if not ins.exists():
            ins.write_text("", encoding="utf-8")
