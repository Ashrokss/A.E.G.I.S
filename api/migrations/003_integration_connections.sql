-- GitHub / Jira connections, OWNED by the user who created them.
--
-- Scoped by owner_user_id in SQL (src/integrations/store.js). `config` is the
-- provider config as JSON; its secret fields (store.SECRET_FIELDS) are sealed
-- with INTEGRATIONS_ENC_KEY before they reach this table, never stored plain.
--
-- id keeps the `<provider>_<hex>` shape the agent already persists on runs.

CREATE TABLE IF NOT EXISTS integration_connections (
  id                 TEXT        PRIMARY KEY,
  owner_user_id      UUID        NOT NULL REFERENCES users(id),
  provider           TEXT        NOT NULL CHECK (provider IN ('jira', 'github')),
  auth_method        TEXT        NOT NULL,
  display_name       TEXT        NULL,
  status             TEXT        NOT NULL DEFAULT 'active',
  last_error         TEXT        NULL,
  last_validated_at  TIMESTAMPTZ NULL,
  config             JSONB       NOT NULL DEFAULT '{}'::jsonb,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS integration_connections_owner_idx
  ON integration_connections (owner_user_id, provider, created_at DESC);
