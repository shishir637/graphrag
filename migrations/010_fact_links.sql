-- Facts linked to database rows ("Northwind Technologies Ltd" -> vendor_master V-4471), with how sure the match is.
CREATE TABLE IF NOT EXISTS fact_links (
  tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE, fact_id TEXT NOT NULL, entity TEXT NOT NULL,
  row_key TEXT NOT NULL, label TEXT NOT NULL, score DOUBLE PRECISION NOT NULL, method TEXT NOT NULL,
  status TEXT NOT NULL, decided_by TEXT, updated TEXT NOT NULL, PRIMARY KEY (tenant_id, fact_id),
  CHECK (status IN ('approved', 'needs_review', 'rejected')));
ALTER TABLE fact_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE fact_links FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_rows ON fact_links;
CREATE POLICY tenant_rows ON fact_links USING (
    tenant_id = current_setting('app.tenant_id', true) OR current_setting('app.bypass', true) = 'on')
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true) OR current_setting('app.bypass', true) = 'on');
