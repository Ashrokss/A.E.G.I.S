variable "aws_region" {
  description = "Region for the pool. Keep it the same as AWS_REGION in .env."
  type        = string
  default     = "us-east-1"
}

variable "aws_profile" {
  description = "AWS CLI profile to create the pool with (null = the default credential chain)."
  type        = string
  default     = null
}

variable "self_signup_enabled" {
  description = "Let the sign-in page's \"Connect your AWS account\" create logins, as in prod."
  type        = bool
  default     = true
}
