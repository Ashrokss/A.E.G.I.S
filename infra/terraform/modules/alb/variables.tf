variable "name" {
  description = "Name prefix."
  type        = string
}

variable "vpc_id" {
  description = "VPC for the target groups."
  type        = string
}

variable "subnet_ids" {
  description = "Private subnets for the internal ALB (two AZs)."
  type        = list(string)
}

variable "alb_security_group_id" {
  description = "ALB security group (CloudFront prefix list in, app ports out)."
  type        = string
}
