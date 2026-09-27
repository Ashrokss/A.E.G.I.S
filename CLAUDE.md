# AEGIS

## Stack

- **agent/**: Python 3.11, FastAPI, Pydantic 2, SQLite through the stdlib `sqlite3` module with raw SQL (`runs.db`, no ORM, no migration tool: `CREATE TABLE IF NOT EXISTS` plus additive `ALTER TABLE` wrapped in `except sqlite3.OperationalError`). Port 8000. Routes are declared as `/api/*`; the public prefix `/agent-api/*` is mapped onto them by `_strip_public_prefix` in `main.py` (the ALB cannot rewrite paths).
- **api/**: Node 20, Express 4, `pg` with raw SQL. Port 3005. `src/db/pool.js` is the only module that constructs the pool; `migrations/NNN_*.sql` are applied in order at start-up by `src/db/migrate.js`.
- **web/**: React 19, Vite, TypeScript, Tailwind v4, TanStack Query, react-router, GSAP (`gsap` + `@gsap/react`: `useGSAP` with a `scope`, plugins registered once per module; the sign-in page is one timeline) and motion. Components: shadcn/ui in `src/components/ui/` and SmoothUI (shadcn registry `@smoothui`, see `web/components.json`) in `src/components/smoothui/`, added with `npx shadcn@latest add ...`. Both are themed by the AEGIS tokens in `src/index.css` (shadcn/SmoothUI token names map onto `--teal`, `--amber`, `--rust`, ...; `dark:` follows the OS or `data-theme`). Screens use the wrappers in `src/components/ui.tsx` (Button, Dialog, Field, Page, Panel, RowMenu). Token clash to remember: AEGIS `muted` is the grey TEXT colour, so any `bg-muted` in a newly added shadcn/SmoothUI component must become `bg-raised`.
- **infra/**: Terraform (AWS provider ~> 6.0) and one CloudFormation template.

## Invariants (do not break)

- **Cognito authenticates; PostgreSQL authorizes.** `users.role` (`user` | `admin`, CHECK-constrained) is re-read on every request (`api/src/middleware/auth.js`). The agent asks the api (`/api/auth/authorize`) with the caller's own token. Never read a role from a token claim. Public sign-up (`self_signup_enabled` in Terraform, one variable for the Cognito pool, `AEGIS_SELF_SIGNUP` and `VITE_SELF_SIGNUP`) lets a Cognito-verified identity get a plain `user` row on its first request (`registerSelfSignup` in `auth.js`, email read from Cognito); admins are only ever made by `scripts/create-admin.js`. Clients never see the operator's own account (`/api/aws-accounts/whoami` is admin-only).
- **Ownership is enforced in SQL / in one helper.** `aws_accounts` and `integration_connections` queries always filter on `owner_user_id` (`api/src/aws/accountStore.js`, `api/src/integrations/store.js`). Every `/api/runs/{id}*` endpoint goes through `_require_run_owner` in `agent/main.py`. Someone else's object is a 404, never a 403.
- **No static AWS credentials anywhere.** No IAM users, no access keys, no env-var fallbacks. The task role is the only runtime identity.
- **Terraform never runs as AEGIS.** `agent/tools/terraform.py::_terraform_env` strips every ambient credential source (env keys, profiles, container credentials, IMDS). Terraform only ever sees the run's assumed `AegisProvisioner-*` credentials. A run with no bound account fails with `PROVISIONING_ACCOUNT_UNBOUND`.
- **The ExternalId is server-generated** (`accountStore.createPending`) and sealed with `AWS_ONBOARDING_ENC_KEY`; it is never accepted from the browser.
- **Generated IAM roles carry the boundary.** Pre-write validation rejects an `aws_iam_role` without `permissions_boundary`; the value arrives as `TF_VAR_aegis_permissions_boundary`.
- **Doc lookup is Tavily search + an allow-list by domain** (`agent/utils/browser.py`): `search_terraform_docs` → `fetch_terraform_docs`. There is no URL table. Queries always pass through `web_search_client.sanitise_query`.
- **State lives in the target account.** The model never writes a `backend`/`cloud` block; the platform writes `_aegis_backend.tf` (S3 backend) before every `init`, pointing at that account's `aegis-tfstate-<account>-<suffix>` bucket (created by the onboarding stack), key `runs/<run_id>/terraform.tfstate`, S3 lockfile. No bucket means no bound account: refuse, never fall back to local state. `_aegis_backend.tf`, `tfplan` and `driftplan` never leave the platform.
- **Drift checks are read-only**: `terraform plan -refresh-only` to `driftplan`, never applied. Fixing drift is an ordinary plan of the run's own code through the normal approval gate.
- **Nothing becomes knowledge without admin approval** (research candidates, pending documents).
- **Model ids have no string defaults.** Blank means mock mode.
- **One ECS task.** Per-run STS credentials, SSE queues and run locks are per-process; `runs.db` has one writer.

## Conventions

- Errors carry stable codes (`PROVISIONING_ACCOUNT_REQUIRED`, `ONBOARDING_NOT_FOUND`, ...).
- UTC timestamps; UUID v4 ids.
- Tests: `agent/tests/test_*.py` are plain assert scripts (`python -m tests.test_x`); api modules carry `require.main === module` self-checks run by `npm run check`.
- Configuration: the root `.env` locally, the ECS task definition + the `aegis/app` secret in AWS.
