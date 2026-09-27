variable "name" {
  description = "Name prefix."
  type        = string
}

variable "aws_region" {
  description = "Region of the pool."
  type        = string
}

variable "account_id" {
  description = "Account id, to make the hosted UI domain prefix unique."
  type        = string
}

variable "app_url" {
  description = "Public app URL (CloudFront). Callback = <app_url>/auth/callback."
  type        = string
}

variable "extra_callback_urls" {
  description = "Additional callbacks, e.g. the local dev server."
  type        = list(string)
  default     = ["http://localhost:5173/auth/callback"]
}

variable "extra_logout_urls" {
  description = "Additional logout URLs, e.g. the local dev server."
  type        = list(string)
  default     = ["http://localhost:5173"]
}
