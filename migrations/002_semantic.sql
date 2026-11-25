-- The semantic layer, per workspace: what the business means by its numbers.
-- One row per definition, versioned. kind:
--   entity        a table and its dimensions and measures            name = entity name
--   join          how two entities connect                           name = from.col->to.col
--   metric        a business number and its formula                  name = metric name
--   synonym       an everyday word for a metric, dimension or value  name = term=>target
--   value_list    a dimension whose known values are stored          name = entity.dimension
--   verified      a question with its approved request (example)     name = the question
--   instructions  business rules in plain words                      name = main
-- status: draft (not used by agents), approved, needs_review (failed a check; hidden), retired.
-- source: template, import, auto_draft, connector, manual. Every change is kept in sl_history.

CREATE TABLE IF NOT EXISTS sl_definitions (
  tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  kind TEXT NOT NULL, name TEXT NOT NULL, domain TEXT NOT NULL DEFAULT 'general',
  definition JSONB NOT NULL, status TEXT NOT NULL DEFAULT 'approved', version INTEGER NOT NULL DEFAULT 1,
  owner TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT 'manual', note TEXT NOT NULL DEFAULT '',
  position INTEGER NOT NULL DEFAULT 0, updated_by TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
  PRIMARY KEY (tenant_id, kind, name),
  CHECK (kind IN ('entity', 'join', 'metric', 'synonym', 'value_list', 'verified', 'instructions')),
  CHECK (status IN ('draft', 'approved', 'needs_review', 'retired')));
CREATE INDEX IF NOT EXISTS sl_definitions_status ON sl_definitions(tenant_id, kind, status);

CREATE TABLE IF NOT EXISTS sl_history (
  id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL, op TEXT NOT NULL,
  old_definition JSONB, old_status TEXT, old_version INTEGER, new_status TEXT, new_version INTEGER,
  changed_by TEXT NOT NULL DEFAULT '', changed_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS sl_history_item ON sl_history(tenant_id, kind, name, id);

-- known values of dimensions (computed from the data, used to recognise "Mumbai" in a question)
CREATE TABLE IF NOT EXISTS sl_value_lists (
  tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE, dimension TEXT NOT NULL, value TEXT NOT NULL,
  PRIMARY KEY (tenant_id, dimension, value));

-- one counter per workspace, bumped on every change: caches compare it to know they are current
CREATE TABLE IF NOT EXISTS sl_state (
  tenant_id TEXT PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE, version BIGINT NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL);

CREATE OR REPLACE FUNCTION sl_track() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE t TEXT := COALESCE(NEW.tenant_id, OLD.tenant_id);
BEGIN
  IF TG_OP = 'UPDATE' THEN
    IF NEW.definition IS DISTINCT FROM OLD.definition THEN
      NEW.version := OLD.version + 1;
    END IF;
    INSERT INTO sl_history (tenant_id, kind, name, op, old_definition, old_status, old_version, new_status,
                            new_version, changed_by, changed_at)
      VALUES (t, OLD.kind, OLD.name, 'update', OLD.definition, OLD.status, OLD.version, NEW.status, NEW.version,
              NEW.updated_by, NEW.updated_at);
  ELSIF TG_OP = 'DELETE' THEN
    INSERT INTO sl_history (tenant_id, kind, name, op, old_definition, old_status, old_version, changed_by, changed_at)
      VALUES (t, OLD.kind, OLD.name, 'delete', OLD.definition, OLD.status, OLD.version,
              COALESCE(current_setting('app.actor', true), ''), now()::text);
  ELSE
    INSERT INTO sl_history (tenant_id, kind, name, op, new_status, new_version, changed_by, changed_at)
      VALUES (t, NEW.kind, NEW.name, 'insert', NEW.status, NEW.version, NEW.updated_by, NEW.updated_at);
  END IF;
  INSERT INTO sl_state (tenant_id, version, updated_at) VALUES (t, 1, now()::text)
    ON CONFLICT (tenant_id) DO UPDATE SET version = sl_state.version + 1, updated_at = excluded.updated_at;
  PERFORM pg_notify('sl_changed', t);
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS sl_definitions_track ON sl_definitions;
CREATE TRIGGER sl_definitions_track BEFORE INSERT OR UPDATE OR DELETE ON sl_definitions
  FOR EACH ROW EXECUTE FUNCTION sl_track();

CREATE OR REPLACE FUNCTION sl_values_track() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE t TEXT := COALESCE(NEW.tenant_id, OLD.tenant_id);
BEGIN
  INSERT INTO sl_state (tenant_id, version, updated_at) VALUES (t, 1, now()::text)
    ON CONFLICT (tenant_id) DO UPDATE SET version = sl_state.version + 1, updated_at = excluded.updated_at;
  RETURN NULL;
END $$;

DROP TRIGGER IF EXISTS sl_value_lists_track ON sl_value_lists;
CREATE TRIGGER sl_value_lists_track AFTER INSERT OR DELETE ON sl_value_lists
  FOR EACH ROW EXECUTE FUNCTION sl_values_track();

DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['sl_definitions', 'sl_history', 'sl_value_lists', 'sl_state'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS tenant_rows ON %I', t);
    EXECUTE format($p$CREATE POLICY tenant_rows ON %I USING (
        tenant_id = current_setting('app.tenant_id', true) OR current_setting('app.bypass', true) = 'on')
      WITH CHECK (tenant_id = current_setting('app.tenant_id', true) OR current_setting('app.bypass', true) = 'on')$p$, t);
  END LOOP;
END $$;
