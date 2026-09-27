#!/usr/bin/env node
/**
 * Create the FIRST AEGIS administrator: the Cognito account and the `users` row,
 * together, or neither.
 *
 *   node scripts/create-admin.js you@example.com
 *   (in AWS)  aws ecs execute-command ... --container api \
 *               --command "node scripts/create-admin.js you@example.com"
 *
 * There is deliberately no HTTP bootstrap endpoint: proving ownership over the
 * web would mean accepting raw IAM access keys in a form. Shell access to this
 * process IS that proof, so no HTTP surface is needed at all.
 *
 * Only works while `users` is empty (users.createFirstUser re-checks under an
 * advisory lock). Every later account is created in the Admin page.
 */
const path = require('path');
require('dotenv').config({ path: path.resolve(__dirname, '../../.env') });

const cognito = require('../src/cognito/users');
const users = require('../src/users/store');
const db = require('../src/db/pool');
const { migrate } = require('../src/db/migrate');

async function main(email) {
  if (!email || !email.includes('@')) {
    throw new Error('usage: node scripts/create-admin.js <email>');
  }
  if (!cognito.userPoolId()) throw new Error('AWS_COGNITO_USER_POOL_ID is not set.');
  await migrate();

  // Adopt an account already in the pool rather than failing; never touch its password.
  let account = await cognito.findByEmail(email);
  let tempPassword = null;
  const createdHere = !account;
  if (createdHere) {
    // No password → Cognito issues a temporary one and forces a change at first sign-in.
    account = await cognito.createUser({ email });
    tempPassword = account.temporaryPassword;
  }

  let row;
  try {
    row = await users.createFirstUser({ cognitoSub: account.cognitoSub, email });
  } catch (err) {
    if (createdHere) await cognito.removeUser(account.username).catch(() => {});
    throw err;
  }
  if (!row) {
    if (createdHere) await cognito.removeUser(account.username).catch(() => {});
    throw new Error('AEGIS already has users. Create further accounts from the Admin page.');
  }

  console.log(`✅ Admin created: ${row.email}`);
  if (tempPassword) {
    console.log(`   Temporary password (shown once, change it at first sign-in): ${tempPassword}`);
  } else {
    console.log('   Existing Cognito account adopted; its password is unchanged.');
  }
}

main(String(process.argv[2] || '').trim())
  .catch((err) => {
    console.error(`❌ ${err.message}`);
    process.exitCode = 1;
  })
  .finally(() => db.close());
