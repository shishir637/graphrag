"""The unstructured-source family: sources of files that become searchable documents (connectors/github.py, s3.py).

A file source only says how to list what exists now (path -> version: a git blob sha, an S3 ETag...), how to fetch
one file, how to render it as a document and what to call it. Shared here:
  change detection  only files whose version changed since the last sync are fetched; the state keeps each path's
                    version per scope (a repository, a bucket/prefix)
  removals          files that disappeared become deleted documents
  groups            each scope's access groups (default: the connector's groups, else public)
  webhooks          WEBHOOK = True sources accept POST /api/v1/hooks/<kind>/<id> with an HMAC-SHA256 signature
                    (secret `webhook_secret`, checked in connectors/manage.py); the scheduler is the fallback
Secrets (tokens, keys) are stored only in app/vault.py.
"""
from contextlib import nullcontext

from .base import Connector, Document, SyncResult


class FileSource(Connector):
    WEBHOOK = False
    MAX_FILES = 5000

    # ---------------------------------------------------------------- source-specific
    def scopes(self) -> list[str]:
        """The units a sync walks (repositories, bucket/prefix...)."""
        raise NotImplementedError

    def listing(self, client, scope: str, **ctx) -> dict[str, str]:
        """path -> version of every file the source holds now (already filtered to the files worth indexing)."""
        raise NotImplementedError

    def fetch(self, client, scope: str, path: str, version: str, **ctx) -> bytes | None:
        raise NotImplementedError

    def render(self, scope: str, path: str, data: bytes, **ctx) -> bytes | None:
        """The document's bytes (None: skip this file, e.g. binary after all)."""
        return data

    def doc_name(self, scope: str, path: str) -> str:
        raise NotImplementedError

    def ref(self, scope: str, path: str, version: str) -> str:
        return f"{scope}:{path}@{version}"

    def client(self):
        """A context manager giving the client the other methods take."""
        return nullcontext(None)

    def scope_groups(self, scope: str) -> list[str]:
        return list(self.groups) or ["public"]

    # ---------------------------------------------------------------- shared
    def sync_files(self, client, scope: str, known: dict, groups: list[str], **ctx):
        """(documents to add or replace, document names to remove, the new path -> version map)."""
        want = self.listing(client, scope, **ctx)
        docs = []
        for path, version in want.items():
            if known.get(path) == version:
                continue                                        # unchanged since the last sync
            data = self.fetch(client, scope, path, version, **ctx)
            if data is None:
                continue
            body = self.render(scope, path, data, **ctx)
            if body is None:
                continue
            docs.append(Document(self.doc_name(scope, path), body, groups, ref=self.ref(scope, path, version)))
        deleted = [self.doc_name(scope, p) for p in known if p not in want]
        return docs, deleted, want

    def sync(self, state: dict) -> SyncResult:
        """A plain file source: every scope's changed files and removals."""
        res = SyncResult(state={"scopes": dict(state.get("scopes") or {})})
        n_files = n_deleted = 0
        with self.client() as c:
            for scope in self.scopes():
                st = dict(res.state["scopes"].get(scope) or {})
                docs, deleted, st["files"] = self.sync_files(c, scope, st.get("files") or {}, self.scope_groups(scope))
                res.documents += docs
                res.deleted += deleted
                n_files, n_deleted = n_files + len(docs), n_deleted + len(deleted)
                res.state["scopes"][scope] = st
        res.summary = {"files_changed": n_files, "files_removed": n_deleted}
        return res
