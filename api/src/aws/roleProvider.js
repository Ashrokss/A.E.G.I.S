/**
 * AWS Role Provider (runtime CommonJS implementation)
 *
 * Real STS AssumeRole via @aws-sdk/client-sts. Plain CommonJS — the AWS
 * backend runs with plain `node`, no TS build step, so this is required
 * directly rather than compiled from a TS source.
 *
 * Credentials used to call AssumeRole come from the default AWS SDK chain —
 * i.e. this process's own static scanner credentials (AWS_ACCESS_KEY_ID/
 * AWS_SECRET_ACCESS_KEY in the container env). That scanner principal must
 * itself be granted sts:AssumeRole (see infra/aws/aws-setup.sh) — it is NOT
 * included in the SecurityAudit/ViewOnlyAccess managed policies.
 */

const { STSClient, AssumeRoleCommand, GetCallerIdentityCommand } = require('@aws-sdk/client-sts');

/** arn:aws:iam::ACCOUNT_ID:role/ROLE_PATH — supports aws/aws-cn/aws-us-gov partitions. */
const IAM_ROLE_ARN_REGEX = /^arn:aws(-cn|-us-gov)?:iam::\d{12}:role\/[\w+=,.@\-/]+$/;

function validateRoleArn(roleArn) {
  return typeof roleArn === 'string' && IAM_ROLE_ARN_REGEX.test(roleArn);
}

function friendlyStsError(err) {
  const code = err?.name || err?.Code || '';
  if (code === 'AccessDenied') {
    return new Error(
      'AssumeRole was denied. Check that: (1) the Role ARN and External ID match exactly ' +
      'what the client\'s CloudFormation stack created, and (2) this app\'s scanner IAM user ' +
      'has an sts:AssumeRole policy (see infra/aws/aws-setup.sh).'
    );
  }
  if (code === 'ValidationError' || code === 'MalformedPolicyDocumentException') {
    return new Error(`AssumeRole request was invalid: ${err.message}`);
  }
  return err instanceof Error ? err : new Error(String(err));
}

/**
 * Assume a client's cross-account role and return temporary credentials.
 * @param {string} roleArn
 * @param {string} sessionName
 * @param {string} [externalId]
 * @param {number} [durationSeconds=3600]
 * @returns {Promise<{accessKeyId:string, secretAccessKey:string, sessionToken:string, expiration:string}>}
 */
async function assumeRole(roleArn, sessionName, externalId, durationSeconds = 3600) {
  if (!validateRoleArn(roleArn)) {
    throw new Error(`Not a valid IAM role ARN: ${roleArn}`);
  }
  const client = new STSClient({ region: process.env.AWS_REGION || 'us-east-1' });
  try {
    const resp = await client.send(new AssumeRoleCommand({
      RoleArn: roleArn,
      RoleSessionName: sessionName,
      ExternalId: externalId || undefined,
      DurationSeconds: durationSeconds,
    }));
    const creds = resp.Credentials;
    if (!creds) throw new Error('STS returned no credentials');
    return {
      accessKeyId: creds.AccessKeyId,
      secretAccessKey: creds.SecretAccessKey,
      sessionToken: creds.SessionToken,
      expiration: creds.Expiration instanceof Date ? creds.Expiration.toISOString() : String(creds.Expiration),
    };
  } catch (err) {
    throw friendlyStsError(err);
  }
}

/**
 * Cheap read-only check that a set of temporary credentials actually work —
 * used right after assumeRole() during onboarding validation, before we
 * persist the client's role ARN.
 * @param {{accessKeyId:string, secretAccessKey:string, sessionToken:string}} creds
 * @returns {Promise<{account:string, arn:string}>}
 */
async function verifyCredentials(creds) {
  const client = new STSClient({
    region: process.env.AWS_REGION || 'us-east-1',
    credentials: {
      accessKeyId: creds.accessKeyId,
      secretAccessKey: creds.secretAccessKey,
      sessionToken: creds.sessionToken,
    },
  });
  const resp = await client.send(new GetCallerIdentityCommand({}));
  return { account: resp.Account, arn: resp.Arn };
}

/**
 * Not implemented — org-wide role discovery is out of scope for the
 * single-role-per-client onboarding model. See IAwsOrganizationProvider
 * (common/interfaces/aws-organization-provider.ts) for the future,
 * AWS-Organizations-wide design this would extend into.
 */
async function listAvailableRoles(_accountId) {
  throw new Error('[AwsRole] Role listing not implemented — single-role-per-client onboarding only.');
}

module.exports = { assumeRole, verifyCredentials, listAvailableRoles, validateRoleArn };
