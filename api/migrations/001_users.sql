-- AEGIS application users.
--
-- Cognito authenticates; THIS TABLE authorizes. A row here is what makes a
-- Cognito identity a AEGIS user — the backend never derives a role from a
-- JWT claim or a Cognito group.
--
-- cognito_sub is the join key, not email: Cognito's `sub` is immutable, while an
-- email can be changed in the pool (and is re-synced here on sign-in).
--
-- Written idempotently (IF NOT EXISTS throughout) so it is safe against a
-- database where an earlier run already created the table.

CREATE TABLE IF NOT EXISTS users (
  id           UUID        PRIMARY KEY,
  cognito_sub  TEXT        NOT NULL UNIQUE,
  email        TEXT        NOT NULL UNIQUE,
  role         TEXT        NOT NULL CHECK (role IN ('admin', 'user')),
  enabled      BOOLEAN     NOT NULL DEFAULT TRUE,
  last_login   TIMESTAMPTZ NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The application lowercases every email before it writes or looks one up
-- (users/store.js normalizeEmail), so the case-insensitive uniqueness has to be
-- enforced here too. Without it "A@b.test" and "a@b.test" are two rows, and the
-- second one is a user nobody can sign in as.
CREATE UNIQUE INDEX IF NOT EXISTS users_email_lower_idx ON users (lower(email));

-- Serves the Admin Portal's roster ordering. The lookup on every authenticated
-- request goes through the cognito_sub UNIQUE constraint's own index.
CREATE INDEX IF NOT EXISTS users_created_at_idx ON users (created_at DESC);
