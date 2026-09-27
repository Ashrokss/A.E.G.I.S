/**
 * The one PostgreSQL connection pool.
 *
 * PostgreSQL is this product's authorization store: a row in `users` is what
 * makes a Cognito identity a AEGIS user, and its `role` column is the
 * ONLY source of an application role. Nothing here is optional — a backend that
 * cannot reach Postgres cannot authorize anybody, and says so rather than
 * falling back to a JWT claim.
 *
 * Configuration comes from the environment (the root .env locally, the ECS
 * task definition in AWS). The password is never logged.
 *
 * ROTATION: in AWS the password is RDS-managed and rotated on a schedule, but
 * ECS injects POSTGRES_PASSWORD only once, at task start. With
 * POSTGRES_PASSWORD_SECRET_ARN set, the password is instead read from that
 * secret for each NEW connection (cached briefly), so a rotation never
 * strands a running task. Established connections are unaffected by rotation.
 */

const fs = require('fs');

const { Pool } = require('pg');

let pool = null;

const PASSWORD_CACHE_MS = 60_000;

/** A pg `password` function reading the RDS-managed secret, or the static value. */
function passwordSource(env) {
  const secretArn = (env.POSTGRES_PASSWORD_SECRET_ARN || '').trim();
  if (!secretArn) return env.POSTGRES_PASSWORD || '';

  let cached = null;
  let fetchedAt = 0;
  return async () => {
    if (cached && Date.now() - fetchedAt < PASSWORD_CACHE_MS) return cached;
    // Lazy: local development never loads the SDK.
    const { SecretsManagerClient, GetSecretValueCommand } = require('@aws-sdk/client-secrets-manager');
    const client = new SecretsManagerClient({ region: env.AWS_REGION || 'us-east-1' });
    const out = await client.send(new GetSecretValueCommand({ SecretId: secretArn }));
    cached = JSON.parse(out.SecretString).password;
    fetchedAt = Date.now();
    return cached;
  };
}

/**
 * TLS settings for the authorization store.
 *
 * @returns undefined (no TLS), or a node-postgres ssl object.
 */
function sslConfig(env) {
  const mode = (env.POSTGRES_SSL || '').trim().toLowerCase();
  if (!mode || mode === 'false') return undefined;

  if (mode === 'insecure') {
    console.warn('[Db] POSTGRES_SSL=insecure — the database certificate is NOT verified. '
      + 'Anyone able to intercept this connection can decide who is an admin.');
    return { rejectUnauthorized: false };
  }

  // Anything else ('true', '1', 'verify', a typo) verifies. Failing closed on a
  // misspelling is the right direction for this particular setting.
  const caPath = (env.POSTGRES_SSL_CA || '').trim();
  if (!caPath) return { rejectUnauthorized: true };

  // Accept either a path or an inline PEM, so a container can pass the bundle
  // through the environment without a mounted file.
  const ca = caPath.includes('-----BEGIN CERTIFICATE-----')
    ? caPath
    : fs.readFileSync(caPath, 'utf8');
  return { rejectUnauthorized: true, ca };
}

function config(env = process.env) {
  return {
    host: (env.POSTGRES_HOST || 'localhost').trim(),
    port: Number((env.POSTGRES_PORT || '5432').trim()),
    database: (env.POSTGRES_DB || 'aegis').trim(),
    user: (env.POSTGRES_USER || 'aegis').trim(),
    password: passwordSource(env),
    // A hosted RDS instance terminates TLS; a local docker Postgres does not.
    // Opt in explicitly rather than guessing from the hostname.
    //
    // THE CERTIFICATE IS VERIFIED. This connection is the sole source of every
    // authorization decision the product makes, so an unauthenticated TLS
    // session means whoever can MITM it decides who is an admin — encryption
    // without authentication buys nothing against that. `POSTGRES_SSL=true`
    // therefore verifies, using POSTGRES_SSL_CA when supplied (RDS publishes a
    // bundle) and the system trust store otherwise.
    //
    // `POSTGRES_SSL=insecure` restores the unverified behaviour, and is named
    // for what it is so nobody reaches for it by accident.
    ssl: sslConfig(env),
    max: Number((env.POSTGRES_POOL_MAX || '10').trim()),
    // Every request authorizes here, so a cold connection is paid by a
    // person: TCP + TLS + login is several round trips, 3-6s from a laptop
    // far from the region. At 30s idle, any pause that long made the next
    // click slow. Five minutes keeps it warm through normal use; TCP
    // keepalive stops a router or NAT from silently dropping the idle socket,
    // which would otherwise surface as a failed authorization.
    idleTimeoutMillis: 300_000,
    keepAlive: true,
    keepAliveInitialDelayMillis: 30_000,
    connectionTimeoutMillis: 10_000,
  };
}

/** Lazily created, so requiring this module never opens a socket. */
function getPool() {
  if (!pool) {
    const cfg = config();
    pool = new Pool(cfg);
    // An idle-client error is emitted on the pool, and an unhandled 'error'
    // event on an EventEmitter takes the process down — which would turn a
    // routine Postgres restart into a backend crash.
    pool.on('error', (err) => console.error('[Db] idle client error:', err.message));
    // Host and database only. Never the password, and never the whole config
    // object — util.inspect would print the password with it.
    console.log(`[Db] PostgreSQL pool → ${cfg.user}@${cfg.host}:${cfg.port}/${cfg.database}`);
  }
  return pool;
}

function query(text, params) {
  return getPool().query(text, params);
}

/** One connection held for the caller — used by the create/rollback transaction. */
function connect() {
  return getPool().connect();
}

/** True when the database answers. Used by /health and by startup. */
async function ping() {
  await query('SELECT 1');
  return true;
}

async function close() {
  if (pool) {
    await pool.end();
    pool = null;
  }
}

module.exports = { getPool, query, connect, ping, close, config };

// ─── Self-check: node src/db/pool.js ────────────────────────────────────
// Config assembly only — it decides which database the product authorizes
// against, and it must never leak the password into a log line.
if (require.main === module) {
  const assert = require('assert');

  const cfg = config({
    POSTGRES_HOST: 'db.internal', POSTGRES_PORT: '6543', POSTGRES_DB: 'ops',
    POSTGRES_USER: 'svc', POSTGRES_PASSWORD: 'sekret', POSTGRES_SSL: 'true',
  });
  assert.strictEqual(cfg.host, 'db.internal');
  assert.strictEqual(cfg.port, 6543, 'port must be a number, not a string');
  assert.strictEqual(cfg.database, 'ops');
  assert.strictEqual(cfg.user, 'svc');
  // ── TLS VERIFIES BY DEFAULT ──────────────────────────────────────────
  // This connection decides who is an admin. An encrypted-but-unauthenticated
  // session hands that decision to anyone who can intercept it, so `true`
  // must mean verified.
  assert.deepStrictEqual(cfg.ssl, { rejectUnauthorized: true },
    'POSTGRES_SSL=true must VERIFY the server certificate');

  // A supplied CA is used — as a path or as inline PEM.
  const inlinePem = '-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----';
  assert.deepStrictEqual(config({ POSTGRES_SSL: 'true', POSTGRES_SSL_CA: inlinePem }).ssl,
    { rejectUnauthorized: true, ca: inlinePem }, 'an inline PEM bundle must be passed through');

  // Only the explicitly-named escape hatch disables verification, and a typo
  // must fail CLOSED (verifying) rather than open.
  assert.deepStrictEqual(config({ POSTGRES_SSL: 'insecure' }).ssl, { rejectUnauthorized: false });
  for (const typo of ['ture', 'yes', 'verify', '1', 'TRUE']) {
    assert.strictEqual(config({ POSTGRES_SSL: typo }).ssl.rejectUnauthorized, true,
      `POSTGRES_SSL=${typo} must verify — an unrecognised value must not disable TLS checking`);
  }

  // Defaults, so a bare .env still starts against the compose Postgres.
  const bare = config({});
  assert.strictEqual(bare.host, 'localhost');
  assert.strictEqual(bare.port, 5432);
  assert.strictEqual(bare.database, 'aegis');
  assert.strictEqual(bare.ssl, undefined, 'TLS must be opt-in, not guessed');
  assert.strictEqual(config({ POSTGRES_SSL: 'false' }).ssl, undefined);

  // A static password without a secret ARN; a function with one (rotation-safe).
  assert.strictEqual(config({ POSTGRES_PASSWORD: 'pw' }).password, 'pw');
  assert.strictEqual(typeof config({ POSTGRES_PASSWORD_SECRET_ARN: 'arn:aws:secretsmanager:x' }).password, 'function');

  // Whitespace in a .env value must not become part of a hostname.
  assert.strictEqual(config({ POSTGRES_HOST: '  db  ' }).host, 'db');

  console.log('✅ db/pool.js self-check passed');
}
