/**
 * JIRA-side helpers for the integrations feature. Phase 1: token-mode only
 * (email + API token, Basic auth). OAuth (buildAuthorizeUrl/exchangeCode/
 * refreshAccessToken/listAccessibleResources) lands in a later phase — the
 * shape here is deliberately left open for it (see JiraApiError `code`s).
 */

const SITE_URL_RE = /^https:\/\/[a-z0-9][a-z0-9-]*\.atlassian\.net\/?$/i;

class JiraApiError extends Error {
  constructor(message, code, status) {
    super(message);
    this.code = code;
    this.status = status;
  }
}

function isValidSiteUrl(siteUrl) {
  return SITE_URL_RE.test(String(siteUrl || '').trim());
}

function normalizeSiteUrl(siteUrl) {
  return String(siteUrl || '').trim().replace(/\/+$/, '');
}

function basicAuthHeader(email, apiToken) {
  return 'Basic ' + Buffer.from(`${email}:${apiToken}`).toString('base64');
}

/**
 * Live-validates an email+token pair against a Jira site and returns the
 * caller's identity. Throws JiraApiError with a `code` the routes map to a
 * friendly message.
 */
async function verifyIdentity({ siteUrl, email, apiToken }) {
  if (!isValidSiteUrl(siteUrl)) {
    throw new JiraApiError('Not a valid Jira site URL (expected https://yourcompany.atlassian.net).', 'jira_invalid_url', 400);
  }
  const base = normalizeSiteUrl(siteUrl);
  let resp;
  try {
    resp = await fetch(`${base}/rest/api/3/myself`, {
      headers: { Authorization: basicAuthHeader(email, apiToken), Accept: 'application/json' },
    });
  } catch (err) {
    throw new JiraApiError(`Could not reach ${base}: ${err.message}`, 'jira_unreachable', 502);
  }
  if (resp.status === 401) throw new JiraApiError('Jira rejected that email/token combination.', 'jira_unauthorized', 401);
  if (resp.status === 403) throw new JiraApiError('That account is not allowed to access this Jira site.', 'jira_forbidden', 403);
  if (!resp.ok) throw new JiraApiError(`Jira returned an unexpected error (${resp.status}).`, 'jira_error', 502);

  const me = await resp.json();
  return {
    accountId: me.accountId,
    emailAddress: me.emailAddress || email,
    displayName: me.displayName || me.emailAddress || email,
  };
}

module.exports = {
  isValidSiteUrl,
  normalizeSiteUrl,
  basicAuthHeader,
  verifyIdentity,
  JiraApiError,
  isOAuthConfigured: () => Boolean(process.env.JIRA_OAUTH_CLIENT_ID && process.env.JIRA_OAUTH_CLIENT_SECRET),
};
