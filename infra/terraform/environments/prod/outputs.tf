output "app_url" {
  description = "Open this."
  value       = module.stack.app_url
}

output "web_bucket" {
  description = "Sync web/dist here."
  value       = module.stack.web_bucket
}

output "distribution_id" {
  description = "Invalidate after a web deploy."
  value       = module.stack.distribution_id
}

output "ecr_repository_urls" {
  description = "Image repositories."
  value       = module.stack.ecr_repository_urls
}

output "ecs_cluster" {
  description = "ECS cluster."
  value       = module.stack.ecs_cluster
}

output "ecs_service" {
  description = "ECS service."
  value       = module.stack.ecs_service
}

output "app_secret_arn" {
  description = "Fill with infra/scripts/seed-secrets.sh."
  value       = module.stack.app_secret_arn
}

output "task_role_arn" {
  description = "Principal trusted by AegisProvisioner roles."
  value       = module.stack.task_role_arn
}

output "provisioner_template_url" {
  description = "Onboarding template URL."
  value       = module.stack.provisioner_template_url
}

output "web_build_env" {
  description = "VITE_* values for the web build."
  value       = module.stack.web_build_env
}

output "cognito" {
  description = "Cognito ids."
  value       = module.stack.cognito
}
