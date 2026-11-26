-- Typed facts read from documents (contracts, invoices, meeting notes...), with provenance, and one card per
-- document. Facts are approved (agents may use them), needs_review (an owner decides) or rejected.

CREATE TABLE IF NOT EXISTS facts (
  id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  document TEXT NOT NULL, kind TEXT NOT NULL, record TEXT NOT NULL DEFAULT '', field TEXT NOT NULL,
  value TEXT NOT NULL, norm JSONB, chunk_id TEXT NOT NULL, quote TEXT NOT NULL, span_start INTEGER NOT NULL,
  span_end INTEGER NOT NULL, extractor TEXT NOT NULL, confidence DOUBLE PRECISION NOT NULL,
  status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', decided_by TEXT, updated TEXT NOT NULL,
  CHECK (status IN ('approved', 'needs_review', 'rejected')));
CREATE INDEX IF NOT EXISTS facts_doc ON facts(tenant_id, document);
CREATE INDEX IF NOT EXISTS facts_status ON facts(tenant_id, status);

CREATE TABLE IF NOT EXISTS doc_cards (
  tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE, name TEXT NOT NULL, kind TEXT NOT NULL,
  card JSONB NOT NULL, updated TEXT NOT NULL, PRIMARY KEY (tenant_id, name));

DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['facts', 'doc_cards'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS tenant_rows ON %I', t);
    EXECUTE format($p$CREATE POLICY tenant_rows ON %I USING (
        tenant_id = current_setting('app.tenant_id', true) OR current_setting('app.bypass', true) = 'on')
      WITH CHECK (tenant_id = current_setting('app.tenant_id', true) OR current_setting('app.bypass', true) = 'on')$p$, t);
  END LOOP;
END $$;
