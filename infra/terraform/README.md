# AEGIS infrastructure

Two roots, run from inside their own directories:

| Root | State | Purpose |
| --- | --- | --- |
| `bootstrap/` | local, once | State bucket, GitHub OIDC provider, deploy role, `environments/prod/backend.hcl` |
| `environments/prod/` | S3 (`aegis/prod.tfstate`, native locking) | Everything else, through `modules/stack` |

`modules/stack` is the only place wiring lives; the other modules are single-purpose:

| Module | Creates |
| --- | --- |
| `networking` | VPC, public/app/data subnets in 2 AZs, security groups (ALB ← CloudFront prefix list only; task ← ALB only; RDS/EFS ← task only), optional NAT |
| `alb` | Internal ALB; `/agent-api/*` → agent, `/api/*/internal/*` → 404, everything else → api |
| `cloudfront` | Private S3 web bucket + OAC, VPC origin to the ALB, SPA routing function, security headers, optional WAF |
| `cognito` | User pool (invite-only, optional MFA), hosted-UI domain, public PKCE client |
| `ecs` | Cluster, task definition (api + agent, ARM64), service (exactly one task) |
| `iam` | Execution role, task role (Bedrock, Cognito admin on the pool, AssumeRole on `AegisProvisioner-*` only), backup role |
| `ecr` | `aegis/api`, `aegis/agent`; immutable tags, last 30 kept |
| `efs` | Encrypted state filesystem, `agent` access point (uid 1000), daily backup |
| `rds` | PostgreSQL 16, RDS-managed password, TLS forced |
| `s3` | Public-read `templates/` prefix holding the onboarding template (CloudFormation quick-create needs an S3 URL) |
| `secrets-manager` | The `aegis/app` JSON secret container (values via `infra/scripts/seed-secrets.sh`) |
| `cloudwatch` | Log group, SNS alarms, AWS Budget |
| `guardduty` | Optional detector + high-severity alerts |

Sizing is literal in `environments/prod/main.tf`; per-account values are in the gitignored `terraform.tfvars` (see `terraform.tfvars.example`). CI passes `-var image_tag=<git sha>`: changing the tag is the deploy.

Checks: `terraform fmt -check -recursive`, `terraform validate` in both roots, `scripts/check-module-vars.sh`.
