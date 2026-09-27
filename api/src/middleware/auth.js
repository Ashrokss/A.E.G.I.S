/**
 * ONE lane reaches every protected route: a Cognito access token from THE
 * AEGIS user pool, verified against that pool's JWKS.
 *
 * The two questions are answered by two different systems, and neither is ever
 * asked of the other:
 *
 *   WHO ARE YOU?      Cognito. The JWT's signature, issuer, token_use and
 *                     client_id are checked, and its `sub` is the identity.
 *   WHAT MAY YOU DO?  PostgreSQL. `users.role` for that sub, re-read on EVERY
 *                     request.
 *
 * NOTHING HERE TRUSTS A CLAIM ABOUT AUTHORIZATION. Not `cognito:groups`, not a
 * `role` claim, not a session, not a header, not anything the client sent. A
 * forged token fails signature verification; a genuine token that claims
 * `role: admin` is authorized purely on what Postgres says. And because the row
 * is read per request rather than cached, a promotion or a disable takes effect
 * on the caller's very next call instead of when their token expires.
 *
 * A AEGIS USER EXISTS IN BOTH SYSTEMS. A valid Cognito identity with no
 * Postgres row is NOT a user: it gets 403 NOT_REGISTERED and no access. This
 * middleware never creates a row — accounts are made in the Admin Portal.
 *
 * THE TWO GATES:
 *   requireAuthenticated — signed in, registered and enabled. The floor for
 *                          every protected route.
 *   requireAdmin         — role === 'admin'. User management and knowledge-base
 *                          approvals.
 */

const jwt = require('jsonwebtoken');
const jwksClient = require('jwks-rsa');

const users = require('../users/store');

function issuerUrl() {
  return (process.env.AWS_COGNITO_ISSUER_URL || '').trim().replace(/\/+$/, '');
}

function clientId() {
  return (process.env.AWS_COGNITO_CLIENT_ID || '').trim();
}

/**
 * The pool's JWKS. AWS_COGNITO_JWKS_URL when configured, else derived from the
 * issuer — the two are the same document, and .env already carries both.
 */
function jwksUri() {
  const explicit = (process.env.AWS_COGNITO_JWKS_URL || '').trim();
  return explicit || `${issuerUrl()}/.well-known/jwks.json`;
}

let keyClient = null;
function signingKeys() {
  if (!keyClient) {
    keyClient = jwksClient({ jwksUri: jwksUri(), cache: true, cacheMaxAge: 600_000, rateLimit: true });
  }
  return keyClient;
}

function getKey(header, callback) {
  signingKeys().getSigningKey(header.kid, (err, key) => {
    if (err) return callback(err, null);
    callback(null, key.publicKey || key.rsaPublicKey);
  });
}

/** The bearer token on this request, or ''. */
function bearerToken(req) {
  const header = req.headers?.authorization;
  if (typeof header !== 'string' || !header.startsWith('Bearer ')) return '';
  return header.slice(7).trim();
}

/**
 * Verify a Cognito ACCESS token.
 *
 * EVERY PARAMETER OF THIS DECISION COMES FROM THE ENVIRONMENT. The issuer, the
 * app client id and the JWKS endpoint are read from `process.env` through the
 * functions above; nothing on the request can influence which pool a token is
 * checked against, which keys are trusted, or what counts as valid. The only
 * thing taken from the request is the token itself.
 *
 * Checked here: RS256 signature against the pool's published JWKS, `iss`,
 * `exp`/`nbf` (jsonwebtoken enforces both by default), `token_use === 'access'`
 * and `client_id`.
 *
 * No `audience` option: Cognito access tokens carry no `aud` claim at all (only
 * ID tokens do), so passing one would reject every valid token. `client_id` and
 * `token_use` are checked explicitly instead, which is the equivalent.
 */
function verifyCognitoToken(token) {
  return new Promise((resolve, reject) => {
    // Refuse rather than verify against a half-configured pool.
    //
    // This is not theoretical: jsonwebtoken skips the issuer check when the
    // `issuer` option is FALSY, so an empty AWS_COGNITO_ISSUER_URL would verify
    // signature alone and accept a token from any issuer whose kid happened to
    // resolve. validateEnv makes both variables fatal at start-up, so a running
    // backend cannot reach this — it is here so that a future caller who
    // bypasses that check still fails closed instead of silently fails open.
    const iss = issuerUrl();
    const aud = clientId();
    if (!iss || !aud) {
      return reject(new Error('Cognito is not configured: AWS_COGNITO_ISSUER_URL and '
        + 'AWS_COGNITO_CLIENT_ID are both required to verify a token.'));
    }

    jwt.verify(
      token,
      getKey,
      { issuer: iss, algorithms: ['RS256'] },
      (err, decoded) => {
        if (err) return reject(err);
        if (decoded.token_use !== 'access') {
          return reject(new Error(`Expected an access token, got token_use=${decoded.token_use}`));
        }
        if (decoded.client_id !== aud) {
          return reject(new Error('Token was not issued for this app client'));
        }
        resolve(decoded);
      },
    );
  });
}

/**
 * The request's authenticated + authorized identity.
 *
 * Sets, on success:
 *   req.user     — { sub, username, email, role }. `sub` and `username` are
 *                  what the service routes already use for job attribution, and
 *                  `email` comes from PostgreSQL (an access token has no email
 *                  claim). `role` is on here for logging only; every gate reads
 *                  req.appRole.
 *   req.appRole  — 'admin' | 'user', from PostgreSQL. THE authorization answer.
 *   req.dbUser   — the full row, for routes that need created_at/last_login.
 *
 * Fails closed. An unreachable database is 503, never a pass.
 */
/**
 * The AUTHORIZATION half, given already-verified Cognito claims.
 *
 * Split out from the middleware so it can be driven directly by the self-check
 * and reused by the /api/auth/me route: this is the function that turns a
 * Cognito identity into a AEGIS role, and it is the only one that does.
 *
 * @returns {Promise<{ok: true, row, user} | {ok: false, status, body}>}
 */
async function authorizeClaims(claims) {
  const cognitoSub = claims?.sub;
  if (!cognitoSub) {
    return { ok: false, status: 401, body: { error: 'Token carries no subject', code: 'INVALID_TOKEN' } };
  }

  let row;
  try {
    row = await users.findByCognitoSub(cognitoSub);
  } catch (err) {
    console.error('[Auth] authorization lookup failed:', err.message);
    return {
      ok: false,
      status: 503,
      body: {
        error: 'Authorization is unavailable — the user database could not be reached.',
        code: 'AUTHZ_UNAVAILABLE',
      },
    };
  }

  // Authenticated by Cognito, but not a AEGIS user. This is the rule that
  // makes the Admin Portal the only way in: no row, no access, and no row is
  // ever created here.
  if (!row) {
    console.warn(`[Auth] ${cognitoSub} authenticated but is not registered — refusing.`);
    return {
      ok: false,
      status: 403,
      body: {
        error: 'Your account is not registered with AEGIS. Ask an administrator to create it.',
        code: 'NOT_REGISTERED',
      },
    };
  }
  if (!row.enabled) {
    return {
      ok: false,
      status: 403,
      body: { error: 'This account has been disabled.', code: 'ACCOUNT_DISABLED' },
    };
  }

  return {
    ok: true,
    row,
    user: {
      sub: row.cognito_sub,
      // Cognito's own username, for the rare caller that needs it. NOT assumed
      // to be the email — on a pool with UsernameAttributes:['email'] it is a
      // uuid.
      username: claims.username || claims['cognito:username'] || row.cognito_sub,
      email: row.email,
      role: row.role,
    },
  };
}

async function requireAuthenticated(req, res, next) {
  const token = bearerToken(req);
  if (!token) {
    return res.status(401).json({ error: 'Missing or invalid Authorization header', code: 'NO_TOKEN' });
  }

  let claims;
  try {
    claims = await verifyCognitoToken(token);
  } catch (err) {
    // The message, not the token — a token must never reach a log.
    console.warn('[Auth] token rejected:', err.message);
    return res.status(401).json({ error: 'Invalid or expired token', code: 'INVALID_TOKEN' });
  }

  const decision = await authorizeClaims(claims);
  if (!decision.ok) return res.status(decision.status).json(decision.body);

  req.user = decision.user;
  req.appRole = decision.row.role;
  req.dbUser = decision.row;
  return next();
}

/** role === 'admin', from PostgreSQL. MUST run after requireAuthenticated. */
function requireAdmin(req, res, next) {
  if (!req.user || !req.appRole) {
    return res.status(401).json({ error: 'Not authenticated', code: 'NOT_AUTHENTICATED' });
  }
  if (req.appRole !== 'admin') {
    return res.status(403).json({
      error: 'This operation requires an administrator.',
      code: 'INSUFFICIENT_ROLE',
    });
  }
  return next();
}

module.exports = {
  requireAuthenticated,
  requireAdmin,
  authorizeClaims,
  verifyCognitoToken,
  bearerToken,
  jwksUri,
};

// ─── Self-check: node src/middleware/auth.js ────────────────────────────
// The authorization decisions, with the PostgreSQL lookup stubbed. Token
// verification needs a live JWKS, so it is only checked for failing closed —
// which is the property that matters.
if (require.main === module) {
  const assert = require('assert');

  const mkRes = () => ({
    code: null, body: null,
    status(c) { this.code = c; return this; },
    json(b) { this.body = b; return this; },
  });

  const run = async (mw, req) => {
    const res = mkRes();
    let nexted = false;
    await mw(req, res, () => { nexted = true; });
    return { nexted, code: res.code, body: res.body, req };
  };

  // Swap the store's lookup for a stub. The middleware must consult it on EVERY
  // request — the counter proves there is no cache.
  const realFind = users.findByCognitoSub;
  let lookups = 0;
  const stubLookup = (result) => {
    users.findByCognitoSub = async () => {
      lookups += 1;
      if (result instanceof Error) throw result;
      return result;
    };
  };

  (async () => {
    // ── no token, or a token that is not a Bearer ────────────────────────
    for (const headers of [{}, { authorization: 'Basic abc' }, { authorization: 'Bearer' }, { authorization: 'Bearer    ' }]) {
      const out = await run(requireAuthenticated, { headers });
      assert.ok(!out.nexted, `${JSON.stringify(headers)} must not authenticate`);
      assert.strictEqual(out.code, 401);
      assert.strictEqual(out.body.code, 'NO_TOKEN');
    }

    // ── a forged / unverifiable token never reaches the database ─────────
    // This is the answer to "forged JWT claiming role=admin": verification
    // fails first, so the claim is never even looked at.
    lookups = 0;
    stubLookup({ cognito_sub: 's', email: 'a@b.test', role: 'admin', enabled: true });
    process.env.AWS_COGNITO_ISSUER_URL = 'https://cognito-idp.us-east-1.amazonaws.com/us-east-1_nope';
    process.env.AWS_COGNITO_CLIENT_ID = 'the-client';
    const forged = jwt.sign(
      { sub: 'attacker', token_use: 'access', client_id: 'the-client', 'cognito:groups': ['admin'], role: 'admin' },
      'not-aws-private-key', { algorithm: 'HS256' },
    );
    const forgedOut = await run(requireAuthenticated, { headers: { authorization: `Bearer ${forged}` } });
    assert.ok(!forgedOut.nexted, 'an HS256-forged token must never authenticate');
    assert.strictEqual(forgedOut.code, 401);
    assert.strictEqual(forgedOut.body.code, 'INVALID_TOKEN');
    assert.strictEqual(lookups, 0, 'an unverified token must not even reach the user store');
    // And no detail from the verifier leaks to the caller.
    assert.ok(!('details' in forgedOut.body), 'a 401 must not echo verifier internals');

    // ── the PostgreSQL outcomes, through the REAL authorizeClaims ────────
    const CLAIMS = { sub: 'sub-123', token_use: 'access', client_id: 'the-client', username: 'uuid-name' };
    const authorize = async (row) => { stubLookup(row); return authorizeClaims(CLAIMS); };

    // No row ⇒ NOT_REGISTERED. A valid Cognito login is not a AEGIS login.
    const unregistered = await authorize(null);
    assert.strictEqual(unregistered.ok, false);
    assert.strictEqual(unregistered.status, 403);
    assert.strictEqual(unregistered.body.code, 'NOT_REGISTERED');

    // enabled=false ⇒ ACCOUNT_DISABLED, and a disabled ADMIN is refused too:
    // enabled is checked before the role is ever considered.
    for (const role of ['user', 'admin']) {
      const disabled = await authorize({ cognito_sub: 'sub-123', email: 'a@b.test', role, enabled: false });
      assert.strictEqual(disabled.ok, false);
      assert.strictEqual(disabled.status, 403);
      assert.strictEqual(disabled.body.code, 'ACCOUNT_DISABLED');
    }

    // A database that cannot be reached is 503, never a pass.
    const down = await authorize(new Error('ECONNREFUSED'));
    assert.strictEqual(down.ok, false);
    assert.strictEqual(down.status, 503);
    assert.strictEqual(down.body.code, 'AUTHZ_UNAVAILABLE');

    // A token with no subject cannot be authorized at all.
    stubLookup({ cognito_sub: 'sub-123', email: 'a@b.test', role: 'admin', enabled: true });
    assert.strictEqual((await authorizeClaims({})).body.code, 'INVALID_TOKEN');
    assert.strictEqual((await authorizeClaims(null)).body.code, 'INVALID_TOKEN');

    // A registered, enabled user is authorized with the role POSTGRES holds —
    // and the email comes from the row, not from the token.
    const asUser = await authorize({ cognito_sub: 'sub-123', email: 'u@b.test', role: 'user', enabled: true });
    assert.strictEqual(asUser.ok, true);
    assert.strictEqual(asUser.row.role, 'user');
    assert.strictEqual(asUser.user.email, 'u@b.test');
    assert.strictEqual(asUser.user.sub, 'sub-123');
    assert.strictEqual(asUser.user.username, 'uuid-name', 'username is the token claim, not the email');
    const asAdmin = await authorize({ cognito_sub: 'sub-123', email: 'a@b.test', role: 'admin', enabled: true });
    assert.strictEqual(asAdmin.row.role, 'admin');

    // A group or role claim ON THE TOKEN changes nothing: the same row decides.
    const loud = { ...CLAIMS, 'cognito:groups': ['admin'], role: 'admin', scope: 'admin' };
    stubLookup({ cognito_sub: 'sub-123', email: 'u@b.test', role: 'user', enabled: true });
    assert.strictEqual((await authorizeClaims(loud)).row.role, 'user',
      'a token claiming admin must still resolve to the PostgreSQL role');

    // EVERY authorization asks the database — there is no cache, so a role
    // changed in Postgres is in force on the very next request.
    const before = lookups;
    stubLookup({ cognito_sub: 'sub-123', email: 'u@b.test', role: 'user', enabled: true });
    await authorizeClaims(CLAIMS);
    stubLookup({ cognito_sub: 'sub-123', email: 'u@b.test', role: 'admin', enabled: true });
    const promoted = await authorizeClaims(CLAIMS);
    assert.strictEqual(promoted.row.role, 'admin', 'the SAME token must pick up a new role immediately');
    assert.strictEqual(lookups - before, 2, 'one database read per authorization, never a cached answer');

    const authModule = require('./auth');

    // ── requireAdmin reads ONLY req.appRole ───────────
    for (const gate of [requireAdmin]) {
      const name = gate.name;
      // Unauthenticated.
      assert.strictEqual((await run(gate, {})).code, 401, `${name}: no user ⇒ 401`);
      assert.strictEqual((await run(gate, { user: { sub: 'x' } })).code, 401,
        `${name}: a user with no resolved role ⇒ 401, never a pass`);
      // A plain user is refused.
      const denied = await run(gate, { user: { sub: 'x' }, appRole: 'user' });
      assert.ok(!denied.nexted, `${name} must refuse a user`);
      assert.strictEqual(denied.code, 403);
      assert.strictEqual(denied.body.code, 'INSUFFICIENT_ROLE');
      // An admin passes.
      assert.ok((await run(gate, { user: { sub: 'x' }, appRole: 'admin' })).nexted, `${name} must pass an admin`);

      // THE CENTRAL PROPERTY: nothing the client can send substitutes for the
      // PostgreSQL role. Every one of these carries an admin claim somewhere
      // and must still be refused.
      const impostors = [
        { user: { sub: 'x', role: 'admin' }, appRole: 'user' },
        { user: { sub: 'x', groups: ['admin'] }, appRole: 'user' },
        { user: { sub: 'x', 'cognito:groups': ['admin'] }, appRole: 'user' },
        { user: { sub: 'x' }, appRole: 'user', tenantRole: 'admin' },
        { user: { sub: 'x' }, appRole: 'user', headers: { 'x-role': 'admin' } },
        { user: { sub: 'x' }, appRole: 'user', session: { role: 'admin' } },
        { user: { sub: 'x' }, appRole: 'user', query: { role: 'admin' } },
        { user: { sub: 'x' }, appRole: 'user', body: { role: 'admin' } },
      ];
      for (const req of impostors) {
        const out = await run(gate, req);
        assert.ok(!out.nexted, `${name}: ${JSON.stringify(req)} must NOT be admin`);
        assert.strictEqual(out.code, 403);
      }
      // And a bogus appRole is not admin either.
      for (const role of ['administrator', 'Admin', 'superadmin', 'platform_admin', 'tenant_admin', 'pending', '', null]) {
        assert.strictEqual((await run(gate, { user: { sub: 'x' }, appRole: role })).code,
          role ? 403 : 401, `appRole=${JSON.stringify(role)} must not authorize`);
      }
    }

    // ── configuration provenance: the request cannot influence any of it ──
    // The only thing this module may take from a request is the token.
    const authSrc = require('fs').readFileSync(__filename, 'utf8')
      .split('if (require.main === module)')[0];
    const reqReads = authSrc.match(/req\.(body|query|params|headers)[^\s;),]*/g) || [];
    assert.deepStrictEqual([...new Set(reqReads)], ['req.headers?.authorization'],
      `this module must read ONLY the Authorization header from a request, found: ${reqReads}`);
    for (const src of ['issuerUrl', 'clientId', 'jwksUri']) {
      assert.ok(new RegExp(`function ${src}\\(\\)[^}]*process\\.env`).test(authSrc),
        `${src}() must read process.env and take no argument`);
    }

    // A half-configured pool refuses instead of verifying signature alone —
    // jsonwebtoken skips a falsy `issuer`, which would otherwise fail OPEN.
    const savedIss = process.env.AWS_COGNITO_ISSUER_URL;
    const savedAud = process.env.AWS_COGNITO_CLIENT_ID;
    for (const [i, a] of [['', 'c'], ['https://iss', ''], ['', '']]) {
      process.env.AWS_COGNITO_ISSUER_URL = i;
      process.env.AWS_COGNITO_CLIENT_ID = a;
      await assert.rejects(() => verifyCognitoToken('any.token.here'),
        /Cognito is not configured/,
        `issuer=${JSON.stringify(i)} client=${JSON.stringify(a)} must refuse, never verify`);
    }
    process.env.AWS_COGNITO_ISSUER_URL = savedIss;
    process.env.AWS_COGNITO_CLIENT_ID = savedAud;

    // No cognito-group helper may survive on this module — its presence would
    // mean group-based authorization had crept back in.
    for (const gone of ['privilegedGroups', 'roleGroups', 'requireTenantRole', 'requirePlatformLane']) {
      assert.strictEqual(authModule[gone], undefined, `${gone} must not exist any more`);
    }

    users.findByCognitoSub = realFind;
    console.log('✅ middleware/auth.js self-check passed');
  })().catch((err) => { console.error('❌ middleware/auth.js self-check FAILED:', err); process.exit(1); });
}
