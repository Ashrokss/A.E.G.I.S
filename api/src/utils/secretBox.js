/**
 * Shared AES-256-GCM secret-at-rest helper, extracted from
 * onboarding/store.js so the new integrations store (JIRA/GitHub tokens,
 * OAuth refresh tokens) can reuse the exact same envelope format
 * ("enc:v1:<iv>:<tag>:<ct>") without duplicating the crypto.
 *
 * Callers choose their own env var for the key and their own behavior when
 * no key is configured via `requireKey` — onboarding/store.js keeps its
 * existing lenient plaintext-with-warning fallback (an AWS external ID is
 * useless without the matching trust policy), while the integrations store
 * hard-fails (a bare OAuth refresh token or GitHub PAT is directly usable by
 * anyone who reads the file).
 */

const crypto = require('crypto');

const ENC_PREFIX = 'enc:v1:';

function parseKey(raw) {
  if (!raw) return null;
  try {
    const buf = /^[0-9a-fA-F]{64}$/.test(raw) ? Buffer.from(raw, 'hex') : Buffer.from(raw, 'base64');
    return buf.length === 32 ? buf : null;
  } catch {
    return null;
  }
}

function keyFromEnv(envVarNames) {
  for (const name of envVarNames) {
    const key = parseKey(process.env[name]?.trim());
    if (key) return key;
  }
  return null;
}

/**
 * @param {string[]} envVarNames tried in order, first valid 32-byte key wins
 * @param {{ requireKey?: boolean, warnLabel?: string }} [opts]
 */
function makeSecretBox(envVarNames, opts = {}) {
  const { requireKey = false, warnLabel = envVarNames[0] } = opts;
  let warned = false;

  function encryptSecret(plain) {
    const key = keyFromEnv(envVarNames);
    if (!key) {
      if (requireKey) {
        throw new Error(`${envVarNames.join(' / ')} not set (32-byte key, hex or base64) — refusing to store a secret in plaintext.`);
      }
      if (!warned) {
        console.warn(`[secretBox] ${warnLabel} not set — secrets stored in PLAINTEXT (dev only). Set a 32-byte key in production.`);
        warned = true;
      }
      return plain;
    }
    const iv = crypto.randomBytes(12);
    const cipher = crypto.createCipheriv('aes-256-gcm', key, iv);
    const ct = Buffer.concat([cipher.update(plain, 'utf8'), cipher.final()]);
    const tag = cipher.getAuthTag();
    return ENC_PREFIX + [iv.toString('base64'), tag.toString('base64'), ct.toString('base64')].join(':');
  }

  function decryptSecret(stored) {
    if (!stored || !stored.startsWith(ENC_PREFIX)) return stored || ''; // plaintext / legacy
    const key = keyFromEnv(envVarNames);
    if (!key) return '';
    const [ivB64, tagB64, ctB64] = stored.slice(ENC_PREFIX.length).split(':');
    if (!ivB64 || !tagB64 || !ctB64) return '';
    const decipher = crypto.createDecipheriv('aes-256-gcm', key, Buffer.from(ivB64, 'base64'));
    decipher.setAuthTag(Buffer.from(tagB64, 'base64'));
    return Buffer.concat([decipher.update(Buffer.from(ctB64, 'base64')), decipher.final()]).toString('utf8');
  }

  function hasKey() {
    return !!keyFromEnv(envVarNames);
  }

  return { encryptSecret, decryptSecret, hasKey };
}

module.exports = { makeSecretBox, ENC_PREFIX };
