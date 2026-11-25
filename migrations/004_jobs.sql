-- Background work (document, data and graph builds, connector syncs, checks), queued in Postgres so any app
-- process can run it and nothing is lost on a restart. One job at a time per workspace; workers claim jobs with
-- FOR UPDATE SKIP LOCKED. status: queued, running, done, failed.

CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE, kind TEXT NOT NULL,
  label TEXT NOT NULL, status TEXT NOT NULL, info JSONB NOT NULL DEFAULT '{}', by TEXT NOT NULL DEFAULT '',
  created TEXT NOT NULL, started TEXT, finished TEXT, heartbeat TEXT, worker TEXT, error TEXT,
  log TEXT NOT NULL DEFAULT '', seq BIGSERIAL);
CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, seq);
CREATE INDEX IF NOT EXISTS jobs_tenant ON jobs(tenant_id, seq);

-- questions the layer could not serve well (no metric matched, no evidence): input for suggested synonyms
CREATE TABLE IF NOT EXISTS sl_gaps (
  tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE, question TEXT NOT NULL, reason TEXT NOT NULL,
  count INTEGER NOT NULL DEFAULT 1, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
  PRIMARY KEY (tenant_id, question, reason));

DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['jobs', 'sl_gaps'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS tenant_rows ON %I', t);
    EXECUTE format($p$CREATE POLICY tenant_rows ON %I USING (
        tenant_id = current_setting('app.tenant_id', true) OR current_setting('app.bypass', true) = 'on')
      WITH CHECK (tenant_id = current_setting('app.tenant_id', true) OR current_setting('app.bypass', true) = 'on')$p$, t);
  END LOOP;
END $$;
