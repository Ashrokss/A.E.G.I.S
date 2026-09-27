output "app_url" {
  description = "Open this."
  value       = module.cloudfront.app_url
}

output "web_bucket" {
  description = "Sync web/dist here."
  value       = module.cloudfront.web_bucket
}

output "distribution_id" {
  description = "Invalidate after a web deploy."
  value       = module.cloudfront.distribution_id
}

output "ecr_repository_urls" {
  description = "Push images here, tagged with the git SHA."
  value       = module.ecr.repository_urls
}

output "ecs_cluster" {
  description = "For ECS Exec and wait services-stable."
  value       = module.ecs.cluster_name
}

output "ecs_service" {
  description = "For ECS Exec and wait services-stable."
  value       = module.ecs.service_name
}

output "app_secret_arn" {
  description = "Fill with infra/scripts/seed-secrets.sh."
  value       = module.secrets_manager.app_secret_arn
}

output "task_role_arn" {
  description = "The principal every AegisProvisioner role trusts."
  value       = module.iam.task_role_arn
}

output "provisioner_template_url" {
  description = "CloudFormation template for onboarding a target account."
  value       = module.s3.provisioner_template_url
}

# The web build needs these at build time (Vite inlines VITE_*).
output "web_build_env" {
  description = "VITE_* values for `npm run build` in web/."
  value = {
    VITE_COGNITO_DOMAIN    = module.cognito.hosted_ui_url
    VITE_COGNITO_CLIENT_ID = module.cognito.client_id
  }
}

output "cognito" {
  description = "Cognito ids, for a local .env."
  value = {
    user_pool_id = module.cognito.user_pool_id
    client_id    = module.cognito.client_id
    domain_url   = module.cognito.hosted_ui_url
    issuer_url   = module.cognito.issuer_url
    jwks_url     = module.cognito.jwks_url
  }
}
