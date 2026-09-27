# Two secrets, not eleven. ECS valueFrom takes a JSON-key selector
# (arn:...:secret:NAME-abc123:KEY::), so one JSON document feeds many env vars
# and you pay for one secret.
#
# Terraform creates the CONTAINER and never the contents. The placeholder
# version exists only so the valueFrom references resolve on first apply;
# ignore_changes means Terraform never plans to overwrite a human-set value,
# and no secret ever enters terraform.tfstate.
#
# Fill it with infra/scripts/seed-secrets.sh, which merges (never replaces)
# and generates the two sealing keys only when absent.
#
# Keys the task definition reads (every key must EXIST in the JSON, even if
# empty, or the task fails to start):
#   AWS_ONBOARDING_ENC_KEY   seals stored ExternalIds          (api, required)
#   INTEGRATIONS_ENC_KEY     seals GitHub/Jira tokens          (api, required)
#   TAVILY_API_KEY           Terraform doc search + KB research (agent, optional)
#   JIRA_API_TOKEN           operator Jira intake poller        (agent, optional)
#   JIRA_WEBHOOK_SECRET      Jira webhook HMAC; empty = off     (agent, optional)

resource "aws_secretsmanager_secret" "app" {
  name                    = "${var.name}/app"
  description             = "Application secrets, one JSON document. Values set out of band."
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "app_placeholder" {
  secret_id     = aws_secretsmanager_secret.app.id
  secret_string = jsonencode({ PLACEHOLDER = "set-me-with-the-cli-see-README" })

  lifecycle {
    ignore_changes = [secret_string]
  }
}
