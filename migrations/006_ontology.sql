-- The knowledge graph's ontology, per workspace: which kinds of things (entity types, with a simple hierarchy) and
-- links (relation types, with domain and range) the graph holds, and the extraction rules (spaCy label map, verb and
-- copula rules, head words, gazetteer). One row per definition (kind entity_type | relation_type | rule), versioned,
-- with the same statuses and approval policy as the semantic layer; every change is kept in onto_history.

CREATE TABLE IF NOT EXISTS onto_definitions (
  tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE, kind TEXT NOT NULL, name TEXT NOT NULL,
  definition JSONB NOT NULL, status TEXT NOT NULL DEFAULT 'approved', version INTEGER NOT NULL DEFAULT 1,
  owner TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT 'manual', note TEXT NOT NULL DEFAULT '',
  position INTEGER NOT NULL DEFAULT 0, updated_by TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
  PRIMARY KEY (tenant_id, kind, name),
  CHECK (kind IN ('entity_type', 'relation_type', 'rule')),
  CHECK (status IN ('draft', 'approved', 'needs_review', 'retired')));

CREATE TABLE IF NOT EXISTS onto_history (
  id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL, op TEXT NOT NULL,
  old_definition JSONB, old_status TEXT, new_status TEXT, changed_by TEXT NOT NULL DEFAULT '', changed_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS onto_history_item ON onto_history(tenant_id, kind, name, id);

CREATE TABLE IF NOT EXISTS onto_state (
  tenant_id TEXT PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE, version BIGINT NOT NULL DEFAULT 0,
  approved_hash TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL);

CREATE OR REPLACE FUNCTION onto_track() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE t TEXT := COALESCE(NEW.tenant_id, OLD.tenant_id);
BEGIN
  IF TG_OP = 'UPDATE' THEN
    IF NEW.definition IS DISTINCT FROM OLD.definition THEN NEW.version := OLD.version + 1; END IF;
    INSERT INTO onto_history (tenant_id, kind, name, op, old_definition, old_status, new_status, changed_by, changed_at)
      VALUES (t, OLD.kind, OLD.name, 'update', OLD.definition, OLD.status, NEW.status, NEW.updated_by, NEW.updated_at);
  ELSIF TG_OP = 'DELETE' THEN
    INSERT INTO onto_history (tenant_id, kind, name, op, old_definition, old_status, changed_by, changed_at)
      VALUES (t, OLD.kind, OLD.name, 'delete', OLD.definition, OLD.status, '', now()::text);
  ELSE
    INSERT INTO onto_history (tenant_id, kind, name, op, new_status, changed_by, changed_at)
      VALUES (t, NEW.kind, NEW.name, 'insert', NEW.status, NEW.updated_by, NEW.updated_at);
  END IF;
  INSERT INTO onto_state (tenant_id, version, updated_at) VALUES (t, 1, now()::text)
    ON CONFLICT (tenant_id) DO UPDATE SET version = onto_state.version + 1, updated_at = excluded.updated_at;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS onto_definitions_track ON onto_definitions;
CREATE TRIGGER onto_definitions_track BEFORE INSERT OR UPDATE OR DELETE ON onto_definitions
  FOR EACH ROW EXECUTE FUNCTION onto_track();

DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['onto_definitions', 'onto_history', 'onto_state'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS tenant_rows ON %I', t);
    EXECUTE format($p$CREATE POLICY tenant_rows ON %I USING (
        tenant_id = current_setting('app.tenant_id', true) OR current_setting('app.bypass', true) = 'on')
      WITH CHECK (tenant_id = current_setting('app.tenant_id', true) OR current_setting('app.bypass', true) = 'on')$p$, t);
  END LOOP;
END $$;
