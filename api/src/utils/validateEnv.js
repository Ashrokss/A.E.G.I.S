/**
 * Start-up configuration check.
 *
 * Configuration comes from the root .env and nothing else — no secrets service,
 * no second loader. server.js calls dotenv.config() on ../../.env before this
 * runs, and this is the only place that decides whether what arrived is enough.
 *
 * NOTHING HERE PRINTS A SECRET. The report names variables and says whether they
 * are set; POSTGRES_PASSWORD, the AWS secret keys and every token are only ever
 * reported as present or missing.
 */

const REQUIRED = [
  // Cognito — authentication.
  'AWS_COGNITO_CLIENT_ID',
  'AWS_COGNITO_DOMAIN',
  'AWS_COGNITO_ISSUER_URL',
  'AWS_COGNITO_USER_POOL_ID',
  // PostgreSQL — authorization. Without these nothing can be authorized, so a
  // missing one is as fatal as a missing pool id.
  'POSTGRES_HOST',
  'POSTGRES_DB',
  'POSTGRES_USER',
  'POSTGRES_PASSWORD',
  // Sealing keys for stored ExternalIds and integration tokens. Both stores
  // refuse to write plaintext, so a missing key is a broken deployment.
  'AWS_ONBOARDING_ENC_KEY',
  'INTEGRATIONS_ENC_KEY',
];

/** Set but never shown — reported as a presence, not a value. */
const SECRETS = new Set([
  'POSTGRES_PASSWORD', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN',
  'INTEGRATIONS_ENC_KEY', 'AWS_ONBOARDING_ENC_KEY',
]);

/** Optional, but worth saying out loud when absent — a feature is off. */
const OPTIONAL = [
  'AWS_COGNITO_JWKS_URL',
  'POSTGRES_SSL',
  // Without these two the Accounts page returns an ExternalId but no launch link.
  'AWS_ONBOARDING_TEMPLATE_URL',
  'AEGIS_TASK_ROLE_ARN',
];

function validateAwsBackendEnv(env = process.env) {
  const missing = REQUIRED.filter((v) => !String(env[v] || '').trim());

  console.log('\n--- STARTUP CONFIGURATION REPORT ---');
  console.log('Loaded Provider: AWS');
  console.log('Configuration source: root .env (dotenv) — no secrets service is used.');

  if (missing.length > 0) {
    console.error('❌ FATAL ERROR: Missing required environment variables:');
    missing.forEach((v) => console.error(`  - ${v}`));
    console.error('Please check your root .env file (see .env.example).');
    console.log('------------------------------------\n');
    process.exit(1);
  }

  console.log('✅ All required variables present.');
  console.log(`Authentication: AWS Cognito, pool ${env.AWS_COGNITO_USER_POOL_ID}`);
  console.log(`Authorization:  PostgreSQL ${env.POSTGRES_USER}@${env.POSTGRES_HOST}:`
    + `${env.POSTGRES_PORT || 5432}/${env.POSTGRES_DB} (users.role)`);

  for (const name of OPTIONAL) {
    const set = !!String(env[name] || '').trim();
    // A secret is reported as set/not set; never echoed.
    console.log(`  ${name}: ${set ? (SECRETS.has(name) ? 'set' : env[name]) : 'not set'}`);
  }
  console.log('------------------------------------\n');
}

module.exports = { validateAwsBackendEnv, REQUIRED, OPTIONAL, SECRETS };

// ─── Self-check: node src/utils/validateEnv.js ──────────────────────────
if (require.main === module) {
  const assert = require('assert');

  // The Postgres settings are REQUIRED — authorization has no fallback.
  for (const v of ['POSTGRES_HOST', 'POSTGRES_DB', 'POSTGRES_USER', 'POSTGRES_PASSWORD']) {
    assert.ok(REQUIRED.includes(v), `${v} must be required — it is the authorization store`);
  }
  // The obsolete session secret must NOT be required (or requested) any more.
  assert.ok(!REQUIRED.includes('TENANT_SESSION_SECRET'));
  assert.ok(!OPTIONAL.includes('TENANT_SESSION_SECRET'));
  // No Cognito group name is configuration any more.
  for (const v of [...REQUIRED, ...OPTIONAL]) {
    assert.ok(!/GROUP/.test(v), `${v} must not exist — there are no authorization groups`);
  }

  // A complete environment passes without exiting.
  const ok = {
    AWS_COGNITO_CLIENT_ID: 'c', AWS_COGNITO_DOMAIN: 'd',
    AWS_COGNITO_ISSUER_URL: 'i', AWS_COGNITO_USER_POOL_ID: 'p',
    POSTGRES_HOST: 'h', POSTGRES_DB: 'db', POSTGRES_USER: 'u', POSTGRES_PASSWORD: 'hunter2',
    AWS_ONBOARDING_ENC_KEY: 'k1-secret', INTEGRATIONS_ENC_KEY: 'k2-secret',
  };
  const lines = [];
  const realLog = console.log; const realErr = console.error;
  console.log = (...a) => lines.push(a.join(' '));
  console.error = (...a) => lines.push(a.join(' '));
  validateAwsBackendEnv(ok);
  console.log = realLog; console.error = realErr;

  const report = lines.join('\n');
  // THE property: no secret value may appear in the startup report.
  assert.ok(!report.includes('hunter2'), 'POSTGRES_PASSWORD must never be printed');
  assert.ok(report.includes('All required variables present'));
  assert.ok(report.includes('users.role'), 'the report must name PostgreSQL as the authorization source');
  assert.ok(report.includes('no secrets service'), 'the report must state the configuration source');

  // A set secret is reported as "set", not as its value.
  lines.length = 0;
  console.log = (...a) => lines.push(a.join(' '));
  console.error = (...a) => lines.push(a.join(' '));
  validateAwsBackendEnv({ ...ok, AEGIS_TASK_ROLE_ARN: 'arn:aws:iam::1:role/t' });
  console.log = realLog; console.error = realErr;
  const report2 = lines.join('\n');
  assert.ok(!report2.includes('k1-secret') && !report2.includes('k2-secret'), 'sealing keys must never be echoed');
  assert.ok(report2.includes('AEGIS_TASK_ROLE_ARN: arn:aws:iam::1:role/t'), 'a non-secret may be shown');

  console.log('✅ utils/validateEnv.js self-check passed');
}
