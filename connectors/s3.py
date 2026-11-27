"""An S3-compatible folder (AWS S3, MinIO, Cloudflare R2, Wasabi...) as a source of documents: the second file
source on the file-source base (connectors/files_base.py), which proves the base: change detection by ETag,
removals and groups are shared; this module only lists, fetches and names objects.

Chosen over Google Drive for this version: Drive needs a Google Cloud OAuth app and consent screen per install,
while one S3 implementation covers every S3-compatible store (including a company's own MinIO).

The client (boto3) is an optional dependency: pip install boto3. Without it the connector is not offered.

Config: bucket, prefix (optional folder, e.g. "contracts/"), endpoint_url (for non-AWS stores, e.g.
http://minio:9000), region (default us-east-1), extensions (default: the document types Context Layer reads),
max_file_kb (default 20,000). Secrets: access_key_id, secret_access_key (read-only credentials for that bucket).
Documents keep their file type: s3-<bucket>--<key with / as __>.<ext>.
"""
import hashlib
import re
from pathlib import PurePosixPath

import boto3                                          # optional dependency: the connector is skipped without it

from .files_base import FileSource

DEFAULT_EXTS = {".pdf", ".docx", ".pptx", ".xlsx", ".csv", ".html", ".htm", ".md", ".txt", ".json", ".jsonl"}


class S3Folder(FileSource):
    kind = "s3"
    label = "S3-compatible folder (S3, MinIO, R2)"
    CONFIG = {"bucket": "Bucket name", "prefix": "Folder inside the bucket (optional, e.g. contracts/)",
              "endpoint_url": "Endpoint for non-AWS stores (e.g. http://minio:9000); empty for AWS",
              "region": "Region (default us-east-1)", "extensions": "File types to read (default: documents)",
              "max_file_kb": "Largest file read, in KB (default 20,000)"}
    SECRETS = {"access_key_id": "Access key id (read-only)", "secret_access_key": "Secret access key"}

    def __init__(self, cid, config, secrets, groups, s3_client=None):
        super().__init__(cid, config, secrets, groups)
        self._s3 = s3_client                       # tests pass a stand-in

    def _bucket(self) -> str:
        b = (self.config.get("bucket") or "").strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]", b):
            raise ValueError("Give the bucket name (lowercase letters, digits, dots and hyphens).")
        return b

    def _exts(self) -> set[str]:
        e = self.config.get("extensions") or DEFAULT_EXTS
        return {x.lower() if x.startswith(".") else f".{x.lower()}" for x in e}

    def client(self):
        from contextlib import nullcontext
        if self._s3 is None:
            kw = {"region_name": self.config.get("region") or "us-east-1"}
            if self.config.get("endpoint_url"):
                kw["endpoint_url"] = self.config["endpoint_url"]
            if self.secrets.get("access_key_id"):
                kw["aws_access_key_id"] = self.secrets["access_key_id"]
                kw["aws_secret_access_key"] = self.secrets.get("secret_access_key") or ""
            self._s3 = boto3.client("s3", **kw)
        return nullcontext(self._s3)

    def scopes(self) -> list[str]:
        return [f"{self._bucket()}/{(self.config.get('prefix') or '').lstrip('/')}"]

    def test(self) -> dict:
        with self.client() as s3:
            bucket, _, prefix = self.scopes()[0].partition("/")
            try:
                r = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=5)
            except Exception as e:                                   # noqa: BLE001 - shown to the admin
                return {"ok": False, "detail": f"{type(e).__name__}: {str(e)[:200]}"}
        keys = [o["Key"] for o in r.get("Contents") or []]
        return {"ok": True, "detail": f"Connected to {bucket}; first files: {', '.join(keys) or '(none yet)'}"}

    def listing(self, s3, scope: str, **ctx) -> dict[str, str]:
        bucket, _, prefix = scope.partition("/")
        exts, max_bytes = self._exts(), int(self.config.get("max_file_kb", 20000)) * 1024
        out: dict[str, str] = {}
        token = None
        while True:
            kw = {"Bucket": bucket, "Prefix": prefix}
            if token:
                kw["ContinuationToken"] = token
            r = s3.list_objects_v2(**kw)
            for o in r.get("Contents") or []:
                k = o["Key"]
                if k.endswith("/") or PurePosixPath(k).suffix.lower() not in exts or (o.get("Size") or 0) > max_bytes:
                    continue
                out[k] = str(o.get("ETag") or "").strip('"')
                if len(out) >= self.MAX_FILES:
                    return out
            if not r.get("IsTruncated"):
                return out
            token = r.get("NextContinuationToken")

    def fetch(self, s3, scope: str, key: str, version: str, **ctx) -> bytes | None:
        bucket = scope.partition("/")[0]
        return s3.get_object(Bucket=bucket, Key=key)["Body"].read()

    def doc_name(self, scope: str, key: str) -> str:
        bucket = scope.partition("/")[0]
        p = PurePosixPath(key)
        base = re.sub(r"[^\w.\- ()]+", "_", f"s3-{bucket}--{str(p.with_suffix('')).replace('/', '__')}")
        if len(base) > 130:
            base = base[:110] + "-" + hashlib.sha1(f"{bucket}/{key}".encode()).hexdigest()[:10]
        return base + p.suffix.lower()

    def ref(self, scope: str, key: str, version: str) -> str:
        return f"s3://{scope.partition('/')[0]}/{key}@{version}"
