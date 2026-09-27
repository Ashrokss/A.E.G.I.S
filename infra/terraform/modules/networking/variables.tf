variable "name" {
  description = "Name prefix for every resource in this module."
  type        = string
}

variable "vpc_cidr" {
  description = "CIDR for the VPC."
  type        = string
}

variable "availability_zones" {
  description = "Exactly two AZs. The deck specifies 2, and the single-task design gains nothing from more."
  type        = list(string)

  validation {
    condition     = length(var.availability_zones) == 2
    error_message = "Provide exactly two availability zones."
  }
}

variable "public_subnet_cidrs" {
  description = "ALB and the one NAT gateway, one CIDR per AZ."
  type        = list(string)
}

variable "app_subnet_cidrs" {
  description = "Fargate tasks, one CIDR per AZ."
  type        = list(string)
}

variable "data_subnet_cidrs" {
  description = "RDS, one CIDR per AZ."
  type        = list(string)
}


variable "enable_nat_gateway" {
  description = <<-EOT
    A NAT gateway for private egress (~$33/month plus per-GB). With it off the
    app subnets route through the internet gateway and the task takes a public
    IP; sg-app still permits ingress from the load balancer only.
  EOT
  type        = bool
  default     = true
}

