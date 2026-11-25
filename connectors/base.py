"""The connector framework.

A connector reads one kind of source (GitHub, a Postgres database...) and returns what changed since its last
sync: documents to add or replace, documents to remove, data tables, a ready-made semantic layer for its tables,
row access rules, and its new state (cursors, file versions). It never writes into the workspace itself;
connectors/manage.py stores the result and starts the rebuild of exactly what changed.

To add a connector: subclass Connector, set kind / CONFIG / SECRETS, implement test() and sync(), and register it
in KINDS (connectors/manage.py).
"""
from dataclasses import dataclass, field


@dataclass
class Document:
    name: str                       # file name in the workspace (unique per connector; must have a document extension)
    data: bytes
    groups: list[str]               # who may see it
    ref: str = ""                   # where it came from (e.g. owner/repo@sha:path)


@dataclass
class Table:
    name: str                       # snake_case table name
    csv: bytes                      # the whole table as CSV (replaces the previous version)


@dataclass
class SyncResult:
    documents: list[Document] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)          # document names to remove
    tables: list[Table] = field(default_factory=list)
    layer: dict | None = None                                  # {"model": ..., "metrics": ..., "enrichment": ...}
    access_rules: list[dict] = field(default_factory=list)     # {"entity", "column", "group", "values"}
    state: dict = field(default_factory=dict)
    summary: dict = field(default_factory=dict)


class Connector:
    kind = ""
    label = ""
    CONFIG: dict = {}                # field -> description (shown in the app and the API)
    SECRETS: dict = {}               # secret field -> description (stored encrypted, never shown again)

    def __init__(self, cid: str, config: dict, secrets: dict, groups: list[str]):
        self.id, self.config, self.secrets, self.groups = cid, config, secrets, groups

    def test(self) -> dict:
        """Check access without changing anything. Returns {"ok": bool, "detail": str}."""
        raise NotImplementedError

    def sync(self, state: dict) -> SyncResult:
        raise NotImplementedError
