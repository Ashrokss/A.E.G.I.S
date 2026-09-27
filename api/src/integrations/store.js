/**
 * Jira / GitHub connections — PostgreSQL, OWNER-SCOPED
 * (migrations/003_integration_connections.sql).
 *
 * Every function takes ownerUserId first and every query filters on it: a
 * connection is its creator's, and nobody else can list, use or delete it.
 *
 * Secret fields (SECRET_FIELDS) are sealed with INTEGRATIONS_ENC_KEY before
 * they reach the `config` JSONB column. The key is REQUIRED: a bare Jira token
 * or GitHub PAT is directly usable by anyone who can read the row.
 */

const crypto = require('crypto');

const db = require('../db/pool');
const { makeSecretBox } = require('../utils/secretBox');

const { encryptSecret, decryptSecret, hasKey } = makeSecretBox(
  ['INTEGRATIONS_ENC_KEY'],
  { requireKey: true, warnLabel: 'INTEGRATIONS_ENC_KEY' },
);

// Secret fields per provider — sealed at rest, stripped by toPublicConnection.
const SECRET_FIELDS = {
  jira: ['apiToken', 'accessToken', 'refreshToken'],
  github: ['token', 'refreshToken'],
};

function sealConfig(provider, config) {
  const out = { ...config };
  for (const f of SECRET_FIELDS[provider] || []) if (out[f]) out[f] = encryptSecret(out[f]);
  return out;
}

function fromRow(row) {
  if (!row) return null;
  const config = { ...row.config };
  for (const f of SECRET_FIELDS[row.provider] || []) if (config[f]) config[f] = decryptSecret(config[f]);
  return {
    id: row.id,
    provider: row.provider,
    authMethod: row.auth_method,
    displayName: row.display_name,
    status: row.status,
    lastError: row.last_error,
    lastValidatedAt: row.last_validated_at ? new Date(row.last_validated_at).toISOString() : null,
    createdAt: new Date(row.created_at).toISOString(),
    updatedAt: new Date(row.updated_at).toISOString(),
    config,
  };
}

/** Public view — strips every secret field for the given provider. */
function toPublicConnection(c) {
  const config = { ...c.config };
  for (const f of SECRET_FIELDS[c.provider] || []) delete config[f];
  return { ...c, config };
}

async function listConnections(ownerUserId, provider) {
  const { rows } = await db.query(
    `SELECT * FROM integration_connections
      WHERE owner_user_id = $1 AND ($2::text IS NULL OR provider = $2)
      ORDER BY created_at DESC`,
    [ownerUserId, provider || null],
  );
  return rows.map(fromRow);
}

async function getConnection(ownerUserId, id) {
  const { rows } = await db.query(
    'SELECT * FROM integration_connections WHERE owner_user_id = $1 AND id = $2',
    [ownerUserId, id],
  );
  return fromRow(rows[0]);
}

async function createConnection(ownerUserId, { provider, authMethod, displayName, config }) {
  if (!hasKey()) {
    const err = new Error('Integration secrets cannot be stored: set INTEGRATIONS_ENC_KEY (32-byte hex/base64) on the api service.');
    err.code = 'no_encryption_key';
    throw err;
  }
  const id = `${provider}_${crypto.randomBytes(8).toString('hex')}`;
  const { rows } = await db.query(
    `INSERT INTO integration_connections
       (id, owner_user_id, provider, auth_method, display_name, status, last_validated_at, config)
     VALUES ($1, $2, $3, $4, $5, 'active', now(), $6)
     RETURNING *`,
    [id, ownerUserId, provider, authMethod, displayName || null, sealConfig(provider, config || {})],
  );
  return fromRow(rows[0]);
}

/** Status bookkeeping after a connection test. Config is not patchable. */
async function updateStatus(ownerUserId, id, { status, lastError = null, validated = false }) {
  await db.query(
    `UPDATE integration_connections
        SET status = $3, last_error = $4, updated_at = now(),
            last_validated_at = CASE WHEN $5 THEN now() ELSE last_validated_at END
      WHERE owner_user_id = $1 AND id = $2`,
    [ownerUserId, id, status, lastError, validated],
  );
}

async function deleteConnection(ownerUserId, id) {
  const { rowCount } = await db.query(
    'DELETE FROM integration_connections WHERE owner_user_id = $1 AND id = $2',
    [ownerUserId, id],
  );
  return rowCount > 0;
}

module.exports = {
  listConnections,
  getConnection,
  createConnection,
  updateStatus,
  deleteConnection,
  toPublicConnection,
  hasEncryptionKey: hasKey,
  SECRET_FIELDS,
};
