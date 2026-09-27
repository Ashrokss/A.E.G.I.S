/**
 * Cognito account management inside THE ONE AEGIS user pool.
 *
 * Cognito's job here is authentication only: it holds the password, runs the
 * hosted UI, and issues the JWT. It does NOT hold the application role — that
 * lives in PostgreSQL (users/store.js). So this module creates and reads
 * accounts and NEVER touches a group:
 *
 *   NO AdminAddUserToGroup, NO AdminRemoveUserFromGroup,
 *   NO AdminListGroupsForUser, NO CreateGroup, NO CreateUserPool.
 *
 * There is exactly one pool, named by AWS_COGNITO_USER_POOL_ID. Nothing in this
 * product creates a pool at runtime; a fresh deployment gets one from
 * infra/aws/aws-setup.sh.
 *
 * IDENTITY: `sub` is the join key to PostgreSQL, and it is READ OFF THE
 * RESPONSE, never assumed. These pools use UsernameAttributes:['email'], so the
 * internal Username is an opaque uuid and the email is only an alias — a backend
 * that treats the email as the username breaks the moment a pool is configured
 * differently.
 *
 * CREDENTIALS: the default AWS chain — the ECS task role in AWS, whose policy
 * scopes cognito-idp:Admin* to this one pool (infra/terraform/modules/iam).
 */

const {
  CognitoIdentityProviderClient,
  AdminCreateUserCommand,
  AdminSetUserPasswordCommand,
  AdminGetUserCommand,
  AdminDeleteUserCommand,
  ListUsersCommand,
} = require('@aws-sdk/client-cognito-identity-provider');

/** The one pool. Read through a function so it cannot go stale against config. */
function userPoolId() {
  return (process.env.AWS_COGNITO_USER_POOL_ID || '').trim();
}

function region() {
  return (process.env.AWS_REGION || 'us-east-1').trim();
}

// Default credential chain: the ECS task role in AWS, your own profile locally.
// AEGIS holds no static AWS keys.
function buildClient() {
  return new CognitoIdentityProviderClient({ region: region() });
}

// One client for the process, reused by every call that is not handed its
// own. A client per call re-did the TCP + TLS handshake to cognito-idp every
// time -- about 3s per sign-in check from a machine far from the region, where
// a warm call costs one round trip. Safe to share: the credentials are static
// the SDK's default chain, which refreshes itself.
let sharedClient = null;
function defaultClient() {
  if (!sharedClient) sharedClient = buildClient();
  return sharedClient;
}

function requirePool() {
  const id = userPoolId();
  if (!id) {
    throw Object.assign(
      new Error('AWS_COGNITO_USER_POOL_ID is not set — this deployment has no Cognito pool to manage.'),
      { code: 'COGNITO_NOT_CONFIGURED' },
    );
  }
  return id;
}

/** The value of one Cognito attribute, from either response shape. */
function attr(user, name) {
  const list = user?.Attributes || user?.UserAttributes || [];
  return list.find((a) => a.Name === name)?.Value;
}

/**
 * `sub` off a Cognito user object — the identity PostgreSQL is keyed on.
 *
 * THE `sub` ATTRIBUTE ONLY, with no fallback to Username. Cognito always returns
 * it, and the two are only equal by coincidence: on a pool with
 * UsernameAttributes:['email'] the Username happens to be the sub uuid, but on a
 * username-based pool it is the chosen username. Falling back would store a
 * join key that is not a sub, and the user would authenticate fine and then
 * never be found in PostgreSQL. Absent ⇒ null, and the caller refuses.
 */
function subOf(user) {
  return attr(user, 'sub') || null;
}

/** A 20-character password that satisfies any default Cognito policy. */
function generateTempPassword() {
  const crypto = require('crypto');
  const upper = 'ABCDEFGHJKLMNPQRSTUVWXYZ';
  const lower = 'abcdefghijkmnopqrstuvwxyz';
  const digits = '23456789';
  const symbols = '!@#$%^&*-_=+';
  const all = upper + lower + digits + symbols;
  const pick = (set) => set[crypto.randomInt(set.length)];
  // One of each class first, so the policy is met regardless of the shuffle.
  const chars = [pick(upper), pick(lower), pick(digits), pick(symbols)];
  while (chars.length < 20) chars.push(pick(all));
  // Fisher-Yates with a CSPRNG — a sort(() => Math.random()-0.5) shuffle is
  // biased and would leave the four class characters near the front.
  for (let i = chars.length - 1; i > 0; i--) {
    const j = crypto.randomInt(i + 1);
    [chars[i], chars[j]] = [chars[j], chars[i]];
  }
  return chars.join('');
}

/**
 * Create a Cognito account and RETURN ITS SUB.
 *
 * No group is written. The role goes to PostgreSQL, by the caller, using the sub
 * this returns — and if that write fails the caller calls removeUser below.
 *
 * `password` supplied ⇒ permanent, the member signs in with exactly it.
 * Omitted ⇒ a one-time password is generated and returned, and Cognito makes
 * them choose their own at the hosted UI. Nothing is emailed; this product has
 * no mail path.
 */
async function createUser({ email, password } = {}, client = defaultClient()) {
  const UserPoolId = requirePool();
  const address = String(email || '').trim();
  if (!address) throw Object.assign(new Error('email is required'), { code: 'EMAIL_REQUIRED' });

  const chosen = typeof password === 'string' && password.length > 0 ? password : null;
  const temporaryPassword = chosen ? null : generateTempPassword();

  const created = await client.send(new AdminCreateUserCommand({
    UserPoolId,
    Username: address,
    MessageAction: 'SUPPRESS',
    ...(temporaryPassword ? { TemporaryPassword: temporaryPassword } : {}),
    UserAttributes: [
      { Name: 'email', Value: address },
      { Name: 'email_verified', Value: 'true' },
    ],
  }));

  const username = created?.User?.Username || address;
  const cognitoSub = subOf(created?.User);
  if (!cognitoSub) {
    // Without a sub there is nothing to key PostgreSQL on, so this account can
    // never be authorized. Undo it rather than leave an unusable half-user.
    await removeUser(username, client).catch((err) =>
      console.error(`[Cognito] MANUAL CLEANUP: created ${username} but could not read its sub or delete it:`, err.message));
    throw Object.assign(new Error('Cognito did not return a sub for the new account.'), { code: 'COGNITO_NO_SUB' });
  }

  if (chosen) {
    await client.send(new AdminSetUserPasswordCommand({
      UserPoolId, Username: username, Password: chosen, Permanent: true,
    }));
  }

  return { cognitoSub, username, email: address, ...(temporaryPassword ? { temporaryPassword } : {}) };
}

/** One account, by username OR email alias. null when absent. */
async function getUser(username, client = defaultClient()) {
  try {
    const resp = await client.send(new AdminGetUserCommand({
      UserPoolId: requirePool(), Username: String(username),
    }));
    return {
      username: resp.Username,
      cognitoSub: subOf(resp),
      email: attr(resp, 'email'),
      emailVerified: attr(resp, 'email_verified') === 'true',
      enabled: resp.Enabled,
    };
  } catch (err) {
    if (err?.name === 'UserNotFoundException') return null;
    throw err;
  }
}

/**
 * Look an account up by email — used by onboarding to reuse an admin who is
 * already in the pool instead of failing on UsernameExistsException.
 */
async function findByEmail(email, client = defaultClient()) {
  const address = String(email || '').trim();
  if (!address) return null;
  const resp = await client.send(new ListUsersCommand({
    UserPoolId: requirePool(),
    Filter: `email = "${address.replace(/"/g, '')}"`,
    Limit: 1,
  }));
  const user = (resp.Users || [])[0];
  if (!user) return null;
  return { username: user.Username, cognitoSub: subOf(user), email: attr(user, 'email'), enabled: user.Enabled };
}

/**
 * Does the pool contain ANY account at all?
 *
 * Used by the first-run bootstrap gate. `Limit: 1` because the question is
 * existence, not a count — this must not page through a large pool.
 *
 * Throws on failure rather than returning false: the caller treats an
 * unanswerable question as "assume it does", so a Cognito outage cannot open an
 * unauthenticated admin-creation path.
 */
async function hasAnyUser(client = defaultClient()) {
  const resp = await client.send(new ListUsersCommand({
    UserPoolId: requirePool(),
    Limit: 1,
  }));
  return (resp.Users || []).length > 0;
}

/**
 * NOT A FEATURE — the compensating half of a failed create, and nothing else.
 *
 * No route, no controller and no UI reaches this. The product has no
 * delete-user capability: an admin cannot remove a member from Cognito or from
 * PostgreSQL. This exists so that a create whose PostgreSQL half failed does not
 * leave an account behind that can authenticate but can never be authorized.
 */
async function removeUser(username, client = defaultClient()) {
  await client.send(new AdminDeleteUserCommand({
    UserPoolId: requirePool(), Username: String(username),
  }));
}

module.exports = {
  createUser, getUser, findByEmail, hasAnyUser, removeUser,
  buildClient, userPoolId, region, subOf, attr, generateTempPassword,
};

// ─── Self-check: node src/cognito/users.js ──────────────────────────────
// What matters here is which commands are sent and that the sub comes off the
// RESPONSE. Both are asserted against a stub client — no AWS call is made.
if (require.main === module) {
  const assert = require('assert');

  process.env.AWS_COGNITO_USER_POOL_ID = 'us-east-1_thepool';
  process.env.AWS_REGION = 'us-east-1';

  const stub = (overrides = {}) => {
    const sent = [];
    return {
      sent,
      names: () => sent.map((c) => c.constructor.name),
      async send(cmd) {
        sent.push(cmd);
        const kind = cmd.constructor.name;
        if (overrides[kind] instanceof Error) throw overrides[kind];
        if (kind === 'AdminCreateUserCommand') {
          return overrides.AdminCreateUserCommand || {
            User: {
              Username: '44f824b8-1111-2222-3333-444444444444',
              Attributes: [
                { Name: 'sub', Value: '44f824b8-1111-2222-3333-444444444444' },
                { Name: 'email', Value: cmd.input.Username },
              ],
            },
          };
        }
        if (kind === 'ListUsersCommand') return overrides.ListUsersCommand || { Users: [] };
        return {};
      },
    };
  };

  (async () => {
    // ── create captures the sub, sets no group ────────────────────────────
    let client = stub();
    const made = await createUser({ email: 'new@corp.test' }, client);
    assert.strictEqual(made.cognitoSub, '44f824b8-1111-2222-3333-444444444444');
    assert.strictEqual(made.email, 'new@corp.test');
    assert.ok(made.temporaryPassword, 'no chosen password ⇒ a one-time one comes back');
    assert.deepStrictEqual(client.names(), ['AdminCreateUserCommand'],
      'a create with no chosen password is ONE call — and never a group call');

    // ── THE ARCHITECTURE RULE: no group command may ever be sent ──────────
    const GROUP_COMMANDS = ['AdminAddUserToGroupCommand', 'AdminRemoveUserFromGroupCommand',
      'AdminListGroupsForUserCommand', 'CreateGroupCommand', 'ListGroupsCommand',
      'ListUsersInGroupCommand', 'CreateUserPoolCommand'];
    client = stub();
    await createUser({ email: 'a@corp.test', password: 'Chosen-Password-1!' }, client);
    assert.deepStrictEqual(client.names(), ['AdminCreateUserCommand', 'AdminSetUserPasswordCommand']);
    for (const banned of GROUP_COMMANDS) {
      assert.ok(!client.names().includes(banned),
        `${banned} must never be sent — Cognito groups do not authorize anything here`);
    }
    // A chosen password is set PERMANENT, so the member signs in with exactly it.
    assert.strictEqual(client.sent[1].input.Permanent, true);
    // And it must not come back in the result.
    const withChosen = await createUser({ email: 'a@corp.test', password: 'Chosen-Password-1!' }, stub());
    assert.strictEqual(withChosen.temporaryPassword, undefined,
      'a chosen password must never be echoed back');

    // ── the sub is read off the RESPONSE, not assumed to be the email ─────
    client = stub({
      AdminCreateUserCommand: {
        User: {
          Username: 'opaque-uuid-username',
          Attributes: [{ Name: 'sub', Value: 'the-real-sub' }, { Name: 'email', Value: 'x@corp.test' }],
        },
      },
    });
    const opaque = await createUser({ email: 'x@corp.test' }, client);
    assert.strictEqual(opaque.cognitoSub, 'the-real-sub', 'sub must come from the sub attribute');
    assert.strictEqual(opaque.username, 'opaque-uuid-username', 'username is not the email');
    assert.notStrictEqual(opaque.cognitoSub, opaque.email);

    // A response with no sub is unusable — it must roll its own account back
    // rather than fall back to Username, which on a username-based pool is NOT
    // the sub and would be stored as a join key that never matches at login.
    client = stub({ AdminCreateUserCommand: { User: { Username: 'chosen-username', Attributes: [] } } });
    await assert.rejects(() => createUser({ email: 'y@corp.test' }, client), /did not return a sub/);
    assert.ok(client.names().includes('AdminDeleteUserCommand'),
      'an account with no sub can never be authorized — it must be undone');
    assert.strictEqual(subOf({ Username: 'chosen-username', Attributes: [] }), null,
      'Username must NEVER stand in for sub');
    assert.strictEqual(subOf({ Username: 'u', Attributes: [{ Name: 'sub', Value: 's' }] }), 's');

    // ── getUser: absent is null, not a throw ──────────────────────────────
    const notFound = stub({ AdminGetUserCommand: Object.assign(new Error('nope'), { name: 'UserNotFoundException' }) });
    assert.strictEqual(await getUser('ghost', notFound), null);
    // Any other error propagates — an AccessDenied must not read as "no user".
    const denied = stub({ AdminGetUserCommand: Object.assign(new Error('no'), { name: 'AccessDeniedException' }) });
    await assert.rejects(() => getUser('someone', denied), /no/);

    // ── hasAnyUser: existence only, and it must not page ─────────────────
    client = stub({ ListUsersCommand: { Users: [] } });
    assert.strictEqual(await hasAnyUser(client), false, 'an empty pool has no users');
    assert.strictEqual(client.sent[0].input.Limit, 1, 'the existence check must ask for ONE user, not a page');
    assert.strictEqual(await hasAnyUser(stub({ ListUsersCommand: { Users: [{ Username: 'a' }] } })), true);
    // An unanswerable question must THROW, so the caller fails closed rather
    // than reading a Cognito outage as "the pool is empty".
    await assert.rejects(
      () => hasAnyUser(stub({ ListUsersCommand: Object.assign(new Error('no'), { name: 'AccessDeniedException' }) })),
      /no/, 'a failed existence check must propagate, never return false');

    // findByEmail returns null on an empty pool, and the sub when present.
    assert.strictEqual(await findByEmail('nobody@corp.test', stub()), null);
    const found = await findByEmail('a@corp.test', stub({
      ListUsersCommand: { Users: [{ Username: 'uuid-1', Attributes: [{ Name: 'sub', Value: 'sub-1' }, { Name: 'email', Value: 'a@corp.test' }], Enabled: true }] },
    }));
    assert.strictEqual(found.cognitoSub, 'sub-1');
    assert.strictEqual(await findByEmail('', stub()), null);

    // ── an unconfigured pool refuses rather than guessing ─────────────────
    const saved = process.env.AWS_COGNITO_USER_POOL_ID;
    delete process.env.AWS_COGNITO_USER_POOL_ID;
    await assert.rejects(() => createUser({ email: 'a@b.test' }, stub()), /AWS_COGNITO_USER_POOL_ID is not set/);
    process.env.AWS_COGNITO_USER_POOL_ID = saved;

    // ── generated passwords satisfy a default Cognito policy ──────────────
    for (let i = 0; i < 200; i++) {
      const p = generateTempPassword();
      assert.strictEqual(p.length, 20);
      assert.ok(/[A-Z]/.test(p) && /[a-z]/.test(p) && /[0-9]/.test(p) && /[^A-Za-z0-9]/.test(p),
        `generated password "${p}" must meet the default policy`);
    }
    // And they must differ — a constant would be a shared credential.
    assert.strictEqual(new Set(Array.from({ length: 50 }, generateTempPassword)).size, 50);

    console.log('✅ cognito/users.js self-check passed');
  })().catch((err) => { console.error('❌ cognito/users.js self-check FAILED:', err); process.exit(1); });
}
