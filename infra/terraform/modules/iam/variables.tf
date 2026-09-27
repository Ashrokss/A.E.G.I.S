variable "name" {
  description = "Name prefix. Role names derive from it (stack/main.tf pre-computes two)."
  type        = string
}

variable "aws_region" {
  description = "Region."
  type        = string
}

variable "account_id" {
  description = "This account."
  type        = string
}

variable "app_secret_arn" {
  description = "The aegis/app JSON secret."
  type        = string
}

variable "db_master_secret_arn" {
  description = "RDS-managed master password secret."
  type        = string
}

variable "efs_arn" {
  description = "State filesystem."
  type        = string
}

variable "efs_access_point_arns" {
  description = "Access points the task may mount."
  type        = list(string)
}

variable "user_pool_arn" {
  description = "The one Cognito pool."
  type        = string
}
