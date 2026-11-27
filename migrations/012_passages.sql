-- Passages (chunks) of each workspace's documents: the system of record for document text (rag/passages.py).
-- Written by the chunking step, read by search (texts for re-ranking and evidence), the graph build, facts and the
-- "answerable?" check. The search index (Qdrant) keeps only ids and filter fields, so it can be rebuilt from here.
-- passages_state.version moves on every change, so caches know when to reload.

CREATE TABLE IF NOT EXISTS passages (
  tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  chunk_id TEXT NOT NULL, source TEXT NOT NULL, position INTEGER NOT NULL DEFAULT 0,
  headings JSONB NOT NULL DEFAULT '[]', pages JSONB NOT NULL DEFAULT '[]',
  text TEXT NOT NULL, embed_text TEXT NOT NULL DEFAULT '', n_tokens INTEGER, text_hash TEXT NOT NULL,
  access JSONB NOT NULL DEFAULT '["public"]',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY (tenant_id, chunk_id));
CREATE INDEX IF NOT EXISTS passages_source ON passages(tenant_id, source, position);

CREATE TABLE IF NOT EXISTS passages_state (
  tenant_id TEXT PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
  version BIGINT NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);

ALTER TABLE passages ENABLE ROW LEVEL SECURITY;
ALTER TABLE passages FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_rows ON passages;
CREATE POLICY tenant_rows ON passages USING (
    tenant_id = current_setting('app.tenant_id', true) OR current_setting('app.bypass', true) = 'on')
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true) OR current_setting('app.bypass', true) = 'on');

ALTER TABLE passages_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE passages_state FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_rows ON passages_state;
CREATE POLICY tenant_rows ON passages_state USING (
    tenant_id = current_setting('app.tenant_id', true) OR current_setting('app.bypass', true) = 'on')
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true) OR current_setting('app.bypass', true) = 'on');
