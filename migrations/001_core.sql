-- Context Layer core schema: workspaces (tenants), people, keys, sign-in, audit, settings.
-- Timestamps are ISO-8601 UTC text (same format everywhere, so text comparison orders correctly).
-- Tables holding a workspace's own data carry tenant_id and are protected by row-level security:
-- a request only sees rows of the workspace it runs in (app.tenant_id), unless the app is doing a
-- system lookup (app.bypass, e.g. "which workspace does this key belong to?").

CREATE TABLE IF NOT EXISTS tenants (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, created TEXT NOT NULL, disabled BOOLEAN NOT NULL DEFAULT false);

CREATE TABLE IF NOT EXISTS users (
  id BIGSERIAL PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL, pw_hash TEXT NOT NULL,
  created TEXT NOT NULL, last_login TEXT, disabled INTEGER NOT NULL DEFAULT 0);

-- a person's role and access groups are per workspace
CREATE TABLE IF NOT EXISTS memberships (
  user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  role TEXT NOT NULL, groups TEXT NOT NULL DEFAULT '["public"]', disabled INTEGER NOT NULL DEFAULT 0,
  created TEXT NOT NULL, seq BIGSERIAL, PRIMARY KEY (user_id, tenant_id));
CREATE INDEX IF NOT EXISTS memberships_tenant ON memberships(tenant_id);

CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE, created TEXT NOT NULL, expires TEXT NOT NULL);

-- scope: agent = asks questions (capabilities documents/data/graph/upload); admin = also sets the workspace up
CREATE TABLE IF NOT EXISTS agents (
  id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', groups TEXT NOT NULL, capabilities TEXT NOT NULL,
  scope TEXT NOT NULL DEFAULT 'agent', token_hash TEXT UNIQUE NOT NULL, prefix TEXT NOT NULL, created TEXT NOT NULL,
  created_by TEXT NOT NULL, last_used TEXT, calls INTEGER NOT NULL DEFAULT 0, revoked TEXT);
CREATE INDEX IF NOT EXISTS agents_tenant ON agents(tenant_id);

CREATE TABLE IF NOT EXISTS events (
  id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL, time TEXT NOT NULL, actor_type TEXT NOT NULL,
  actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '{}',
  ok INTEGER NOT NULL DEFAULT 1, seconds DOUBLE PRECISION);
CREATE INDEX IF NOT EXISTS events_time ON events(tenant_id, time);
CREATE INDEX IF NOT EXISTS events_actor ON events(tenant_id, actor_type, actor);

CREATE TABLE IF NOT EXISTS settings (
  tenant_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY (tenant_id, key));

-- server-wide values (e.g. the OAuth signing secret); not readable through a workspace
CREATE TABLE IF NOT EXISTS system_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS oauth_clients (
  client_id TEXT PRIMARY KEY, secret_hash TEXT, name TEXT NOT NULL, redirect_uris TEXT NOT NULL,
  kind TEXT NOT NULL, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS oauth_codes (
  code_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  redirect_uri TEXT NOT NULL, challenge TEXT NOT NULL, scope TEXT NOT NULL, resource TEXT, expires TEXT NOT NULL,
  tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS oauth_tokens (
  token_hash TEXT PRIMARY KEY, kind TEXT NOT NULL, family TEXT NOT NULL, client_id TEXT NOT NULL,
  client_name TEXT NOT NULL, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE, scope TEXT NOT NULL,
  resource TEXT, created TEXT NOT NULL, expires TEXT NOT NULL, revoked TEXT, last_used TEXT,
  tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS oauth_tokens_family ON oauth_tokens(family);

CREATE TABLE IF NOT EXISTS upload_links (
  token_hash TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  principal TEXT NOT NULL, groups TEXT NOT NULL, created TEXT NOT NULL, expires TEXT NOT NULL,
  files INTEGER NOT NULL DEFAULT 0);

-- row-level security for workspace tables
DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['agents', 'events', 'settings', 'upload_links'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS tenant_rows ON %I', t);
    EXECUTE format($p$CREATE POLICY tenant_rows ON %I USING (
        tenant_id = current_setting('app.tenant_id', true) OR current_setting('app.bypass', true) = 'on')
      WITH CHECK (tenant_id = current_setting('app.tenant_id', true) OR current_setting('app.bypass', true) = 'on')$p$, t);
  END LOOP;
END $$;
