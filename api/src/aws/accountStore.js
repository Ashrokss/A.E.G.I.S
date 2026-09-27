/**
 * Onboarded AWS accounts — PostgreSQL, OWNER-SCOPED (migrations/002_aws_accounts.sql).
 *
 * Every function takes ownerUserId first and every query filters on it, so a
 * route cannot forget the ownership check: there is no unscoped read to call.
 *
 * The ExternalId is generated here (createPending), sealed with
 * AWS_ONBOARDING_ENC_KEY, and never accepted from the client. The key is
 * REQUIRED — an AEGIS deployment never stores an ExternalId in plaintext.
 */

const crypto = require('crypto');

const db = require('../db/pool');
const { makeSecretBox } = require('../utils/secretBox');

const { encryptSecret, decryptSecret } = makeSecretBox(['AWS_ONBOARDING_ENC_KEY'], {
  requireKey: true,
  warnLabel: 'AWS_ONBOARDING_ENC_KEY',
});

// Abandoned onboarding attempts are cleaned up on the next /start.
const PENDING_TTL = "interval '1 day'";

/**
 * AegisProvisioner-<suffix> -> the Terraform state bucket the same
 * CloudFormation stack creates: aegis-tfstate-<account>-<suffix>.
 */
function stateBucketFor(roleArn) {
  const m = /^arn:aws(?:-cn|-us-gov)?:iam::(\d{12}):role\/(?:.*\/)?AegisProvisioner-([a-z0-9-]+)$/.exec(roleArn || '');
  return m ? `aegis-tfstate-${m[1]}-${m[2]}` : undefined;
}

/** Public view — never exposes the ExternalId. */
function toPublicAccount(row) {
  return {
    id: row.id,
    status: row.status,
    accountId: row.account_id,
    roleArn: row.role_arn,
    stateBucket: stateBucketFor(row.role_arn) || null,
    displayName: row.display_name,
    createdAt: new Date(row.created_at).toISOString(),
  };
}

async function createPending(ownerUserId) {
  await db.query(
    `DELETE FROM aws_accounts WHERE owner_user_id = $1 AND status = 'pending' AND created_at < now() - ${PENDING_TTL}`,
    [ownerUserId],
  );
  const id = crypto.randomUUID();
  const externalId = crypto.randomBytes(16).toString('hex');
  await db.query(
    `INSERT INTO aws_accounts (id, owner_user_id, status, external_id_enc) VALUES ($1, $2, 'pending', $3)`,
    [id, ownerUserId, encryptSecret(externalId)],
  );
  return { id, externalId };
}

/** The pending row's ExternalId, for validating the role before activation. */
async function getPending(ownerUserId, id) {
  const { rows } = await db.query(
    `SELECT external_id_enc FROM aws_accounts WHERE id = $1 AND owner_user_id = $2 AND status = 'pending'`,
    [id, ownerUserId],
  );
  return rows[0] ? { externalId: decryptSecret(rows[0].external_id_enc) } : null;
}

/** pending → active. Replaces an earlier active binding of the same account. */
async function activate(ownerUserId, id, { accountId, roleArn, displayName }) {
  const client = await db.connect();
  try {
    await client.query('BEGIN');
    await client.query(
      `DELETE FROM aws_accounts WHERE owner_user_id = $1 AND account_id = $2 AND status = 'active' AND id <> $3`,
      [ownerUserId, accountId, id],
    );
    const { rows } = await client.query(
      `UPDATE aws_accounts
          SET status = 'active', account_id = $3, role_arn = $4, display_name = $5, updated_at = now()
        WHERE id = $1 AND owner_user_id = $2 AND status = 'pending'
        RETURNING *`,
      [id, ownerUserId, accountId, roleArn, displayName || null],
    );
    await client.query('COMMIT');
    return rows[0] || null;
  } catch (err) {
    await client.query('ROLLBACK').catch(() => {});
    throw err;
  } finally {
    client.release();
  }
}

async function listAccounts(ownerUserId) {
  const { rows } = await db.query(
    `SELECT * FROM aws_accounts WHERE owner_user_id = $1 AND status = 'active' ORDER BY created_at DESC`,
    [ownerUserId],
  );
  return rows;
}

/** Role ARN + ExternalId for assumeRole(), only for the caller's own account. */
async function getAccountCreds(ownerUserId, accountId) {
  const { rows } = await db.query(
    `SELECT role_arn, external_id_enc FROM aws_accounts
      WHERE owner_user_id = $1 AND account_id = $2 AND status = 'active'`,
    [ownerUserId, accountId],
  );
  if (!rows[0]) return null;
  return { roleArn: rows[0].role_arn, externalId: decryptSecret(rows[0].external_id_enc) };
}

async function deleteAccount(ownerUserId, accountId) {
  const { rowCount } = await db.query(
    `DELETE FROM aws_accounts WHERE owner_user_id = $1 AND account_id = $2`,
    [ownerUserId, accountId],
  );
  return rowCount > 0;
}

module.exports = {
  createPending, getPending, activate, listAccounts, getAccountCreds, deleteAccount, toPublicAccount, stateBucketFor,
};
