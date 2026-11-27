-- The knowledge graph of each workspace as plain tables (graph/kg.py): the system of record for entities, links,
-- the passages behind them and communities. Answering loads it into an in-memory graph (graph/search.py), reloaded
-- when graph_state.version moves. Entity and community vectors stay in Qdrant. Apache AGE is no longer used.

CREATE TABLE IF NOT EXISTS kg_entities (
  tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE, id TEXT NOT NULL,
  name TEXT NOT NULL, type TEXT NOT NULL DEFAULT '', aliases JSONB NOT NULL DEFAULT '[]',
  description TEXT NOT NULL DEFAULT '', degree INTEGER NOT NULL DEFAULT 0, community_l0 INTEGER, community_l1 INTEGER,
  data JSONB NOT NULL DEFAULT '{}', PRIMARY KEY (tenant_id, id));
CREATE INDEX IF NOT EXISTS kg_entities_name ON kg_entities(tenant_id, lower(name));

CREATE TABLE IF NOT EXISTS kg_relations (
  tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE, position INTEGER NOT NULL,
  source TEXT NOT NULL, target TEXT NOT NULL, type TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
  strength DOUBLE PRECISION NOT NULL DEFAULT 0, passage_ids JSONB NOT NULL DEFAULT '[]', data JSONB NOT NULL DEFAULT '{}',
  PRIMARY KEY (tenant_id, position));
CREATE INDEX IF NOT EXISTS kg_relations_source ON kg_relations(tenant_id, source);
CREATE INDEX IF NOT EXISTS kg_relations_target ON kg_relations(tenant_id, target);

CREATE TABLE IF NOT EXISTS kg_entity_passages (
  tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE, entity_id TEXT NOT NULL, chunk_id TEXT NOT NULL,
  PRIMARY KEY (tenant_id, entity_id, chunk_id));
CREATE INDEX IF NOT EXISTS kg_entity_passages_chunk ON kg_entity_passages(tenant_id, chunk_id);

CREATE TABLE IF NOT EXISTS kg_communities (
  tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE, id INTEGER NOT NULL, level INTEGER NOT NULL DEFAULT 0,
  parent INTEGER, members JSONB NOT NULL DEFAULT '[]', profile TEXT NOT NULL DEFAULT '', data JSONB NOT NULL DEFAULT '{}',
  PRIMARY KEY (tenant_id, id));

CREATE TABLE IF NOT EXISTS graph_state (
  tenant_id TEXT PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE, version BIGINT NOT NULL DEFAULT 0,
  chunk_ids JSONB NOT NULL DEFAULT '[]', entities INTEGER NOT NULL DEFAULT 0, relations INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL);

DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['kg_entities', 'kg_relations', 'kg_entity_passages', 'kg_communities', 'graph_state'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS tenant_rows ON %I', t);
    EXECUTE format('CREATE POLICY tenant_rows ON %I USING (tenant_id = current_setting(''app.tenant_id'', true) '
                   'OR current_setting(''app.bypass'', true) = ''on'') WITH CHECK (tenant_id = '
                   'current_setting(''app.tenant_id'', true) OR current_setting(''app.bypass'', true) = ''on'')', t);
  END LOOP;
END $$;
