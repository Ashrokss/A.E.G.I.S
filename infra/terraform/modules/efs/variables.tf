variable "name" {
  description = "Name prefix."
  type        = string
}


variable "app_subnet_ids" {
  description = "Subnets for the EFS mount targets. Same subnets as the task."
  type        = list(string)
}

variable "efs_security_group_id" {
  description = "Security group for the mount targets."
  type        = string
}

variable "task_role_arn" {
  description = "Principal allowed to mount the file system."
  type        = string
}

variable "backup_role_arn" {
  description = "Role AWS Backup assumes to snapshot the file system."
  type        = string
}

variable "access_points" {
  description = <<-EOT
    EFS access point per writer, so a bug in one service cannot reach another
    directory. Keys become volume names in the task definition.
  EOT
  type        = map(string)
  default = {
    agent = "/agent"
  }
}
