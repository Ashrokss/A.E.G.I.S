# Deploying AEGIS

This is the runbook for putting AEGIS into your own AWS account (us-east-1) and keeping it running. The first deploy takes about an hour, most of it waiting for CloudFront and RDS.

## What you end up with

| Piece | Where |
| --- | --- |
| Web app | `https://<id>.cloudfront.net` (the S3 SPA plus api/agent behind CloudFront) |
| Compute | One ECS Fargate task (ARM64, 1 vCPU / 3 GB) running `api` and `agent` |
| Data | RDS PostgreSQL 16 `db.t4g.micro` (users, accounts, connections), EFS (runs.db, workspaces) |
| Identity | Cognito user pool (invite-only), GitHub OIDC deploy role |
| Secrets | `aegis/app` in Secrets Manager, and the RDS-managed master password |
| Guardrails | CloudWatch alarms and an AWS Budget (80 USD) emailed to you |

Rough cost: **about 70 USD/month plus Bedrock usage** (see `infra/terraform/environments/prod/main.tf`).

## 0. Prerequisites

- AWS CLI v2, signed in to your account with an administrator role (SSO is fine).
- Terraform ≥ 1.11, Node 20, Docker.
- **Bedrock model access** enabled in us-east-1 for the models you plan to use (Bedrock console → Model access): a Claude Haiku-class model for chat, a Claude Sonnet-class model for code generation, and Titan Text Embeddings v2.
- A GitHub repository for this code (for example `you/aegis`).
- Optional: a [Tavily](https://tavily.com) API key. Terraform doc search works without it, falling back to direct registry pages.

## 1. Bootstrap the account (once, from your laptop)

```bash
cd infra/terraform/bootstrap
terraform init
terraform apply -var github_repository=you/aegis
```

This creates the state bucket, the GitHub OIDC provider and the `aegis-github-deploy` role, and writes `environments/prod/backend.hcl`. Keep the local `terraform.tfstate` it produced somewhere safe.

## 2. Configure GitHub

In the repository, go to **Settings → Environments → New environment `prod`** and add yourself as a **required reviewer**. The deploy role trusts only jobs running in this environment.

| Kind | Name | Value |
| --- | --- | --- |
| Environment secret | `AWS_DEPLOY_ROLE_ARN` | `terraform output deploy_role_arn` (bootstrap) |
| Environment variable | `TF_STATE_BUCKET` | `terraform output state_bucket` (bootstrap) |
| Environment variable | `AWS_ACCOUNT_ID` | your 12-digit account id |
| Environment variable | `ALARM_EMAIL` | where alarms and budget alerts go |
| Environment variable | `BEDROCK_MODEL_CHAT` | e.g. `us.anthropic.claude-haiku-4-5-20251001-v1:0` |
| Environment variable | `BEDROCK_MODEL_CODEGEN` | a Sonnet-class inference profile id |
| Environment variable | `BEDROCK_EMBED_MODEL` | `amazon.titan-embed-text-v2:0` |

## 3. First deploy

Push to `main`, or run the **deploy** workflow manually, and approve it. The workflow:

1. assumes the deploy role via OIDC (no stored keys),
2. creates the ECR repositories if needed,
3. builds `api` and `agent` for linux/arm64 and pushes them tagged with the git SHA (tags are immutable),
4. runs `terraform plan`, **refusing any delete or replace other than the ECS task definition**,
5. applies, builds the SPA with the Cognito values from the outputs, and syncs it to S3,
6. waits for ECS to be stable and smoke-tests the URL.

The very first run takes about 25 minutes (CloudFront + RDS). The ECS service may report unhealthy until step 4 below has seeded the secret; re-run the workflow after seeding.

## 4. Seed the secret

```bash
cd infra/terraform/environments/prod
terraform init -backend-config=backend.hcl        # your laptop, same state as CI
cd ../../../..
TAVILY_API_KEY=tvly-... ./infra/scripts/seed-secrets.sh
aws ecs update-service --cluster aegis --service aegis --force-new-deployment
```

The script generates the two sealing keys only if they are absent. **Never regenerate them by hand**: every stored ExternalId and integration token would become unreadable.

## 5. Create the first administrator

```bash
TASK=$(aws ecs list-tasks --cluster aegis --query 'taskArns[0]' --output text)
aws ecs execute-command --cluster aegis --task "$TASK" --container api --interactive \
  --command "node scripts/create-admin.js you@example.com"
```

It prints a temporary password once. Sign in at the app URL, set a new password, and invite everyone else from **Admin**.

## 6. Connect your AWS account

In the app, go to **Accounts → Connect an account**. AEGIS generates an ExternalId and a CloudFormation quick-create link for `infra/onboarding/aegis-provisioner-role.yaml`:

- `AegisProvisioner-<suffix>`: PowerUserAccess, plus IAM limited to roles that carry the boundary. It trusts only the AEGIS task role, and only with that ExternalId.
- `AegisBoundary-<suffix>`: the permissions boundary every role AEGIS creates must carry.

Launch the stack in the target account, paste the `RoleArn` output back, then **Verify and connect**. You can connect the account AEGIS itself runs in the same way: runs still go through the role, never through AEGIS's own identity.

## Local development against real AWS

Deploy the same template once more with `RoleSuffix=local`, and set `TrustedRoleArn` to **your own** IAM principal. Then set `AEGIS_TASK_ROLE_ARN` in `.env` to that principal. The api assumes the role with your profile; Terraform still never sees your profile.

## Day-2 operations

| Task | How |
| --- | --- |
| Deploy | Merge to `main` and approve the `prod` job. |
| Roll back | Re-run the deploy workflow on an older commit (images for every SHA stay in ECR; the last 30 are kept). |
| Rotate a sealing key | Not supported in place; add a migration that re-seals first. |
| DB password rotation | Automatic (RDS-managed). The api reads the current password from the secret for each new connection. |
| Inspect runs.db | `aws ecs execute-command ... --container agent --command "sqlite3 /mnt/agent/runs.db"` |
| Prune old run workspaces | ECS Exec into `agent`, delete `/mnt/agent/runs/<old-id>`; the EFS size alarm fires at 20 GB. |
| Logs | CloudWatch log group `/ecs/aegis` (streams `api/...`, `agent/...`). |
| Turn on Jira intake | Put `JIRA_API_TOKEN` in the secret and add `JIRA_POLL_SECONDS`, `JIRA_BASE_URL`, `JIRA_EMAIL` to the agent environment in `modules/ecs`. |

## Upgrade paths (not built, on purpose)

- **Custom domain:** ACM certificate in us-east-1 + aliases on the distribution + a Route 53 record; add the domain to the Cognito callback list.
- **Private egress:** `enable_nat_gateway = true` (about 33 USD/mo). The task then runs without a public IP.
- **High availability:** `db_multi_az = true`. The task stays at one: move `runs.db` to PostgreSQL and SSE to a shared bus first.
- **WAF:** `enable_waf = true` on the CloudFront module (about 9 USD/mo).
- **Public sign-up (SaaS):** set `allow_admin_create_user_only = false`, add a sign-up flow that creates the `users` row, and add organisations as an owner level above users.
