variable "name" {
  description = "Name prefix."
  type        = string
}

variable "data_subnet_ids" {
  description = "Private data subnets. Two, in different AZs, for Multi-AZ."
  type        = list(string)
}

variable "db_security_group_id" {
  description = "Ingress from the app security group only."
  type        = string
}

variable "instance_class" {
  description = "RDS instance class."
  type        = string
  default     = "db.t4g.medium"
}

variable "engine_version" {
  description = "PostgreSQL major version."
  type        = string
  default     = "16"
}

variable "multi_az" {
  description = "Multi-AZ. Losing this database locks every user out of the product."
  type        = bool
  default     = true
}

variable "db_name" {
  description = "Database name. Must match POSTGRES_DB in the app config."
  type        = string
  default     = "aegis"
}

variable "username" {
  description = "Master username. The password is generated and rotated by RDS."
  type        = string
  default     = "aegis"
}

variable "allocated_storage" {
  description = "Initial storage (GB)."
  type        = number
  default     = 20
}

variable "max_allocated_storage" {
  description = "Autoscaling ceiling (GB)."
  type        = number
  default     = 100
}

variable "backup_retention_days" {
  description = "Automated backup retention."
  type        = number
  default     = 7
}

variable "deletion_protection" {
  description = "Guards the authorization store. Turn off only to decommission deliberately."
  type        = bool
  default     = true
}
