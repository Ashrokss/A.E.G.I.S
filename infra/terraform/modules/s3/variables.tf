variable "name" {
  description = "Name prefix."
  type        = string
}

variable "account_id" {
  description = "Account id, for a globally unique bucket name."
  type        = string
}

variable "provisioner_template_path" {
  description = "Local path of infra/onboarding/aegis-provisioner-role.yaml."
  type        = string
}
