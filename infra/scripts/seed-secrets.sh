#!/usr/bin/env bash
# Fill the aegis/app secret that Terraform creates empty.
#
#   TAVILY_API_KEY=tvly-... ./infra/scripts/seed-secrets.sh
#
# MERGES, never replaces: keys already set are kept. The two sealing keys are
# generated only when absent -- regenerating one would make every stored
# ExternalId / integration token unreadable. Every key the task definition
# reads is guaranteed to EXIST afterwards (possibly empty), because ECS refuses
# to start a task whose secret references a missing JSON key.
set -euo pipefail

ENV_DIR="$(cd "$(dirname "$0")/../terraform/environments/${ENV:-prod}" && pwd)"
SECRET_ARN="$(terraform -chdir="$ENV_DIR" output -raw app_secret_arn)"
REGION="${AWS_REGION:-us-east-1}"

current="$(aws secretsmanager get-secret-value --region "$REGION" --secret-id "$SECRET_ARN" \
  --query SecretString --output text 2>/dev/null || echo '{}')"

umask 077
tmp="$(mktemp)"
trap 'shred -u "$tmp" 2>/dev/null || unlink "$tmp"' EXIT

CURRENT="$current" python3 - "$tmp" <<'PY'
import json, os, secrets, sys
cur = json.loads(os.environ.get("CURRENT") or "{}")
cur.pop("PLACEHOLDER", None)
for key in ("AWS_ONBOARDING_ENC_KEY", "INTEGRATIONS_ENC_KEY"):
    if not cur.get(key):
        cur[key] = secrets.token_hex(32)
        print(f"generated {key}")
for key in ("TAVILY_API_KEY", "JIRA_API_TOKEN", "JIRA_WEBHOOK_SECRET"):
    if os.environ.get(key):
        cur[key] = os.environ[key]
        print(f"set {key} from environment")
    cur.setdefault(key, "")
open(sys.argv[1], "w").write(json.dumps(cur))
print("keys:", ", ".join(sorted(cur)))
PY

aws secretsmanager put-secret-value --region "$REGION" --secret-id "$SECRET_ARN" \
  --secret-string "file://$tmp" >/dev/null
echo "aegis/app updated. Redeploy (or force a new ECS deployment) for tasks to pick it up."
