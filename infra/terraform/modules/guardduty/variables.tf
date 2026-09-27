variable "name" {
  description = "Name prefix."
  type        = string
}

variable "enabled" {
  description = <<-EOT
    Create the detector. ONE per AWS account, not one per environment -- a
    second apply in the same account fails with BadRequestException.
  EOT
  type        = bool
  default     = false
}

variable "finding_publishing_frequency" {
  description = "How often findings are exported. FIFTEEN_MINUTES, ONE_HOUR or SIX_HOURS."
  type        = string
  default     = "SIX_HOURS"

  validation {
    condition     = contains(["FIFTEEN_MINUTES", "ONE_HOUR", "SIX_HOURS"], var.finding_publishing_frequency)
    error_message = "Must be FIFTEEN_MINUTES, ONE_HOUR or SIX_HOURS."
  }
}

variable "min_severity" {
  description = "Findings at or above this severity are forwarded to the alarm topic. 7.0 is GuardDuty's HIGH band."
  type        = number
  default     = 7.0
}

variable "alarm_topic_arn" {
  description = <<-EOT
    SNS topic from modules/cloudwatch that findings are forwarded to.

    Required, with no blank fallback: the ARN is unknown until apply, so it
    cannot gate whether the rule is created.
  EOT
  type        = string
}
