/**
 * SQL migrations — plain files, applied in filename order, recorded once.
 *
 * `backend/migrations/NNN_name.sql` is the whole mechanism. Each file runs
 * inside a transaction together with the INSERT that records it, so a failed
 * migration leaves neither a half-applied schema nor a row claiming it ran.
 *
 * Run at startup (server.js) and standalone:
 *   node src/db/migrate.js
 */

const fs = require('fs');
const path = require('path');

const db = require('./pool');

const MIGRATIONS_DIR = path.resolve(__dirname, '../../migrations');

// `name` rather than `filename`: an existing deployment already tracks applied
// migrations under that column, and renaming it would replay every migration
// against a live database.
const TRACKING_TABLE = `
  CREATE TABLE IF NOT EXISTS schema_migrations (
    name       TEXT        PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
  )`;

/** Every .sql file, in lexical order — which is why they are numbered. */
function migrationFiles(dir = MIGRATIONS_DIR) {
  // A MISSING directory is not "nothing to run" — it is a broken build, and
  // returning [] made the two indistinguishable: the app logged "schema up to
  // date", opened the port, and then failed at runtime on a table that was
  // never created. That is exactly what happened when backend/Dockerfile
  // shipped without this directory. Authorization has no fallback here, so
  // neither should its schema.
  if (!fs.existsSync(dir)) {
    throw new Error(
      `migrations directory not found at ${dir}. The image or checkout is `
      + 'incomplete — every migration would be silently skipped.',
    );
  }
  return fs.readdirSync(dir).filter((f) => f.endsWith('.sql')).sort();
}

async function migrate() {
  await db.query(TRACKING_TABLE);
  const { rows } = await db.query('SELECT name FROM schema_migrations');
  const applied = new Set(rows.map((r) => r.name));

  const pending = migrationFiles().filter((f) => !applied.has(f));
  if (pending.length === 0) {
    console.log(`[Migrate] schema up to date (${applied.size} applied)`);
    return { applied: [], alreadyApplied: applied.size };
  }

  const client = await db.connect();
  const ran = [];
  try {
    for (const filename of pending) {
      const sql = fs.readFileSync(path.join(MIGRATIONS_DIR, filename), 'utf8');
      // One transaction PER migration: an early file that succeeded stays
      // applied when a later one fails, so a re-run resumes rather than
      // replaying work that already landed.
      await client.query('BEGIN');
      try {
        await client.query(sql);
        await client.query('INSERT INTO schema_migrations (name) VALUES ($1)', [filename]);
        await client.query('COMMIT');
      } catch (err) {
        await client.query('ROLLBACK');
        throw new Error(`migration ${filename} failed: ${err.message}`);
      }
      ran.push(filename);
      console.log(`[Migrate] applied ${filename}`);
    }
  } finally {
    client.release();
  }
  return { applied: ran, alreadyApplied: applied.size };
}

module.exports = { migrate, migrationFiles, MIGRATIONS_DIR };

if (require.main === module) {
  const dotenv = require('dotenv');
  dotenv.config({ path: path.resolve(__dirname, '../../../.env') });
  migrate()
    .then((r) => { console.log(`✅ migrations done (${r.applied.length} new)`); return db.close(); })
    .then(() => process.exit(0))
    .catch((err) => { console.error('❌ migration failed:', err.message); process.exit(1); });
}
