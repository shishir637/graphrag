-- Sources a workspace connects once and that are then kept up to date on their own (GitHub, a database...),
-- and the workspace's secrets (connector tokens, database passwords, AI keys), encrypted by the app
-- (Fernet, key CL_SECRET_KEY) before they reach the database.

CREATE TABLE IF NOT EXISTS connectors (
  id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE, kind TEXT NOT NULL,
  name TEXT NOT NULL, config JSONB NOT NULL DEFAULT '{}', groups TEXT NOT NULL DEFAULT '["public"]',
  every_minutes INTEGER NOT NULL DEFAULT 360, state JSONB NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'active',
  last_run TEXT, last_ok TEXT, last_error TEXT, next_run TEXT, created TEXT NOT NULL, created_by TEXT NOT NULL,
  CHECK (status IN ('active', 'paused')));
CREATE INDEX IF NOT EXISTS connectors_due ON connectors(status, next_run);

CREATE TABLE IF NOT EXISTS secrets (
  tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE, name TEXT NOT NULL, ciphertext TEXT NOT NULL,
  hint TEXT NOT NULL DEFAULT '', updated TEXT NOT NULL, PRIMARY KEY (tenant_id, name));

DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['connectors', 'secrets'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS tenant_rows ON %I', t);
    EXECUTE format($p$CREATE POLICY tenant_rows ON %I USING (
        tenant_id = current_setting('app.tenant_id', true) OR current_setting('app.bypass', true) = 'on')
      WITH CHECK (tenant_id = current_setting('app.tenant_id', true) OR current_setting('app.bypass', true) = 'on')$p$, t);
  END LOOP;
END $$;

-- Row access for metrics: callers in `grp` only see rows of `entity` whose `col` is one of `vals`. An entity with
-- rules is closed to callers who match none of them (e.g. GitHub tables are filtered to the caller's repositories).
CREATE TABLE IF NOT EXISTS sl_access_rules (
  tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE, entity TEXT NOT NULL, col TEXT NOT NULL,
  grp TEXT NOT NULL, vals JSONB NOT NULL, source TEXT NOT NULL DEFAULT 'manual', updated TEXT NOT NULL,
  PRIMARY KEY (tenant_id, entity, col, grp));
ALTER TABLE sl_access_rules ENABLE ROW LEVEL SECURITY;
ALTER TABLE sl_access_rules FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_rows ON sl_access_rules;
CREATE POLICY tenant_rows ON sl_access_rules USING (
    tenant_id = current_setting('app.tenant_id', true) OR current_setting('app.bypass', true) = 'on')
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true) OR current_setting('app.bypass', true) = 'on');
