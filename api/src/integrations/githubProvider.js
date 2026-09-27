/**
 * GitHub-side helpers for the integrations feature. Phase 1: token-mode
 * only (personal access token, Bearer auth). OAuth (buildAuthorizeUrl/
 * exchangeCode/revokeGrant) lands in a later phase.
 */

const REPO_FULL_NAME_RE = /^[\w.-]+\/[\w.-]+$/;
const API_BASE = 'https://api.github.com';

class GitHubApiError extends Error {
  constructor(message, code, status) {
    super(message);
    this.code = code;
    this.status = status;
  }
}

function isValidRepoFullName(repoFullName) {
  return REPO_FULL_NAME_RE.test(String(repoFullName || '').trim());
}

function authHeaders(token) {
  return {
    Authorization: `Bearer ${token}`,
    Accept: 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28',
  };
}

/** Live-validates a token and returns the caller's identity + granted scopes. */
async function verifyIdentity(token) {
  let resp;
  try {
    resp = await fetch(`${API_BASE}/user`, { headers: authHeaders(token) });
  } catch (err) {
    throw new GitHubApiError(`Could not reach GitHub: ${err.message}`, 'github_unreachable', 502);
  }
  if (resp.status === 401) throw new GitHubApiError('GitHub rejected that token.', 'github_unauthorized', 401);
  if (!resp.ok) throw new GitHubApiError(`GitHub returned an unexpected error (${resp.status}).`, 'github_error', 502);

  const scopesHeader = resp.headers.get('x-oauth-scopes') || '';
  const me = await resp.json();
  return {
    login: me.login,
    id: me.id,
    avatarUrl: me.avatar_url,
    scopes: scopesHeader ? scopesHeader.split(',').map((s) => s.trim()).filter(Boolean) : [],
  };
}

/** Live-validates that the token can push to a specific repo, returns its default branch. */
async function verifyRepoAccess(token, repoFullName) {
  if (!isValidRepoFullName(repoFullName)) {
    throw new GitHubApiError('Not a valid repository (expected owner/repo).', 'github_invalid_repo', 400);
  }
  let resp;
  try {
    resp = await fetch(`${API_BASE}/repos/${repoFullName}`, { headers: authHeaders(token) });
  } catch (err) {
    throw new GitHubApiError(`Could not reach GitHub: ${err.message}`, 'github_unreachable', 502);
  }
  if (resp.status === 404) throw new GitHubApiError(`Repository ${repoFullName} was not found, or this token can't see it.`, 'github_repo_not_found', 404);
  if (resp.status === 401) throw new GitHubApiError('GitHub rejected that token.', 'github_unauthorized', 401);
  if (!resp.ok) throw new GitHubApiError(`GitHub returned an unexpected error (${resp.status}).`, 'github_error', 502);

  const repo = await resp.json();
  if (!repo.permissions || repo.permissions.push !== true) {
    throw new GitHubApiError(`This token can see ${repoFullName} but isn't allowed to push to it.`, 'github_no_write', 403);
  }
  // An empty repository reports a default_branch that does not exist yet. That
  // is no longer a reason to refuse the connection: the agent creates the first
  // commit (a README and .gitignore) when it first pushes. `size` is on-disk
  // kilobytes and lags a push, so confirm with the branch itself.
  const defaultBranch = repo.default_branch || 'main';
  const isEmpty = repo.size === 0 && !(await branchExists(token, repoFullName, defaultBranch));
  return { defaultBranch, isEmpty };
}

/** True if the given branch exists in the repo. */
async function branchExists(token, repoFullName, branch) {
  try {
    const resp = await fetch(`${API_BASE}/repos/${repoFullName}/branches/${encodeURIComponent(branch)}`, {
      headers: authHeaders(token),
    });
    return resp.status === 200;
  } catch {
    return false;
  }
}

/** Lists repos the token can reach, filtered to push-capable ones by the caller if desired. */
async function listRepos(token, { page = 1, perPage = 100 } = {}) {
  let resp;
  try {
    resp = await fetch(
      `${API_BASE}/user/repos?per_page=${perPage}&page=${page}&sort=updated&affiliation=owner,collaborator,organization_member`,
      { headers: authHeaders(token) }
    );
  } catch (err) {
    throw new GitHubApiError(`Could not reach GitHub: ${err.message}`, 'github_unreachable', 502);
  }
  if (resp.status === 401) throw new GitHubApiError('GitHub rejected that token.', 'github_unauthorized', 401);
  if (!resp.ok) throw new GitHubApiError(`GitHub returned an unexpected error (${resp.status}).`, 'github_error', 502);

  const repos = await resp.json();
  const linkHeader = resp.headers.get('link') || '';
  return {
    repos: repos.map((r) => ({
      fullName: r.full_name,
      private: r.private,
      defaultBranch: r.default_branch || 'main',
      canPush: Boolean(r.permissions?.push),
    })),
    hasMore: linkHeader.includes('rel="next"'),
  };
}

module.exports = {
  isValidRepoFullName,
  verifyIdentity,
  verifyRepoAccess,
  branchExists,
  listRepos,
  GitHubApiError,
  isOAuthConfigured: () => Boolean(process.env.GITHUB_OAUTH_CLIENT_ID && process.env.GITHUB_OAUTH_CLIENT_SECRET),
};
