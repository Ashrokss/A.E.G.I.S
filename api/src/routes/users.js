/**
 * The Admin Portal's API, and the one endpoint that tells a browser its role.
 *
 * FOUR ROUTES, AND THAT IS THE WHOLE SURFACE:
 *   GET   /api/auth/me   — who am I and what am I? (any signed-in user)
 *   GET   /api/users     — the roster, from PostgreSQL (admin)
 *   POST  /api/users     — create in Cognito AND PostgreSQL (admin)
 *   PATCH /api/users/:cognitoSub — promote user → admin (admin)
 *
 * THERE IS NO DELETE ROUTE, AND THERE IS NO DEMOTION. Both are absent by
 * design: no admin can remove a member or strip another admin's privileges, so
 * a mistaken or compromised admin session cannot take the deployment over. The
 * self-check at the bottom asserts the router exposes no DELETE verb at all, so
 * adding one later fails loudly.
 *
 * The roster is read from PostgreSQL, never assembled from Cognito groups — this
 * product has no authorization groups.
 */

const express = require('express');

const { requireAuthenticated, requireAdmin } = require('../middleware/auth');
const users = require('../users/store');
const cognito = require('../cognito/users');

const router = express.Router();

/** Map a store/Cognito error onto a status code. */
function sendUserError(res, err, fallbackMessage) {
  const byCode = {
    USER_EXISTS: 409,
    UsernameExistsException: 409,
    ROLE_CHANGE_NOT_ALLOWED: 403,
    USER_NOT_FOUND: 404,
    UserNotFoundException: 404,
    INVALID_ROLE: 400,
    EMAIL_REQUIRED: 400,
    InvalidPasswordException: 400,
    InvalidParameterException: 400,
    COGNITO_NOT_CONFIGURED: 503,
  };
  const status = byCode[err?.code] || byCode[err?.name];

  // Every mutating path here is a cognito-idp:Admin* call, none of which
  // SecurityAudit/ViewOnlyAccess grant. Say what is actually wrong — the fix is
  // an IAM policy, and nothing the operator does in the UI will change it.
  if (err?.name === 'AccessDeniedException') {
    console.error('[Users] Cognito access denied:', err.message);
    return res.status(503).json({
      error: 'This deployment is not authorized to manage accounts in Cognito. The task role is '
        + 'missing cognito-idp:Admin* on the user pool (infra/terraform/modules/iam).',
      code: 'COGNITO_ACCESS_DENIED',
    });
  }

  // A MAPPED condition was chosen and worded for whoever reads it. Anything
  // unmapped is a fault, and its message is not written for a user — it gets
  // logged and the caller gets this route's own wording.
  if (!status) {
    console.error('[Users]', err);
    return res.status(502).json({ error: fallbackMessage });
  }
  return res.status(status).json({
    error: err instanceof Error ? err.message : fallbackMessage,
    code: err?.code || err?.name || undefined,
  });
}

// ─── Who am I? ──────────────────────────────────────────────────────────
// The frontend calls this immediately after the OIDC callback and then relies
// on it for every role decision it renders. The role in the answer is
// PostgreSQL's, and the browser is told it rather than deriving it — there is
// no group claim for a client to read, and nothing it could send would change
// what the server allows anyway.
//
// This is also the "successful authentication" hook: last_login is stamped and
// the email is re-synced from Cognito, which owns it.
router.get('/api/auth/me', requireAuthenticated, async (req, res) => {
  const sub = req.user.sub;

  // Cognito owns the email; this table mirrors it. Read it live so an address
  // changed in the pool follows here on the next sign-in. A failure is not
  // fatal — the caller is already authenticated and authorized, and a stale
  // display email is not worth refusing a login over.
  let liveEmail = null;
  try {
    const account = await cognito.getUser(sub);
    liveEmail = account?.email || null;
  } catch (err) {
    console.warn('[Users] could not read the live Cognito email:', err.message);
  }

  let row = req.dbUser;
  try {
    row = (await users.recordLogin(sub, liveEmail || row.email)) || row;
  } catch (err) {
    console.error('[Users] could not record the login:', err.message);
  }

  res.json({ user: users.toPublicUser(row) });
});

// Per-request authorization for the agent service, which checks the caller on
// every chat request and every snapshot poll (600ms while a turn runs). It is
// /api/auth/me without the sign-in side effects: no Cognito round trip for the
// email, no last_login write. The role is still read from PostgreSQL on every
// call by requireAuthenticated, so a promotion or a disable is in force on the
// very next request, exactly as with /api/auth/me.
router.get('/api/auth/authorize', requireAuthenticated, (req, res) => {
  res.json({ user: users.toPublicUser(req.dbUser) });
});

// ─── The roster — admin only ────────────────────────────────────────────
// From PostgreSQL. Email, role, enabled, created, last login: exactly the
// columns the portal shows.
router.get('/api/users', requireAuthenticated, requireAdmin, async (_req, res) => {
  try {
    res.json({ users: (await users.listUsers()).map(users.toPublicUser) });
  } catch (err) {
    sendUserError(res, err, 'Could not list users.');
  }
});

// ─── Create a user — admin only ─────────────────────────────────────────
// BOTH SYSTEMS OR NEITHER. Cognito first (it is the one that can tell us the
// sub), then PostgreSQL. If the PostgreSQL write fails, the Cognito account is
// deleted again, because an account that can authenticate but can never be
// authorized is worse than no account: its owner reaches the hosted UI, signs
// in successfully, and is then refused with NOT_REGISTERED forever.
router.post('/api/users', requireAuthenticated, requireAdmin, async (req, res) => {
  const email = String(req.body?.email || '').trim();
  const role = String(req.body?.role || 'user').trim() || 'user';
  const password = typeof req.body?.password === 'string' ? req.body.password : '';

  if (!email) return res.status(400).json({ error: 'email is required', code: 'EMAIL_REQUIRED' });
  if (!users.isValidRole(role)) {
    return res.status(400).json({ error: `"${role}" is not a role.`, code: 'INVALID_ROLE' });
  }

  // Check PostgreSQL first. Creating the Cognito half and only then discovering
  // the address is taken here would mean a rollback for a case we can see
  // coming — and a rollback that itself fails leaves debris.
  try {
    if (await users.findByEmail(email)) {
      return res.status(409).json({ error: 'A user with that email already exists.', code: 'USER_EXISTS' });
    }
  } catch (err) {
    return sendUserError(res, err, 'Could not check for an existing user.');
  }

  let created;
  try {
    created = await cognito.createUser({ email, password });
  } catch (err) {
    return sendUserError(res, err, 'Could not create that account in Cognito.');
  }

  let row;
  try {
    row = await users.createUser({ cognitoSub: created.cognitoSub, email, role });
  } catch (err) {
    // Compensate: undo the Cognito half. If THAT fails there is nothing left to
    // try automatically, so say exactly what is orphaned and where.
    try {
      await cognito.removeUser(created.username);
      console.warn(`[Users] rolled back Cognito account ${created.username} after the database write failed.`);
    } catch (cleanupErr) {
      console.error(`[Users] MANUAL CLEANUP REQUIRED: Cognito account "${created.username}" `
        + `(sub ${created.cognitoSub}) exists with no database row:`, cleanupErr.message);
      return res.status(500).json({
        error: `The account was created in Cognito but could not be saved, and could not be removed again. `
          + `An operator must delete "${email}" from the user pool manually.`,
        code: 'PARTIAL_CREATE',
      });
    }
    return sendUserError(res, err, 'Could not save that user.');
  }

  console.log(`[Users] ${req.user.email} created ${email} as ${role}`);
  res.status(201).json({
    ok: true,
    user: users.toPublicUser(row),
    // Only present when no password was chosen — it is the one-time password
    // Cognito will make them replace. Returned once, never stored, never logged.
    ...(created.temporaryPassword ? { temporaryPassword: created.temporaryPassword } : {}),
  });
});

// ─── Promote a user → admin — admin only ────────────────────────────────
// The only role change this product has. PostgreSQL ONLY: nothing is written to
// Cognito, because Cognito holds no role. `admin → user` comes back as
// 403 ROLE_CHANGE_NOT_ALLOWED from the store, which reads the member's current
// role out of the table rather than trusting the request.
router.patch('/api/users/:cognitoSub', requireAuthenticated, requireAdmin, async (req, res) => {
  const role = String(req.body?.role || '').trim();
  if (!role) return res.status(400).json({ error: 'role is required', code: 'INVALID_ROLE' });

  try {
    const row = await users.promote(req.params.cognitoSub, role);
    console.log(`[Users] ${req.user.email} promoted ${row.email} to ${row.role}`);
    res.json({ ok: true, user: users.toPublicUser(row) });
  } catch (err) {
    sendUserError(res, err, 'Could not change that user\'s role.');
  }
});

module.exports = router;
module.exports.sendUserError = sendUserError;

// ─── Self-check: node src/routes/users.js ───────────────────────────────
// The WIRING is the security property here: which gate sits on which route, and
// the absence of a delete. Both are asserted off the router's own stack.
if (require.main === module) {
  const assert = require('assert');

  const layers = router.stack.filter((l) => l.route).map((l) => ({
    path: l.route.path,
    methods: Object.keys(l.route.methods).sort(),
    handlers: l.route.stack.map((h) => h.name),
  }));

  const at = (path, method) => layers.find((l) => l.path === path && l.methods.includes(method));

  // ── every route authenticates ─────────────────────────────────────────
  for (const l of layers) {
    assert.ok(l.handlers.includes('requireAuthenticated'),
      `${l.methods.join('/')} ${l.path} MUST require authentication`);
  }

  // ── the admin routes are admin-gated ──────────────────────────────────
  for (const [path, method] of [['/api/users', 'get'], ['/api/users', 'post'], ['/api/users/:cognitoSub', 'patch']]) {
    const l = at(path, method);
    assert.ok(l, `${method} ${path} must exist`);
    assert.ok(l.handlers.includes('requireAdmin'),
      `${method} ${path} MUST be admin-gated — without this any signed-in user manages the roster`);
  }

  // ── /api/auth/me is open to any signed-in user, and NOT admin-gated ───
  // Its whole job is to report a role, including 'user'. Gating it on admin
  // would 403 every plain user and leave the UI unable to render at all.
  const me = at('/api/auth/me', 'get');
  assert.ok(me, '/api/auth/me must exist');
  assert.ok(!me.handlers.includes('requireAdmin'), '/api/auth/me must not require admin');
  const authorize = at('/api/auth/authorize', 'get');
  assert.ok(authorize, '/api/auth/authorize must exist — the agent service authorizes every call through it');
  assert.ok(!authorize.handlers.includes('requireAdmin'), '/api/auth/authorize must not require admin');

  // ── NO DELETE, ANYWHERE ───────────────────────────────────────────────
  assert.deepStrictEqual(layers.filter((l) => l.methods.includes('delete')), [],
    'there is no delete-user capability — a DELETE route here would reintroduce it');
  // And no route may mention a member-removal path by any other verb.
  for (const l of layers) {
    assert.ok(!/remove|delete|demote|disable/i.test(l.path),
      `${l.path} looks like a removal route — this product has none`);
  }

  // ── exactly five routes: nothing has been added quietly ───────────────
  // (/api/auth/authorize is the fifth: the agent's per-request check.)
  assert.strictEqual(layers.length, 5, `expected 5 routes, found ${layers.length}: ${layers.map((l) => l.path).join(', ')}`);

  // ── sendUserError never leaks an unmapped error's message ─────────────
  const mkRes = () => ({
    code: null, body: null,
    status(c) { this.code = c; return this; },
    json(b) { this.body = b; return this; },
  });
  let res = mkRes();
  sendUserError(res, new Error('ECONNREFUSED 10.0.0.5:5432 password=hunter2'), 'Could not save that user.');
  assert.strictEqual(res.code, 502);
  assert.strictEqual(res.body.error, 'Could not save that user.',
    'an unmapped fault must not echo its own message — it could carry a connection string');
  assert.ok(!/hunter2/.test(JSON.stringify(res.body)), 'no secret may ride out on an error');

  // A mapped condition DOES get its own wording, because it was written for a reader.
  res = mkRes();
  sendUserError(res, Object.assign(new Error('A user with that email already exists.'), { code: 'USER_EXISTS' }), 'fallback');
  assert.strictEqual(res.code, 409);
  assert.strictEqual(res.body.code, 'USER_EXISTS');

  // Demotion maps to 403, not 400 — it is a refusal, not a malformed request.
  res = mkRes();
  sendUserError(res, Object.assign(new Error('nope'), { code: 'ROLE_CHANGE_NOT_ALLOWED' }), 'fallback');
  assert.strictEqual(res.code, 403);

  // A missing IAM grant is reported as such, on either error shape.
  res = mkRes();
  sendUserError(res, Object.assign(new Error('no'), { name: 'AccessDeniedException' }), 'fallback');
  assert.strictEqual(res.code, 503);
  assert.strictEqual(res.body.code, 'COGNITO_ACCESS_DENIED');

  console.log('✅ routes/users.js self-check passed');
}
