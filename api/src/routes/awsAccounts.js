// AWS provisioning targets: onboard (CloudFormation quick-create of the
// AegisProvisioner role), list, remove, and — for the agent — mint short-lived
// STS credentials for a run.
//
// Every route is OWNER-SCOPED through aws/accountStore.js: a user onboards and
// provisions into their own accounts only. There is no admin override and no
// internal-key lane; credentials are issued only to the account's owner.

const express = require('express');
const { STSClient, GetCallerIdentityCommand } = require('@aws-sdk/client-sts');
const { S3Client, HeadBucketCommand } = require('@aws-sdk/client-s3');

const { requireAuthenticated, requireAdmin } = require('../middleware/auth');
const store = require('../aws/accountStore');
const roleProvider = require('../aws/roleProvider');

const router = express.Router();

const region = () => process.env.AWS_REGION || 'us-east-1';

// CloudFormation quick-create for infra/onboarding/aegis-provisioner-role.yaml.
// The role trusts exactly one principal: this deployment's task role.
function buildLaunchUrl(externalId) {
  const templateUrl = (process.env.AWS_ONBOARDING_TEMPLATE_URL || '').trim();
  const trustedRoleArn = (process.env.AEGIS_TASK_ROLE_ARN || '').trim();
  if (!templateUrl || !trustedRoleArn) return null;
  const params = new URLSearchParams({
    templateURL: templateUrl,
    stackName: 'AEGIS-Provisioner',
    param_TrustedRoleArn: trustedRoleArn,
    param_ExternalId: externalId,
  });
  return `https://${region()}.console.aws.amazon.com/cloudformation/home?region=${region()}#/stacks/quickcreate?${params}`;
}

// AegisProvisioner-<suffix>  ->  its sibling policy AegisBoundary-<suffix>,
// both created by the same CloudFormation stack.
function boundaryArnFor(roleArn) {
  const m = /^arn:(aws(?:-cn|-us-gov)?):iam::(\d{12}):role\/(?:.*\/)?AegisProvisioner-([\w-]+)$/.exec(roleArn || '');
  return m ? `arn:${m[1]}:iam::${m[2]}:policy/AegisBoundary-${m[3]}` : undefined;
}

function accountIdFromArn(roleArn) {
  const match = /^arn:aws(?:-cn|-us-gov)?:iam::(\d{12}):role\//.exec(roleArn || '');
  return match ? match[1] : null;
}

// ─── Start: server-generated ExternalId bound to the caller ─────────────
router.post('/api/aws-accounts/start', requireAuthenticated, async (req, res) => {
  const { id, externalId } = await store.createPending(req.user.id);
  const launchUrl = buildLaunchUrl(externalId);
  if (!launchUrl) {
    console.warn('[AwsAccounts] AWS_ONBOARDING_TEMPLATE_URL / AEGIS_TASK_ROLE_ARN not set — no launch link.');
  }
  res.json({
    onboardingId: id,
    externalId,
    launchUrl,
    trustedRoleArn: process.env.AEGIS_TASK_ROLE_ARN || null,
  });
});

// ─── Complete: prove the role works, then activate ───────────────────────
router.post('/api/aws-accounts/complete', requireAuthenticated, async (req, res) => {
  const onboardingId = String(req.body?.onboardingId || '').trim();
  const roleArn = String(req.body?.roleArn || '').trim();
  const displayName = String(req.body?.displayName || '').trim() || undefined;

  if (!roleProvider.validateRoleArn(roleArn)) {
    return res.status(400).json({ error: 'Not a valid IAM role ARN (arn:aws:iam::<account>:role/<name>).', code: 'ROLE_ARN_INVALID' });
  }
  const pending = onboardingId ? await store.getPending(req.user.id, onboardingId) : null;
  if (!pending) {
    return res.status(404).json({ error: 'Onboarding session not found or expired — start again.', code: 'ONBOARDING_NOT_FOUND' });
  }
  const accountId = accountIdFromArn(roleArn);
  const stateBucket = store.stateBucketFor(roleArn);
  if (!stateBucket) {
    return res.status(400).json({ error: 'Expected the role created by the AEGIS template (AegisProvisioner-<suffix>).', code: 'ROLE_ARN_INVALID' });
  }

  let creds;
  try {
    creds = await roleProvider.assumeRole(roleArn, `aegis-onboarding-${Date.now()}`, pending.externalId);
    const identity = await roleProvider.verifyCredentials(creds);
    if (identity.account !== accountId) {
      return res.status(400).json({ error: `Assumed role reports account ${identity.account}, expected ${accountId}.` });
    }
  } catch (err) {
    return res.status(400).json({ error: err instanceof Error ? err.message : 'Role validation failed', code: 'ROLE_ASSUME_FAILED' });
  }

  // Terraform state lives in the account, in the bucket the same stack creates.
  // Checked here so a stack from an older template, or one created in another
  // region, fails at onboarding instead of at the first `terraform init`.
  try {
    const s3 = new S3Client({
      region: region(),
      credentials: { accessKeyId: creds.accessKeyId, secretAccessKey: creds.secretAccessKey, sessionToken: creds.sessionToken },
    });
    await s3.send(new HeadBucketCommand({ Bucket: stateBucket }));
  } catch (err) {
    console.warn(`[AwsAccounts] state bucket check failed for ${accountId}: ${err.name || err.message}`);
    return res.status(400).json({
      error: `The Terraform state bucket ${stateBucket} was not found in ${region()}. Create (or update) the AEGIS stack from the current template, in ${region()}.`,
      code: 'ONBOARDING_STATE_BUCKET_MISSING',
    });
  }

  const row = await store.activate(req.user.id, onboardingId, { accountId, roleArn, displayName });
  console.log(`[AwsAccounts] ${req.user.email} onboarded account ${accountId}`);
  res.json({ ok: true, account: store.toPublicAccount(row) });
});

// ─── List the caller's accounts (feeds the run picker) ───────────────────
router.get('/api/aws-accounts', requireAuthenticated, async (req, res) => {
  const accounts = (await store.listAccounts(req.user.id)).map(store.toPublicAccount);
  res.json({ accounts });
});

// ─── Which account does AEGIS itself run in? Admins only: clients never
// need the operator's account id. ─────────────────────────────────────────
let cachedIdentity;
router.get('/api/aws-accounts/whoami', requireAuthenticated, requireAdmin, async (_req, res) => {
  if (cachedIdentity) return res.json(cachedIdentity);
  try {
    const out = await new STSClient({ region: region() }).send(new GetCallerIdentityCommand({}));
    cachedIdentity = { accountId: out.Account, source: 'sts' };
  } catch (err) {
    console.warn('[AwsAccounts] could not resolve this deployment\'s account id:', err.message);
    return res.json({ accountId: null, source: 'unavailable' });
  }
  res.json(cachedIdentity);
});

// ─── Remove one of the caller's accounts ─────────────────────────────────
router.delete('/api/aws-accounts/:accountId', requireAuthenticated, async (req, res) => {
  const removed = await store.deleteAccount(req.user.id, req.params.accountId);
  if (!removed) return res.status(404).json({ error: 'Account not found' });
  console.log(`[AwsAccounts] ${req.user.email} removed account ${req.params.accountId}`);
  res.json({ ok: true });
});

// ─── For the agent: short-lived STS creds for the caller's own account ───
// The agent forwards the USER'S token, so ownership is enforced here, at the
// point credentials are issued. Not reachable from outside: the ALB answers
// /api/*/internal/* with a fixed 404; the agent calls over localhost.
router.get('/api/aws-accounts/internal/provisioning-creds', requireAuthenticated, async (req, res) => {
  const accountId = String(req.query.accountId || '').trim();
  if (!accountId) return res.status(400).json({ error: 'accountId is required', code: 'ACCOUNT_ID_REQUIRED' });

  const account = await store.getAccountCreds(req.user.id, accountId);
  if (!account) return res.status(404).json({ error: 'Unknown AWS account', code: 'PROVISIONING_ACCOUNT_UNKNOWN' });

  try {
    const creds = await roleProvider.assumeRole(account.roleArn, `aegis-run-${req.user.id.slice(0, 8)}-${Date.now()}`, account.externalId);
    res.json({
      AWS_ACCESS_KEY_ID: creds.accessKeyId,
      AWS_SECRET_ACCESS_KEY: creds.secretAccessKey,
      AWS_SESSION_TOKEN: creds.sessionToken,
      AWS_REGION: region(),
      // Every role the agent writes must carry this boundary (the provisioner
      // role cannot create one without it). Reaches Terraform as a TF_VAR.
      TF_VAR_aegis_permissions_boundary: boundaryArnFor(account.roleArn),
      // Where this account's Terraform state lives (the agent passes these as
      // -backend-config; Terraform reaches the bucket with the creds above).
      AEGIS_STATE_BUCKET: store.stateBucketFor(account.roleArn),
      AEGIS_STATE_REGION: region(),
    });
  } catch (err) {
    res.status(502).json({ error: err instanceof Error ? err.message : 'Failed to assume the account role' });
  }
});

module.exports = router;

// ─── Self-check: node src/routes/awsAccounts.js ─────────────────────────
// Every route must require authentication — ownership scoping in the store
// is meaningless if a route never learns who is asking.
if (require.main === module) {
  const assert = require('assert');
  const layers = router.stack.filter((l) => l.route);
  assert.ok(layers.length >= 6, 'expected the six account routes');
  for (const l of layers) {
    const names = l.route.stack.map((h) => h.name);
    assert.ok(names.includes('requireAuthenticated'), `${l.route.path} must require authentication`);
  }
  const creds = layers.filter((l) => l.route.path.includes('provisioning-creds'));

  // State bucket naming matches the CloudFormation template.
  assert.strictEqual(store.stateBucketFor('arn:aws:iam::123456789012:role/AegisProvisioner-default'), 'aegis-tfstate-123456789012-default');
  assert.strictEqual(store.stateBucketFor('arn:aws:iam::123456789012:role/path/AegisProvisioner-team-a'), 'aegis-tfstate-123456789012-team-a');
  assert.strictEqual(store.stateBucketFor('arn:aws:iam::123456789012:role/SomethingElse'), undefined);
  assert.strictEqual(store.stateBucketFor('arn:aws:iam::123456789012:role/AegisProvisioner-Upper'), undefined);
  assert.strictEqual(creds.length, 1, 'exactly one provisioning-creds registration (no internal-key lane)');
  assert.strictEqual(accountIdFromArn('arn:aws:iam::123456789012:role/AegisProvisioner-x'), '123456789012');
  assert.strictEqual(accountIdFromArn('arn:aws:iam::bad:role/x'), null);
  assert.strictEqual(boundaryArnFor('arn:aws:iam::123456789012:role/AegisProvisioner-default'),
    'arn:aws:iam::123456789012:policy/AegisBoundary-default');
  assert.strictEqual(boundaryArnFor('arn:aws:iam::123456789012:role/SomethingElse'), undefined);
  console.log('✅ routes/awsAccounts.js self-check passed');
}
