-- The documents of each workspace: who may see them (access groups) and where they came from (an upload or a
-- connector). The files themselves are in the workspace's file store (CL_FILES_DIR/<workspace>/documents).

CREATE TABLE IF NOT EXISTS documents (
  tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE, name TEXT NOT NULL,
  groups TEXT NOT NULL DEFAULT '["public"]', source TEXT NOT NULL DEFAULT 'upload', source_ref TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL, PRIMARY KEY (tenant_id, name));

ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_rows ON documents;
CREATE POLICY tenant_rows ON documents USING (
    tenant_id = current_setting('app.tenant_id', true) OR current_setting('app.bypass', true) = 'on')
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true) OR current_setting('app.bypass', true) = 'on');
