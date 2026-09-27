variable "name" {
  description = "Name prefix."
  type        = string
}

variable "account_id" {
  description = "Account id, for a globally unique web bucket name."
  type        = string
}

variable "alb_arn" {
  description = "Internal ALB ARN (VPC origin endpoint)."
  type        = string
}

variable "alb_dns_name" {
  description = "Internal ALB DNS name."
  type        = string
}

variable "cloudfront_price_class" {
  description = "PriceClass_100 = North America + Europe edges (cheapest)."
  type        = string
  default     = "PriceClass_100"
}

variable "enable_waf" {
  description = "Attach a WAFv2 web ACL (about 9 USD/mo plus requests). Off for a personal deployment."
  type        = bool
  default     = false
}

variable "waf_rate_limit" {
  description = "Requests per 5 minutes per IP before WAF blocks."
  type        = number
  default     = 2000
}
