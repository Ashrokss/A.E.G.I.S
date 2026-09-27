variable "name" {
  description = "Name prefix, also the custom metric namespace."
  type        = string
}

variable "log_retention_days" {
  description = "CloudWatch retention for the one application log group."
  type        = number
  default     = 30
}

variable "alarm_email" {
  description = "Subscribed to the alarm topic. Blank creates the topic with no subscription."
  type        = string
  default     = ""
}

variable "efs_id" {
  description = "For the StorageBytes growth alarm."
  type        = string
}

variable "alb_arn_suffix" {
  description = "For the HealthyHostCount alarm dimension."
  type        = string
}

variable "target_group_arn_suffixes" {
  description = "Target group ARN suffixes by name (api, agent) — one no-healthy-targets alarm each."
  type        = map(string)
}

variable "monthly_budget_usd" {
  description = "Monthly AWS Budget in USD (forecast + actual alerts to alarm_email). 0 disables."
  type        = number
  default     = 80
}

variable "db_identifier" {
  description = "For the RDS alarm dimensions."
  type        = string
}

variable "efs_storage_alarm_bytes" {
  description = "Un-pruned Terraform workspaces are the usual cause. See the note in main.tf before deleting any."
  type        = number
  default     = 53687091200
}
