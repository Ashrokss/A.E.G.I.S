variable "name" {
  description = "Name prefix. The secret is <name>/app."
  type        = string
}

variable "secret_keys" {
  description = <<-EOT
    Keys inside the single JSON app secret. Terraform creates the container and
    never the values; these names build the ECS valueFrom references.
  EOT
  type        = list(string)
  default = [
    "AWS_ONBOARDING_ENC_KEY",
    "INTEGRATIONS_ENC_KEY",
    "TAVILY_API_KEY",
    "JIRA_API_TOKEN",
    "JIRA_WEBHOOK_SECRET",
  ]
}
