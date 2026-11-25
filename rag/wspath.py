"""Folders that belong to the current workspace.

`WorkspaceDir("documents", default)` behaves like a pathlib.Path but is resolved each time it is used: inside a
workspace (the app sets one per request, MCP call or job) it is <CL_FILES_DIR>/<workspace id>/documents, otherwise
(command line, tests) the default folder. Joining (`/`) stays lazy, so module-level constants such as
`OUT_DIR / "2_chunks.json"` follow the workspace too.

CL_FILES_DIR: where workspace files live (default ./var/workspaces; a Docker volume in production).
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def files_root() -> Path:
    return Path(os.environ.get("CL_FILES_DIR") or (ROOT / "var" / "workspaces"))


def current_workspace() -> str | None:
    try:
        from app.db import current_tenant
    except ImportError:                                      # engines used without the app
        return None
    return current_tenant()


class PerFile(dict):
    """A small cache of things loaded from a file, one entry per resolved path (so workspaces never share)."""

    def __init__(self, size: int = 64):
        super().__init__()
        self.size = size

    def entry(self, path) -> dict:
        key = str(Path(path))
        e = self.get(key)
        if e is None:
            e = self[key] = {"mtime": None}
            while len(self) > self.size:
                self.pop(next(iter(self)))
        return e


def tenant_key() -> str:
    """The workspace id for search-index payloads ("" outside the app)."""
    return current_workspace() or ""


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
