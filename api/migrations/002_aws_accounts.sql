-- AWS accounts a user has onboarded as provisioning targets.
--
-- OWNED rows: every read and write is scoped by owner_user_id in SQL
-- (src/aws/accountStore.js), never by a guard in a route. That is what lets a
-- plain `user` provision into their OWN accounts and nobody else's.
--
-- A row is created 'pending' by /api/aws-accounts/start, holding a server-
-- generated ExternalId, so the ExternalId is never chosen by the browser (the
-- confused-deputy protection STS ExternalIds exist for). /complete binds the
-- role ARN and flips it to 'active'.
--
-- external_id_enc is sealed with AWS_ONBOARDING_ENC_KEY (src/utils/secretBox.js).

CREATE TABLE IF NOT EXISTS aws_accounts (
  id               UUID        PRIMARY KEY,
  owner_user_id    UUID        NOT NULL REFERENCES users(id),
  status           TEXT        NOT NULL CHECK (status IN ('pending', 'active')),
  account_id       TEXT        NULL CHECK (account_id ~ '^[0-9]{12}$'),
  role_arn         TEXT        NULL,
  external_id_enc  TEXT        NOT NULL,
  display_name     TEXT        NULL,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (status = 'pending' OR (account_id IS NOT NULL AND role_arn IS NOT NULL))
);

-- One active binding per (owner, AWS account); re-onboarding replaces it.
CREATE UNIQUE INDEX IF NOT EXISTS aws_accounts_owner_account_idx
  ON aws_accounts (owner_user_id, account_id) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS aws_accounts_owner_idx ON aws_accounts (owner_user_id, created_at DESC);
