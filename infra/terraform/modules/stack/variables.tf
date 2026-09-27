# Sizing and feature switches. environments/<env>/main.tf passes literals; the
# gitignored terraform.tfvars carries only what differs per ACCOUNT.

variable "project" {
  description = "Name prefix for every resource."
  type        = string
}

variable "aws_region" {
  description = "Region."
  type        = string
}

variable "account_id" {
  description = "Target account (the provider is pinned to it)."
  type        = string
}

variable "image_tag" {
  description = "Image tag to deploy (git SHA)."
  type        = string
}

variable "provisioner_template_path" {
  description = "Path of infra/onboarding/aegis-provisioner-role.yaml."
  type        = string
}

# ── Network ──
variable "vpc_cidr" {
  description = "VPC CIDR."
  type        = string
}

variable "public_subnet_cidrs" {
  description = "Two public subnets (NAT, if enabled)."
  type        = list(string)
}

variable "app_subnet_cidrs" {
  description = "Two app subnets (task, internal ALB, EFS)."
  type        = list(string)
}

variable "data_subnet_cidrs" {
  description = "Two isolated data subnets (RDS)."
  type        = list(string)
}

variable "enable_nat_gateway" {
  description = "NAT for private egress (about 33 USD/mo). Off = task gets a public IP for egress only."
  type        = bool
}

# ── Compute ──
variable "task_cpu" {
  description = "Task CPU units."
  type        = number
}

variable "task_memory" {
  description = "Task memory (MiB)."
  type        = number
}

# ── Data ──
variable "db_instance_class" {
  description = "RDS instance class."
  type        = string
}

variable "db_multi_az" {
  description = "Multi-AZ RDS."
  type        = bool
}

variable "db_name" {
  description = "Database name."
  type        = string
}

variable "db_username" {
  description = "Database master username."
  type        = string
}

variable "db_backup_retention_days" {
  description = "RDS automated backup retention."
  type        = number
}

variable "db_deletion_protection" {
  description = "RDS deletion protection."
  type        = bool
}

# ── Edge / identity ──
variable "cloudfront_price_class" {
  description = "CloudFront price class."
  type        = string
}

variable "enable_waf" {
  description = "Attach WAF to CloudFront."
  type        = bool
}

variable "cognito_extra_callback_urls" {
  description = "Extra Cognito callback URLs (local dev)."
  type        = list(string)
}

variable "cognito_extra_logout_urls" {
  description = "Extra Cognito logout URLs (local dev)."
  type        = list(string)
}

# ── Observability ──
variable "log_retention_days" {
  description = "CloudWatch log retention."
  type        = number
}

variable "alarm_email" {
  description = "Alarm + budget notification address (blank = no subscription)."
  type        = string
}

variable "monthly_budget_usd" {
  description = "Monthly AWS Budget in USD."
  type        = number
}

variable "efs_storage_alarm_bytes" {
  description = "EFS size alarm threshold."
  type        = number
}

variable "enable_guardduty" {
  description = "GuardDuty detector (account-wide, about 4 USD/mo on a small account)."
  type        = bool
}

# ── Models: NO defaults. A blank id degrades to mock mode; a stale one fails every call. ──
variable "bedrock_model" {
  description = "Default model id."
  type        = string
}

variable "bedrock_model_chat" {
  description = "Chat model id."
  type        = string
}

variable "bedrock_model_codegen" {
  description = "Codegen model id."
  type        = string
}

variable "bedrock_model_heavy" {
  description = "Repair model id."
  type        = string
}

variable "bedrock_embed_model" {
  description = "Embedding model id."
  type        = string
}

variable "bedrock_embed_dimensions" {
  description = "Embedding dimensions."
  type        = number
}

variable "model_max_output_tokens" {
  description = "Max output tokens per call."
  type        = number
}
