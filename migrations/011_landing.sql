-- The landing manifest: one row per file that landed in a workspace's landing zone (data/ tables as Parquet,
-- documents/ originals), whoever pushed it (a connector sync, a web upload, an upload link, an agent's
-- add_document, the 0.7 backfill). A replaced or removed file keeps its row with replaced_at set, so the history
-- of what arrived, from where and when is never lost. app/landing.py writes it.

CREATE TABLE IF NOT EXISTS landing_files (
  id BIGSERIAL PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  kind TEXT NOT NULL, name TEXT NOT NULL, path TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'upload', connector_id TEXT, pushed_by TEXT NOT NULL DEFAULT '',
  groups JSONB NOT NULL DEFAULT '[]', sha256 TEXT NOT NULL, bytes BIGINT NOT NULL DEFAULT 0,
  rows BIGINT, schema JSONB, landed_at TEXT NOT NULL, replaced_at TEXT,
  CHECK (kind IN ('table', 'document')));
CREATE INDEX IF NOT EXISTS landing_current ON landing_files(tenant_id, kind, name) WHERE replaced_at IS NULL;
CREATE INDEX IF NOT EXISTS landing_time ON landing_files(tenant_id, landed_at);

ALTER TABLE landing_files ENABLE ROW LEVEL SECURITY;
ALTER TABLE landing_files FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_rows ON landing_files;
CREATE POLICY tenant_rows ON landing_files USING (
    tenant_id = current_setting('app.tenant_id', true) OR current_setting('app.bypass', true) = 'on')
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true) OR current_setting('app.bypass', true) = 'on');
