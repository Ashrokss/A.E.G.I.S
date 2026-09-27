variable "name" {
  description = "Cluster, service and task family name."
  type        = string
}

variable "aws_region" {
  description = "Region."
  type        = string
}

variable "repository_urls" {
  description = "ECR repository URL by image (api, agent)."
  type        = map(string)
}

variable "image_tag" {
  description = "Image tag to run (the git SHA). Changing it IS the deploy."
  type        = string
}

variable "task_cpu" {
  description = "Task CPU units."
  type        = number
}

variable "task_memory" {
  description = "Task memory (MiB)."
  type        = number
}

variable "assign_public_ip" {
  description = "True when there is no NAT gateway (egress via the IGW)."
  type        = bool
}

variable "subnet_ids" {
  description = "Subnets for the task."
  type        = list(string)
}

variable "app_security_group_id" {
  description = "Task security group."
  type        = string
}

variable "target_group_arns" {
  description = "ALB target groups by name (api, agent)."
  type        = map(string)
}

variable "listener_arn" {
  description = "ALB listener the service must wait for."
  type        = string
}

variable "execution_role_arn" {
  description = "ECS execution role."
  type        = string
}

variable "task_role_arn" {
  description = "ECS task role (also AEGIS_TASK_ROLE_ARN for the onboarding link)."
  type        = string
}

variable "efs_id" {
  description = "State filesystem."
  type        = string
}

variable "efs_access_points" {
  description = "Access points from modules/efs (needs agent)."
  type        = map(object({ id = string, arn = string }))
}

variable "log_group_name" {
  description = "CloudWatch log group."
  type        = string
}

variable "db_endpoint" {
  description = "RDS hostname."
  type        = string
}

variable "db_name" {
  description = "Database name."
  type        = string
}

variable "db_username" {
  description = "Database master username."
  type        = string
}

variable "db_password_value_from" {
  description = "ECS valueFrom for the RDS-managed password."
  type        = string
}

variable "db_master_secret_arn" {
  description = "RDS-managed password secret ARN (rotation-safe reads)."
  type        = string
}

variable "secret_value_from" {
  description = "ECS valueFrom per key of the aegis/app secret."
  type        = map(string)
}

variable "cognito_user_pool_id" {
  description = "Cognito pool id."
  type        = string
}

variable "cognito_client_id" {
  description = "Cognito web client id."
  type        = string
}

variable "cognito_domain_url" {
  description = "Hosted UI ABSOLUTE URL."
  type        = string
}

variable "cognito_issuer_url" {
  description = "Token issuer URL."
  type        = string
}

variable "cognito_jwks_url" {
  description = "JWKS URL."
  type        = string
}

variable "onboarding_template_url" {
  description = "S3 URL of the provisioner-role template."
  type        = string
}

variable "bedrock_model" {
  description = "Default Bedrock model id (blank = mock mode)."
  type        = string
}

variable "bedrock_model_chat" {
  description = "Chat / clarification model id."
  type        = string
}

variable "bedrock_model_codegen" {
  description = "Terraform generation model id."
  type        = string
}

variable "bedrock_model_heavy" {
  description = "Repair / escalation model id."
  type        = string
}

variable "bedrock_embed_model" {
  description = "Knowledge-base embedding model id."
  type        = string
}

variable "bedrock_embed_dimensions" {
  description = "Embedding dimensions."
  type        = number
}

variable "model_max_output_tokens" {
  description = "Max output tokens per model call."
  type        = number
}

variable "self_signup_enabled" {
  description = "Public sign-up: anyone can create an AEGIS login (verified email), becoming a plain user who connects their own AWS accounts. False = invitation only."
  type        = bool
  default     = false
}
