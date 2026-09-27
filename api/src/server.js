const express = require('express');
const cors = require('cors');
const path = require('path');
const dotenv = require('dotenv');

dotenv.config({ path: path.resolve(__dirname, '../../.env') });

const { validateAwsBackendEnv } = require('./utils/validateEnv');
validateAwsBackendEnv();
const db = require('./db/pool');
const { migrate } = require('./db/migrate');
const awsAccountsRoutes = require('./routes/awsAccounts');
const integrationsRoutes = require('./routes/integrations');
const usersRoutes = require('./routes/users');

const app = express();
app.use(cors());
app.use(express.json());
app.use(express.urlencoded({ extended: true }));

// ─── Configuration from environment variables ──────────────────────
const AWS_COGNITO_DOMAIN = process.env.AWS_COGNITO_DOMAIN || '';
const AWS_COGNITO_CLIENT_ID = process.env.AWS_COGNITO_CLIENT_ID || '';
const AWS_COGNITO_ISSUER_URL = process.env.AWS_COGNITO_ISSUER_URL || '';
const AWS_COGNITO_USER_POOL_ID = process.env.AWS_COGNITO_USER_POOL_ID || '';

function checkConfig() {
  const missing = [];
  if (!AWS_COGNITO_DOMAIN) missing.push('AWS_COGNITO_DOMAIN');
  if (!AWS_COGNITO_CLIENT_ID) missing.push('AWS_COGNITO_CLIENT_ID');
  if (!AWS_COGNITO_ISSUER_URL) missing.push('AWS_COGNITO_ISSUER_URL');
  if (!AWS_COGNITO_USER_POOL_ID) missing.push('AWS_COGNITO_USER_POOL_ID');
  return missing;
}

const missingVars = checkConfig();
if (missingVars.length > 0) {
  console.warn(`[AwsAuth] WARNING: Missing environment variables: ${missingVars.join(', ')}`);
} else {
  console.log('[AwsAuth] Cognito Configuration OK');
}

// ─── Health check ──────────────────────────────────────────────────
// The database is part of health, not a detail: Cognito authenticates but
// PostgreSQL authorizes, so a backend that cannot reach it can serve nobody.
app.get('/health', async (_req, res) => {
  let database = 'ok';
  try {
    await db.ping();
  } catch (err) {
    database = 'unreachable';
    console.error('[Health] PostgreSQL unreachable:', err.message);
  }
  res.status(database === 'ok' ? 200 : 503).json({
    status: database === 'ok' ? 'ok' : 'degraded',
    provider: 'aws-cognito',
    database,
    timestamp: new Date().toISOString(),
  });
});

// ─── Config Status ─────────────────────────────────────────────────
app.get('/auth/aws/config-status', (_req, res) => {
  const missing = checkConfig();
  res.json({
    status: {
      configured: missing.length === 0,
      missingVars: missing,
      provider: 'aws-cognito',
    },
  });
});

// ─── SSO Callback — Token Exchange ─────────────────────────────────
app.post('/auth/aws/callback', async (req, res) => {
  const { code, redirect_uri, code_verifier } = req.body;

  if (!code) {
    return res.status(400).json({ error: 'Missing authorization code' });
  }

  const missing = checkConfig();
  if (missing.length > 0) {
    return res.status(503).json({ error: `Cognito not configured. Missing: ${missing.join(', ')}` });
  }

  const tokenEndpoint = `${AWS_COGNITO_DOMAIN}/oauth2/token`;
  
  // Cognito requires application/x-www-form-urlencoded
  const params = new URLSearchParams();
  params.append('grant_type', 'authorization_code');
  params.append('client_id', AWS_COGNITO_CLIENT_ID);
  params.append('code', code);
  params.append('redirect_uri', redirect_uri || 'http://localhost:5174/auth/callback');
  if (code_verifier) {
    params.append('code_verifier', code_verifier);
  }

  console.log(`[AwsAuth] Exchanging code at ${tokenEndpoint}`);

  try {
    const response = await fetch(tokenEndpoint, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded',
      },
      body: params.toString(),
    });

    const data = await response.json();

    if (!response.ok) {
      console.error(`[AwsAuth] Token exchange failed: ${response.status}`, data);
      return res.status(response.status).json({
        error: data.error || 'Token exchange failed',
        error_description: data.error_description
      });
    }

    console.log('[AwsAuth] Token exchange successful');

    res.json({
      tokens: {
        accessToken: data.access_token,
        idToken: data.id_token,
        refreshToken: data.refresh_token,
        expiresAt: Date.now() + ((data.expires_in || 3600) * 1000),
      },
    });
  } catch (err) {
    console.error('[AwsAuth] Token exchange error:', err);
    res.status(500).json({ error: `Token exchange failed: ${err.message}` });
  }
});

// ─── Refresh ───────────────────────────────────────────────────────
// The access token lives an hour. Without this, an hour is also the lifetime
// of the whole session: getAccessToken() in the SPA starts returning null and
// every authenticated control fails while unauthenticated ones keep working,
// which reads as "the app half-broke" rather than "you were signed out".
//
// Same public app client and same token endpoint as the code exchange above,
// with grant_type=refresh_token. Cognito returns a new access and ID token and
// does NOT return a new refresh token, so the caller keeps the one it has.
//
// The refresh token is sent by the client and used here immediately; it is
// never logged and never stored server-side.
app.post('/auth/aws/refresh', async (req, res) => {
  const refreshToken = req.body?.refresh_token;
  if (!refreshToken) {
    return res.status(400).json({ error: 'Missing refresh token' });
  }

  const missing = checkConfig();
  if (missing.length > 0) {
    return res.status(503).json({ error: `Cognito not configured. Missing: ${missing.join(', ')}` });
  }

  const params = new URLSearchParams();
  params.append('grant_type', 'refresh_token');
  params.append('client_id', AWS_COGNITO_CLIENT_ID);
  params.append('refresh_token', refreshToken);

  try {
    const response = await fetch(`${AWS_COGNITO_DOMAIN}/oauth2/token`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: params.toString(),
    });
    const data = await response.json();

    if (!response.ok) {
      // A revoked or expired refresh token is a normal end-of-session, not a
      // server fault: 401 tells the SPA to sign in again rather than retry.
      console.warn(`[AwsAuth] Refresh failed: ${response.status} ${data.error || ''}`);
      return res.status(response.status === 400 ? 401 : response.status).json({
        error: data.error || 'Refresh failed',
        error_description: data.error_description,
      });
    }

    res.json({
      tokens: {
        accessToken: data.access_token,
        idToken: data.id_token,
        expiresAt: Date.now() + ((data.expires_in || 3600) * 1000),
      },
    });
  } catch (err) {
    console.error('[AwsAuth] Refresh error:', err.message);
    res.status(500).json({ error: `Refresh failed: ${err.message}` });
  }
});

// ─── Logout ────────────────────────────────────────────────────────
app.post('/auth/aws/logout', (_req, res) => {
  res.json({ status: 'ok' });
});

// ─── API Routes ────────────────────────────────────────────────────
// Routers declare absolute paths (/api/...).
app.use(awsAccountsRoutes);
app.use(integrationsRoutes);
// Authorization: /api/auth/me plus the Admin Portal's roster. The first admin
// is created by scripts/create-admin.js, never over HTTP.
app.use(usersRoutes);

// ─── Start server ──────────────────────────────────────────────────
// Migrations run BEFORE the port opens. There is no point accepting a request
// the `users` table cannot answer, and a half-migrated schema answering
// authorization questions is worse than a backend that has not started.
//
// A database that cannot be reached at boot is fatal: authorization has no
// fallback, so starting anyway would mean 503 on every request with the cause
// buried in a log line.
const PORT = process.env.PORT || 3005;

migrate()
  .then(() => {
    app.listen(PORT, '0.0.0.0', () => {
      console.log(`AEGIS api listening on port ${PORT}`);
    });
  })
  .catch((err) => {
    console.error('❌ FATAL: could not prepare the PostgreSQL schema:', err.message);
    console.error('   PostgreSQL is this deployment\'s authorization store — check POSTGRES_* in .env.');
    process.exit(1);
  });
