variable "name" {
  description = "Name prefix."
  type        = string
}

variable "ecr_repositories" {
  description = "One repository per container image."
  type        = list(string)
  default     = ["api", "agent"]
}
