"""GitHub: a repository's code and docs become searchable documents (and knowledge-graph facts); its activity
(commits, pull requests, issues, CI runs, contributors) becomes tables with a ready-made engineering semantic layer
(connectors/templates/github.yaml), so agents can ask "median time to merge last month" or "open bugs by repo".

Incremental: only files whose blob changed are fetched (state keeps each path's sha), activity is read since the
last cursor and merged into the tables by id. Each repository is its own access group (repo-<owner>-<name>), so a
person or key sees only the repositories they were given, in passages and in numbers (row access rules).

Config: repos (["owner/name"]), include_code (default true), include_activity (default true), max_files (2000),
max_file_kb (200), history_days (365). Secret: token (a fine-grained read-only token; optional for public repos).
GitHub Enterprise: CL_GITHUB_API=https://ghe.example.com/api/v3.
"""
import base64
import csv
import hashlib
import io
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath

from .base import Connector, Document, SyncResult, Table

API = os.environ.get("CL_GITHUB_API", "https://api.github.com").rstrip("/")
CODE_LANG = {".py": "python", ".js": "javascript", ".jsx": "jsx", ".ts": "typescript", ".tsx": "tsx", ".java": "java",
             ".go": "go", ".rb": "ruby", ".rs": "rust", ".cs": "csharp", ".php": "php", ".kt": "kotlin", ".swift": "swift",
             ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp", ".scala": "scala", ".sql": "sql", ".sh": "bash",
             ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".tf": "hcl", ".proto": "protobuf", ".vue": "vue",
             ".dart": "dart", ".r": "r", ".m": "objectivec", ".ex": "elixir", ".lua": "lua", ".css": "css",
             ".gradle": "groovy", ".dockerfile": "dockerfile"}
DOC_TEXT = {".md", ".markdown", ".rst", ".txt", ".adoc"}
DOC_NAMES = {"readme", "license", "licence", "changelog", "contributing", "codeowners", "notice", "authors",
             "security", "makefile"}                          # well-known files without an extension
SKIP_DIRS = {"node_modules", "vendor", "dist", "build", ".git", "__pycache__", ".venv", "venv", "target", "out",
             ".next", "coverage", "third_party", ".idea", ".vscode", "bin", "obj"}
SKIP_FILES = re.compile(r"(\.min\.(js|css)$|\.lock$|package-lock\.json$|yarn\.lock$|\.pb\.go$|_pb2\.py$)", re.I)
TABLES = {"github_repos": "repo_id", "github_commits": "sha", "github_pull_requests": "pr_id",
          "github_issues": "issue_id", "github_ci_runs": "run_id", "github_contributors": "contributor_id"}
BUG = re.compile(r"\bbug|defect|regression|crash\b", re.I)
COLUMNS = {
    "github_repos": ["repo_id", "name", "language", "default_branch", "stars", "forks", "created_at", "pushed_at"],
    "github_commits": ["sha", "repo_id", "author_login", "committed_at"],
    "github_pull_requests": ["pr_id", "repo_id", "number", "title", "author_login", "state", "created_at", "merged_at",
                             "closed_at", "hours_to_merge"],
    "github_issues": ["issue_id", "repo_id", "number", "title", "author_login", "state", "labels", "is_bug", "created_at",
                      "closed_at", "hours_to_close"],
    "github_ci_runs": ["run_id", "repo_id", "workflow", "event", "branch", "status", "conclusion", "created_at",
                       "duration_minutes"],
    "github_contributors": ["contributor_id", "repo_id", "login", "contributions"],
}


def repo_group(full: str) -> str:
    """Access group of a repository: repo-<owner>-<name> (lowercase, '-', at most 40 characters)."""
    g = "repo-" + re.sub(r"[^a-z0-9_-]+", "-", full.lower().replace("/", "-")).strip("-")
    return g if len(g) <= 40 else g[:31] + "-" + hashlib.sha1(full.encode()).hexdigest()[:8]


def _doc_name(full: str, path: str) -> str:
    base = f"gh-{full.replace('/', '-')}--{path.replace('/', '__')}"
    base = re.sub(r"[^\w.\- ()]+", "_", base)
    if len(base) > 140:
        base = base[:120] + "-" + hashlib.sha1(f"{full}/{path}".encode()).hexdigest()[:10]
    return base + ".md"


def _hours(a: str | None, b: str | None):
    if not a or not b:
        return ""
    d = datetime.fromisoformat(b.replace("Z", "+00:00")) - datetime.fromisoformat(a.replace("Z", "+00:00"))
    return round(d.total_seconds() / 3600, 2)


class GitHub(Connector):
    kind = "github"
    label = "GitHub"
    CONFIG = {"repos": "Repositories as owner/name, e.g. [\"acme/api\"]",
              "include_code": "Index code and docs (default true)",
              "include_activity": "Commits, pull requests, issues, CI runs as tables (default true)",
              "max_files": "Most files per repository (default 2000)",
              "max_file_kb": "Largest file to index in KB (default 200)",
              "history_days": "Activity history on the first sync (default 365)"}
    SECRETS = {"token": "A fine-grained, read-only token (Contents, Metadata, Issues, Pull requests, Actions). "
                        "Optional for public repositories.",
               "webhook_secret": "Secret of the repository webhook (optional; set the same value in GitHub)."}

    # ------------------------------------------------------------------ http
    def _client(self):
        import httpx
        h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
             "User-Agent": "context-layer-connector"}
        if self.secrets.get("token"):
            h["Authorization"] = f"Bearer {self.secrets['token']}"
        return httpx.Client(base_url=API, headers=h, timeout=60, follow_redirects=True)

    def _get(self, c, path, **params):
        r = c.get(path, params=params or None)
        if r.status_code == 403 and r.headers.get("x-ratelimit-remaining") == "0":
            raise RuntimeError("GitHub rate limit reached; the next scheduled sync will continue. "
                               "Add a token to raise the limit.")
        if r.status_code == 404:
            raise LookupError(path)
        if r.status_code in (401, 403):
            raise PermissionError(f"GitHub refused {path} ({r.status_code}). Check the token's access.")
        r.raise_for_status()
        return r

    def _pages(self, c, path, limit_pages=20, **params):
        params.setdefault("per_page", 100)
        url, n = path, 0
        while url and n < limit_pages:
            r = self._get(c, url, **(params if n == 0 else {}))
            yield from r.json() if isinstance(r.json(), list) else r.json().get("workflow_runs", [])
            nxt = r.links.get("next", {}).get("url")
            url, n = (nxt.replace(API, "") if nxt else None), n + 1

    def repos(self) -> list[str]:
        rs = self.config.get("repos") or []
        if isinstance(rs, str):
            rs = [x.strip() for x in rs.split(",")]
        bad = [r for r in rs if not re.fullmatch(r"[\w.-]+/[\w.-]+", r)]
        if not rs or bad:
            raise ValueError("Give repositories as owner/name" + (f" (not: {', '.join(bad)})" if bad else "."))
        return rs

    # ------------------------------------------------------------------ connector
    def test(self) -> dict:
        out = []
        with self._client() as c:
            for full in self.repos():
                try:
                    r = self._get(c, f"/repos/{full}").json()
                    out.append(f"{full}: ok ({'private' if r.get('private') else 'public'}, {r.get('default_branch')})")
                except LookupError:
                    return {"ok": False, "detail": f"{full}: not found or no access."}
                except PermissionError as e:
                    return {"ok": False, "detail": str(e)}
        return {"ok": True, "detail": "; ".join(out)}

    def sync(self, state: dict) -> SyncResult:
        res = SyncResult(state={"repos": dict(state.get("repos") or {})})
        rows: dict[str, list[dict]] = {t: [] for t in TABLES}
        n_files = n_deleted = 0
        with self._client() as c:
            for full in self.repos():
                st = dict(res.state["repos"].get(full) or {})
                meta = self._get(c, f"/repos/{full}").json()
                group = [repo_group(full)] + [g for g in self.groups if g != repo_group(full)]
                if self.config.get("include_code", True):
                    docs, deleted, st["files"] = self._files(c, full, meta["default_branch"], st.get("files") or {}, group)
                    res.documents += docs
                    res.deleted += deleted
                    n_files, n_deleted = n_files + len(docs), n_deleted + len(deleted)
                if self.config.get("include_activity", True):
                    self._activity(c, full, meta, st, rows)
                    res.access_rules += [{"entity": t, "column": "repo_id", "group": group[0], "values": [full]}
                                         for t in TABLES]
                res.state["repos"][full] = st
        if self.config.get("include_activity", True):
            res.tables = [Table(t, _merge_csv(t, key, rows[t])) for t, key in TABLES.items()]
            res.layer = _template()
        res.summary = {"files_changed": n_files, "files_removed": n_deleted,
                       "activity_rows": {t: len(v) for t, v in rows.items() if v}}
        return res

    # ------------------------------------------------------------------ code and docs
    def _files(self, c, full, branch, known: dict, groups) -> tuple[list[Document], list[str], dict]:
        max_files = int(self.config.get("max_files", 2000))
        max_bytes = int(self.config.get("max_file_kb", 200)) * 1024
        tree = self._get(c, f"/repos/{full}/git/trees/{branch}", recursive="1").json().get("tree", [])
        want = {}
        for e in tree:
            if e.get("type") != "blob" or (e.get("size") or 0) > max_bytes:
                continue
            p = PurePosixPath(e["path"])
            ext = _ext(p)
            if set(p.parts[:-1]) & SKIP_DIRS or SKIP_FILES.search(p.name) or (ext not in CODE_LANG and ext not in DOC_TEXT):
                continue
            want[e["path"]] = e["sha"]
            if len(want) >= max_files:
                break
        docs = []
        for path, sha in want.items():
            if known.get(path) == sha:
                continue                                        # unchanged since the last sync
            blob = self._get(c, f"/repos/{full}/git/blobs/{sha}").json()
            raw = base64.b64decode(blob.get("content") or "") if blob.get("encoding") == "base64" else \
                (blob.get("content") or "").encode()
            if b"\x00" in raw[:4096]:
                continue                                        # binary after all
            text = raw.decode("utf-8", errors="replace")
            docs.append(Document(_doc_name(full, path), _render(full, path, branch, text).encode("utf-8"), groups,
                                 ref=f"{full}@{sha}:{path}"))
        deleted = [_doc_name(full, p) for p in known if p not in want]
        return docs, deleted, want

    # ------------------------------------------------------------------ activity
    def _activity(self, c, full, meta, st: dict, rows: dict):
        now = datetime.now(timezone.utc)
        first = (now - timedelta(days=int(self.config.get("history_days", 365)))).isoformat(timespec="seconds")
        rows["github_repos"].append({"repo_id": full, "name": meta.get("name"), "language": meta.get("language") or "",
                                     "default_branch": meta.get("default_branch"), "stars": meta.get("stargazers_count", 0),
                                     "forks": meta.get("forks_count", 0), "created_at": meta.get("created_at"),
                                     "pushed_at": meta.get("pushed_at")})
        since = st.get("commits_since") or first
        for cm in self._pages(c, f"/repos/{full}/commits", since=since):
            a = cm.get("commit", {}).get("author") or {}
            rows["github_commits"].append({"sha": cm["sha"], "repo_id": full,
                                           "author_login": (cm.get("author") or {}).get("login") or a.get("name") or "",
                                           "committed_at": a.get("date")})
        prs_since = st.get("prs_since") or first
        for pr in self._pages(c, f"/repos/{full}/pulls", state="all", sort="updated", direction="desc"):
            if (pr.get("updated_at") or "") < prs_since:
                break
            rows["github_pull_requests"].append({
                "pr_id": f"{full}#{pr['number']}", "repo_id": full, "number": pr["number"], "title": pr.get("title", "")[:300],
                "author_login": (pr.get("user") or {}).get("login") or "", "state": pr.get("state"),
                "created_at": pr.get("created_at"), "merged_at": pr.get("merged_at") or "", "closed_at": pr.get("closed_at") or "",
                "hours_to_merge": _hours(pr.get("created_at"), pr.get("merged_at"))})
        issues_since = st.get("issues_since") or first
        for it in self._pages(c, f"/repos/{full}/issues", state="all", since=issues_since):
            if "pull_request" in it:
                continue
            labels = ", ".join(lb.get("name", "") for lb in it.get("labels") or [] if isinstance(lb, dict))
            rows["github_issues"].append({
                "issue_id": f"{full}#{it['number']}", "repo_id": full, "number": it["number"],
                "title": it.get("title", "")[:300], "author_login": (it.get("user") or {}).get("login") or "",
                "state": it.get("state"), "labels": labels, "is_bug": bool(BUG.search(labels)),
                "created_at": it.get("created_at"), "closed_at": it.get("closed_at") or "",
                "hours_to_close": _hours(it.get("created_at"), it.get("closed_at"))})
        runs_since = st.get("runs_since") or first
        try:
            for run in self._pages(c, f"/repos/{full}/actions/runs", created=f">={runs_since[:10]}", limit_pages=10):
                rows["github_ci_runs"].append({
                    "run_id": str(run["id"]), "repo_id": full, "workflow": run.get("name") or "", "event": run.get("event"),
                    "branch": run.get("head_branch") or "", "status": run.get("status"), "conclusion": run.get("conclusion") or "",
                    "created_at": run.get("created_at"),
                    "duration_minutes": round((_hours(run.get("run_started_at") or run.get("created_at"),
                                                      run.get("updated_at")) or 0) * 60, 1)})
        except (LookupError, PermissionError):
            pass                                              # Actions disabled or not allowed for this token
        try:
            for ct in self._pages(c, f"/repos/{full}/contributors", limit_pages=5):
                rows["github_contributors"].append({"contributor_id": f"{full}/{ct.get('login')}", "repo_id": full,
                                                    "login": ct.get("login") or "", "contributions": ct.get("contributions", 0)})
        except (LookupError, PermissionError):
            pass
        stamp = now.isoformat(timespec="seconds")
        st.update(commits_since=stamp, prs_since=stamp, issues_since=stamp, runs_since=stamp)


def _ext(p: PurePosixPath) -> str:
    if p.name.lower() == "dockerfile":
        return ".dockerfile"
    if not p.suffix and p.name.lower() in DOC_NAMES:
        return ".txt"
    return p.suffix.lower()


def _render(full: str, path: str, branch: str, text: str) -> str:
    p = PurePosixPath(path)
    head = f"# {full}: {path}\n\nRepository {full}, file `{path}` (branch {branch}).\n\n"
    ext = _ext(p)
    if ext in DOC_TEXT:
        return head + text
    return head + f"```{CODE_LANG.get(ext, '')}\n{text}\n```\n"


def _as_text(v) -> str:
    """A value read back from the stored table, written the way GitHub's API gives it (times as ...Z)."""
    import datetime as dt
    if v is None:
        return ""
    if isinstance(v, dt.datetime):
        if v.tzinfo is not None:
            v = v.astimezone(dt.timezone.utc).replace(tzinfo=None)
        return v.strftime("%Y-%m-%dT%H:%M:%SZ")
    return v


def _merge_csv(table: str, key: str, new: list[dict]) -> bytes:
    """The workspace's current table updated with new rows (same id: replaced)."""
    from app.storage import store
    st = store()
    rows: dict[str, dict] = {}
    if st.exists("data", f"{table}.parquet"):                # the landed table (0.7+)
        import pyarrow.parquet as pq
        for r in pq.read_table(st.path_for_duckdb("data", f"{table}.parquet")).to_pylist():
            rows[str(r[key])] = {k: _as_text(v) for k, v in r.items()}
    elif st.exists("data", f"{table}.csv"):                  # a workspace not upgraded yet
        with st.open("data", f"{table}.csv") as fb, io.TextIOWrapper(fb, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                rows[r[key]] = r
    for r in new:
        rows[str(r[key])] = {k: ("" if v is None else v) for k, v in r.items()}
    cols = COLUMNS[table]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows.values():
        w.writerow(r)
    return buf.getvalue().encode("utf-8")


def _template() -> dict:
    from pathlib import Path
    from ruamel.yaml import YAML
    return YAML(typ="safe").load((Path(__file__).parent / "templates" / "github.yaml").read_text(encoding="utf-8"))
