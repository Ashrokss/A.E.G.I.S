// Jira / GitHub connections: paste-a-token flow. Every credential is proven
// against the live API before anything is written.
//
// OWNER-SCOPED through integrations/store.js — a connection belongs to the user
// who created it, and only that user can list, test, use or delete it. There is
// deliberately no environment-variable "default" connection: in a multi-user
// deployment that would hand the operator's tokens to every user.

const express = require('express');

const { requireAuthenticated } = require('../middleware/auth');
const store = require('../integrations/store');
const jiraProvider = require('../integrations/jiraProvider');
const githubProvider = require('../integrations/githubProvider');

const router = express.Router();

// ─── List the caller's connections (feeds the pickers) ─────────────────────
router.get('/api/integrations', requireAuthenticated, async (req, res) => {
  const provider = String(req.query.provider || '').trim() || undefined;
  const rows = await store.listConnections(req.user.id, provider);
  res.json({ connections: rows.map(store.toPublicConnection) });
});

// ─── Create: Jira via API token ─────────────────────────────────────────────
router.post('/api/integrations/jira/token', requireAuthenticated, async (req, res) => {
  const siteUrl = jiraProvider.normalizeSiteUrl(req.body?.baseUrl);
  const email = String(req.body?.email || '').trim();
  const apiToken = String(req.body?.apiToken || '').trim();
  const defaultProject = String(req.body?.defaultProject || '').trim().toUpperCase() || undefined;
  const reviewTransitionName = String(req.body?.reviewTransitionName || '').trim() || 'In Review';
  const displayName = String(req.body?.displayName || '').trim() || undefined;

  if (!email || !apiToken) {
    return res.status(400).json({ error: 'email and apiToken are required.', code: 'jira_missing_fields' });
  }

  let identity;
  try {
    identity = await jiraProvider.verifyIdentity({ siteUrl, email, apiToken });
  } catch (err) {
    return res.status(err.status || 400).json({ error: err.message, code: err.code || 'jira_error' });
  }

  try {
    const record = await store.createConnection(req.user.id, {
      provider: 'jira',
      authMethod: 'token',
      displayName: displayName || siteUrl.replace(/^https:\/\//, ''),
      config: {
        siteUrl,
        email,
        apiToken,
        defaultProject,
        reviewTransitionName,
        accountId: identity.accountId,
        accountEmail: identity.emailAddress,
        accountDisplayName: identity.displayName,
      },
    });
    console.log(`[Integrations] ${req.user.email} created jira connection ${record.id}`);
    res.status(201).json({ connection: store.toPublicConnection(record) });
  } catch (err) {
    res.status(err.code === 'no_encryption_key' ? 503 : 500).json({ error: err.message });
  }
});

// ─── Create: GitHub via token ───────────────────────────────────────────────
router.post('/api/integrations/github/token', requireAuthenticated, async (req, res) => {
  const token = String(req.body?.token || '').trim();
  const repoFullName = String(req.body?.repoFullName || '').trim();
  const displayName = String(req.body?.displayName || '').trim() || undefined;
  let baseBranch = String(req.body?.baseBranch || '').trim();

  if (!token || !repoFullName) {
    return res.status(400).json({ error: 'token and repoFullName are required.', code: 'github_missing_fields' });
  }

  let identity;
  let repoInfo;
  try {
    identity = await githubProvider.verifyIdentity(token);
    repoInfo = await githubProvider.verifyRepoAccess(token, repoFullName);
  } catch (err) {
    return res.status(err.status || 400).json({ error: err.message, code: err.code || 'github_error' });
  }
  if (!baseBranch || repoInfo.isEmpty) {
    // An empty repository has no branch to check against — the agent creates it with the first commit.
    baseBranch = repoInfo.defaultBranch;
  } else if (!(await githubProvider.branchExists(token, repoFullName, baseBranch))) {
    return res.status(400).json({
      error: `There's no branch called "${baseBranch}" in ${repoFullName}.`,
      code: 'github_branch_not_found',
    });
  }

  try {
    const record = await store.createConnection(req.user.id, {
      provider: 'github',
      authMethod: 'token',
      displayName: displayName || repoFullName,
      config: {
        repoFullName,
        baseBranch,
        token,
        tokenKind: 'pat',
        login: identity.login,
        avatarUrl: identity.avatarUrl,
        scopes: identity.scopes,
        repoEmpty: repoInfo.isEmpty === true,
      },
    });
    console.log(`[Integrations] ${req.user.email} created github connection ${record.id}`);
    res.status(201).json({ connection: store.toPublicConnection(record) });
  } catch (err) {
    res.status(err.code === 'no_encryption_key' ? 503 : 500).json({ error: err.message });
  }
});

// ─── GitHub repo lookup (feeds the token form's repo picker) ───────────────
router.post('/api/integrations/github/repos/lookup', requireAuthenticated, async (req, res) => {
  const token = String(req.body?.token || '').trim();
  if (!token) return res.status(400).json({ error: 'token is required.' });

  try {
    const identity = await githubProvider.verifyIdentity(token);
    const { repos } = await githubProvider.listRepos(token, { page: 1, perPage: 100 });
    res.json({ connectedAs: identity.login, repos: repos.filter((r) => r.canPush) });
  } catch (err) {
    res.status(err.status || 400).json({ error: err.message, code: err.code || 'github_error' });
  }
});

// ─── Test one of the caller's connections ──────────────────────────────────
router.post('/api/integrations/:provider/:id/test', requireAuthenticated, async (req, res) => {
  const { provider, id } = req.params;
  const conn = await store.getConnection(req.user.id, id);
  if (!conn || conn.provider !== provider) return res.status(404).json({ error: 'Connection not found' });

  try {
    let connectedAs;
    if (provider === 'jira') {
      const identity = await jiraProvider.verifyIdentity({
        siteUrl: conn.config.siteUrl,
        email: conn.config.email,
        apiToken: conn.config.apiToken,
      });
      connectedAs = identity.emailAddress;
    } else {
      const identity = await githubProvider.verifyIdentity(conn.config.token);
      await githubProvider.verifyRepoAccess(conn.config.token, conn.config.repoFullName);
      connectedAs = identity.login;
    }
    await store.updateStatus(req.user.id, id, { status: 'active', validated: true });
    res.json({ ok: true, connectedAs });
  } catch (err) {
    await store.updateStatus(req.user.id, id, { status: 'needs_reauth', lastError: err.message });
    res.status(err.status || 400).json({ error: err.message, code: err.code || 'error' });
  }
});

// ─── Delete one of the caller's connections ────────────────────────────────
router.delete('/api/integrations/:provider/:id', requireAuthenticated, async (req, res) => {
  const removed = await store.deleteConnection(req.user.id, req.params.id);
  if (!removed) return res.status(404).json({ error: 'Connection not found' });
  console.log(`[Integrations] ${req.user.email} removed connection ${req.params.id}`);
  res.json({ ok: true });
});

// ─── For the agent: resolve a run's chosen connection ──────────────────────
// Called with the USER'S forwarded token, so a run can only use its owner's
// connections. Blocked at the ALB from outside (/api/*/internal/* → 404).
router.get('/api/integrations/internal/jira-creds', requireAuthenticated, async (req, res) => {
  const connectionId = String(req.query.connectionId || '').trim();
  if (!connectionId) return res.status(400).json({ error: 'connectionId is required' });

  const conn = await store.getConnection(req.user.id, connectionId);
  if (!conn || conn.provider !== 'jira') return res.status(404).json({ error: 'Unknown Jira connection' });

  res.json({
    connectionId,
    mode: 'token',
    apiBaseUrl: conn.config.siteUrl,
    authHeader: jiraProvider.basicAuthHeader(conn.config.email, conn.config.apiToken),
    siteUrl: conn.config.siteUrl,
    defaultProject: conn.config.defaultProject,
    reviewTransitionName: conn.config.reviewTransitionName,
    expiresAt: null,
  });
});

router.get('/api/integrations/internal/github-creds', requireAuthenticated, async (req, res) => {
  const connectionId = String(req.query.connectionId || '').trim();
  if (!connectionId) return res.status(400).json({ error: 'connectionId is required' });

  const conn = await store.getConnection(req.user.id, connectionId);
  if (!conn || conn.provider !== 'github') return res.status(404).json({ error: 'Unknown GitHub connection' });

  res.json({
    connectionId,
    token: conn.config.token,
    repoFullName: conn.config.repoFullName,
    baseBranch: conn.config.baseBranch,
  });
});

module.exports = router;

// ─── Self-check: node src/routes/integrations.js ───────────────────────────
if (require.main === module) {
  const assert = require('assert');
  const layers = router.stack.filter((l) => l.route);
  for (const l of layers) {
    assert.ok(l.route.stack.some((h) => h.name === 'requireAuthenticated'), `${l.route.path} must require authentication`);
  }
  assert.ok(!layers.some((l) => l.route.stack.some((h) => h.name === 'internalKeyOnly')), 'no internal-key lane');
  console.log('✅ routes/integrations.js self-check passed');
}
