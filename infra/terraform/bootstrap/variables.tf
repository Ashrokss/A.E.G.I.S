variable "project" {
  description = "Name prefix (must match environments/prod)."
  type        = string
  default     = "aegis"
}

variable "aws_region" {
  description = "Region."
  type        = string
  default     = "us-east-1"
}

variable "github_repository" {
  description = "owner/repo of the AEGIS repository (the only repo CI may deploy from)."
  type        = string
}
