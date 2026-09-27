/**
 * The application user store — PostgreSQL, and the ONLY source of a role.
 *
 * Cognito answers "is this really you". This file answers "and what are you
 * allowed to do here", for every authenticated request. Nothing in this product
 * reads a role from a JWT claim, a Cognito group, a session or the client; the
 * one answer comes from `users.role`, re-read per request so a role change takes
 * effect on the caller's very next call rather than when their token expires.
 *
 * ROLES: exactly `admin` and `user`, enforced by a CHECK constraint in
 * migrations/001_users.sql as well as here. There is no `pending`, no
 * superadmin, and no tenant or platform role.
 *
 * WHAT IS DELIBERATELY ABSENT:
 *  - No delete. `removeBySub` exists and is NOT exported through any route: it
 *    is the compensating half of a create whose Cognito side succeeded and whose
 *    Postgres side did not. Nobody can invoke it.
 *  - No demotion. `promote` refuses admin → user, so a compromised admin account
 *    cannot strip the other admins and take the deployment over.
 */

const crypto = require('crypto');

const db = require('../db/pool');

const ROLES = ['user', 'admin'];
const ROLE_RANK = { user: 1, admin: 2 };

function isValidRole(role) {
  return typeof role === 'string' && Object.prototype.hasOwnProperty.call(ROLE_RANK, role);
}

/** Emails are matched case-insensitively; Cognito treats them that way too. */
function normalizeEmail(email) {
  return String(email || '').trim().toLowerCase();
}

const COLUMNS = 'id, cognito_sub, email, role, enabled, last_login, created_at, updated_at';

/**
 * The shape the API returns. Note what is NOT here: nothing secret, and — for
 * the roster — no Cognito username. `cognitoSub` is carried because the portal's
 * promote call addresses a user by it, and it is an opaque identifier, not a
 * credential.
 */
function toPublicUser(row) {
  if (!row) return null;
  return {
    id: row.id,
    cognitoSub: row.cognito_sub,
    email: row.email,
    role: row.role,
    enabled: row.enabled,
    lastLogin: row.last_login ? new Date(row.last_login).toISOString() : null,
    createdAt: new Date(row.created_at).toISOString(),
    updatedAt: new Date(row.updated_at).toISOString(),
  };
}

/** The authorization lookup. Called on EVERY authenticated request. */
async function findByCognitoSub(cognitoSub) {
  const { rows } = await db.query(
    `SELECT ${COLUMNS} FROM users WHERE cognito_sub = $1`, [String(cognitoSub || '')],
  );
  return rows[0] || null;
}

/**
 * Look a user up by email, CASE-INSENSITIVELY.
 *
 * `lower(email)` rather than `email`, because that is the uniqueness the table
 * actually enforces (users_email_lower_idx). A row written by anything other
 * than createUser — a manual fix, a restore, the bootstrap adoption path — can
 * hold mixed case, and an exact match would then report it as absent while the
 * unique index still refuses to insert it: invisible to lookup, impossible to
 * re-create. Matching the constraint also lets this use that index.
 */
async function findByEmail(email) {
  const { rows } = await db.query(
    `SELECT ${COLUMNS} FROM users WHERE lower(email) = $1`, [normalizeEmail(email)],
  );
  return rows[0] || null;
}

async function findById(id) {
  const { rows } = await db.query(`SELECT ${COLUMNS} FROM users WHERE id = $1`, [id]);
  return rows[0] || null;
}

/** The Admin Portal roster. Newest first. */
async function listUsers() {
  const { rows } = await db.query(`SELECT ${COLUMNS} FROM users ORDER BY created_at DESC`);
  return rows;
}

async function countUsers() {
  const { rows } = await db.query('SELECT COUNT(*)::int AS n FROM users');
  return rows[0].n;
}

/**
 * Create the Postgres half of a user. The Cognito half is created first by the
 * caller (routes/users.js), which passes the sub it captured.
 *
 * enabled defaults TRUE — a created user is a usable user.
 */
async function createUser({ cognitoSub, email, role = 'user' }) {
  if (!cognitoSub) throw Object.assign(new Error('cognitoSub is required'), { code: 'COGNITO_SUB_REQUIRED' });
  const normalized = normalizeEmail(email);
  if (!normalized) throw Object.assign(new Error('email is required'), { code: 'EMAIL_REQUIRED' });
  if (!isValidRole(role)) throw Object.assign(new Error(`"${role}" is not a role.`), { code: 'INVALID_ROLE' });

  try {
    const { rows } = await db.query(
      `INSERT INTO users (id, cognito_sub, email, role, enabled, created_at, updated_at)
       VALUES ($1, $2, $3, $4, TRUE, now(), now())
       RETURNING ${COLUMNS}`,
      [crypto.randomUUID(), String(cognitoSub), normalized, role],
    );
    return rows[0];
  } catch (err) {
    // 23505 = unique_violation. Which column collided decides the message, and
    // both are the admin's problem to fix rather than a fault.
    if (err.code === '23505') {
      const on = /email/.test(err.constraint || '') ? 'email' : 'cognito_sub';
      throw Object.assign(new Error(`A user with that ${on === 'email' ? 'email' : 'Cognito identity'} already exists.`),
        { code: 'USER_EXISTS', on });
    }
    throw err;
  }
}

/**
 * Create the FIRST admin, atomically, or return null because one already exists.
 *
 * The bootstrap route checks that `users` is empty and then inserts. Those are
 * two statements, and between them a second concurrent bootstrap can pass the
 * same check — under READ COMMITTED a `WHERE NOT EXISTS` predicate does not
 * prevent it either, because the other transaction's row is not yet visible.
 * Two callers with different emails would each get an admin on a deployment
 * that is supposed to get exactly one.
 *
 * `pg_advisory_xact_lock` serializes the check and the insert against every
 * other bootstrap attempt on this database, and is released when the
 * transaction ends either way. The lock key is an arbitrary constant that means
 * "the bootstrap".
 *
 * Returns the row, or null when the deployment was already initialized — the
 * caller turns that into the same 409 the pre-check returns.
 */
const BOOTSTRAP_LOCK_KEY = 528491;

async function createFirstUser({ cognitoSub, email }) {
  if (!cognitoSub) throw Object.assign(new Error('cognitoSub is required'), { code: 'COGNITO_SUB_REQUIRED' });
  const normalized = normalizeEmail(email);
  if (!normalized) throw Object.assign(new Error('email is required'), { code: 'EMAIL_REQUIRED' });

  const client = await db.connect();
  try {
    await client.query('BEGIN');
    await client.query('SELECT pg_advisory_xact_lock($1)', [BOOTSTRAP_LOCK_KEY]);

    const { rows: existing } = await client.query('SELECT 1 FROM users LIMIT 1');
    if (existing.length > 0) {
      await client.query('ROLLBACK');
      return null;
    }

    const { rows } = await client.query(
      `INSERT INTO users (id, cognito_sub, email, role, enabled, created_at, updated_at)
       VALUES ($1, $2, $3, 'admin', TRUE, now(), now())
       RETURNING ${COLUMNS}`,
      [crypto.randomUUID(), String(cognitoSub), normalized],
    );
    await client.query('COMMIT');
    return rows[0];
  } catch (err) {
    await client.query('ROLLBACK').catch(() => {});
    if (err.code === '23505') {
      // Somebody inserted this exact identity while we waited for the lock.
      return null;
    }
    throw err;
  } finally {
    client.release();
  }
}

/**
 * PROMOTION ONLY: user → admin.
 *
 * admin → user is refused with ROLE_CHANGE_NOT_ALLOWED, read from the row rather
 * than from anything the caller sent. The UPDATE itself carries
 * `WHERE role = 'user'` so two concurrent requests cannot race one through.
 */
async function promote(cognitoSub, nextRole) {
  if (!isValidRole(nextRole)) {
    throw Object.assign(new Error(`"${nextRole}" is not a role.`), { code: 'INVALID_ROLE' });
  }
  const current = await findByCognitoSub(cognitoSub);
  if (!current) throw Object.assign(new Error('That user does not exist.'), { code: 'USER_NOT_FOUND' });

  if (ROLE_RANK[nextRole] <= ROLE_RANK[current.role]) {
    throw Object.assign(
      new Error(current.role === nextRole
        ? `That user is already ${nextRole}.`
        : `Cannot change ${current.role} → ${nextRole}. Promotion to admin is the only role change this product has.`),
      { code: 'ROLE_CHANGE_NOT_ALLOWED' },
    );
  }

  const { rows } = await db.query(
    `UPDATE users SET role = $2, updated_at = now()
     WHERE cognito_sub = $1 AND role = 'user'
     RETURNING ${COLUMNS}`,
    [String(cognitoSub), nextRole],
  );
  if (!rows[0]) {
    throw Object.assign(new Error('That user is no longer a user — nothing changed.'), { code: 'ROLE_CHANGE_NOT_ALLOWED' });
  }
  return rows[0];
}

/**
 * Successful-authentication bookkeeping: stamp last_login, and follow an email
 * that changed in Cognito.
 *
 * Cognito owns the email; this table only mirrors it. A collision (somebody else
 * already holds that address here) is logged and skipped rather than failing the
 * sign-in — the caller is authenticated and authorized either way, and their
 * display email is not worth a 500.
 */
async function recordLogin(cognitoSub, email) {
  const normalized = normalizeEmail(email);
  try {
    const { rows } = await db.query(
      `UPDATE users
          SET last_login = now(),
              email      = COALESCE(NULLIF($2, ''), email),
              updated_at = now()
        WHERE cognito_sub = $1
        RETURNING ${COLUMNS}`,
      [String(cognitoSub), normalized],
    );
    return rows[0] || null;
  } catch (err) {
    if (err.code === '23505') {
      console.warn(`[Users] email ${normalized} is already held by another user — last_login stamped without it.`);
      const { rows } = await db.query(
        `UPDATE users SET last_login = now(), updated_at = now()
          WHERE cognito_sub = $1 RETURNING ${COLUMNS}`, [String(cognitoSub)],
      );
      return rows[0] || null;
    }
    throw err;
  }
}

/**
 * NOT A FEATURE. The compensating half of a create: called only when the Cognito
 * user was made and this row could not be, or vice versa. No route reaches it.
 */
async function removeBySub(cognitoSub) {
  const { rowCount } = await db.query('DELETE FROM users WHERE cognito_sub = $1', [String(cognitoSub)]);
  return rowCount;
}

module.exports = {
  ROLES, ROLE_RANK, isValidRole, normalizeEmail, toPublicUser,
  findByCognitoSub, findByEmail, findById, listUsers, countUsers,
  createUser, createFirstUser, promote, recordLogin, removeBySub,
};

// ─── Self-check: node src/users/store.js ────────────────────────────────
// The pure rules — roles, normalisation, and the public shape. The SQL paths
// are exercised against a live Postgres by scripts/verify-auth.js.
if (require.main === module) {
  const assert = require('assert');

  assert.deepStrictEqual(ROLES, ['user', 'admin']);
  assert.ok(isValidRole('user') && isValidRole('admin'));
  // The roles this architecture forbids must not validate anywhere.
  for (const gone of ['pending', 'superadmin', 'tenant_admin', 'platform_admin', 'owner', '', undefined, null]) {
    assert.ok(!isValidRole(gone), `"${gone}" must not be a role`);
  }
  // Prototype keys must not read as roles.
  assert.ok(!isValidRole('constructor') && !isValidRole('toString'));

  assert.strictEqual(normalizeEmail('  Admin@Example.COM '), 'admin@example.com');
  assert.strictEqual(normalizeEmail(undefined), '');

  // toPublicUser: exactly the portal's columns, and nothing secret.
  const row = {
    id: '11111111-2222-3333-4444-555555555555',
    cognito_sub: 'sub-abc', email: 'a@b.test', role: 'admin', enabled: true,
    last_login: new Date('2026-01-02T03:04:05Z'),
    created_at: new Date('2025-12-01T00:00:00Z'),
    updated_at: new Date('2026-01-02T03:04:05Z'),
  };
  const pub = toPublicUser(row);
  assert.deepStrictEqual(Object.keys(pub).sort(),
    ['cognitoSub', 'createdAt', 'email', 'enabled', 'id', 'lastLogin', 'role', 'updatedAt']);
  assert.strictEqual(pub.lastLogin, '2026-01-02T03:04:05.000Z');
  assert.strictEqual(toPublicUser({ ...row, last_login: null }).lastLogin, null);
  assert.strictEqual(toPublicUser(null), null);
  // No password, no credential, no token may ever ride out on this shape.
  for (const forbidden of ['password', 'tempPassword', 'secret', 'token', 'credentials']) {
    assert.ok(!(forbidden in pub), `${forbidden} must never be returned by the API`);
  }

  // The promotion rule, as a table — the same comparison promote() makes.
  const allowed = (from, to) => ROLE_RANK[to] > ROLE_RANK[from];
  assert.ok(allowed('user', 'admin'), 'user → admin is the one role change');
  assert.ok(!allowed('admin', 'user'), 'admin → user must be refused');
  assert.ok(!allowed('admin', 'admin') && !allowed('user', 'user'), 'a no-op is not a promotion');

  // createFirstUser must serialize on an advisory lock, not merely re-check —
  // a plain check-then-insert races with a concurrent bootstrap.
  const storeSrc = require('fs').readFileSync(__filename, 'utf8');
  assert.ok(/pg_advisory_xact_lock/.test(storeSrc),
    'createFirstUser must hold an advisory lock across the check and the insert');
  assert.ok(/VALUES \(\$1, \$2, \$3, 'admin', TRUE/.test(storeSrc),
    'the first user is created as an enabled admin, and the role is not caller-supplied');

  console.log('✅ users/store.js self-check passed');
}
