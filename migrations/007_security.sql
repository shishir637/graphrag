-- Security hardening.
-- Keys: optional expiry and IP allow-list (CIDR). Scope "delegate": an app's backend acting for its own end users,
-- who are identified per request and mapped to external users with their own groups (never the key's union).

ALTER TABLE agents ADD COLUMN IF NOT EXISTS expires TEXT;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS allowed_ips TEXT NOT NULL DEFAULT '[]';

CREATE TABLE IF NOT EXISTS external_users (
  tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE, user_id TEXT NOT NULL,
  groups TEXT NOT NULL DEFAULT '[]', name TEXT NOT NULL DEFAULT '', disabled INTEGER NOT NULL DEFAULT 0,
  created TEXT NOT NULL, updated TEXT NOT NULL, PRIMARY KEY (tenant_id, user_id));

-- one-time signed identities already used (replay protection), kept for the validity window
CREATE TABLE IF NOT EXISTS used_signatures (sig TEXT PRIMARY KEY, expires TEXT NOT NULL);

ALTER TABLE external_users ENABLE ROW LEVEL SECURITY;
ALTER TABLE external_users FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_rows ON external_users;
CREATE POLICY tenant_rows ON external_users USING (
    tenant_id = current_setting('app.tenant_id', true) OR current_setting('app.bypass', true) = 'on')
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true) OR current_setting('app.bypass', true) = 'on');
