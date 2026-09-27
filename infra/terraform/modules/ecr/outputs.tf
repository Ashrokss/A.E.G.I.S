output "repository_urls" {
  description = "Push targets, keyed by container name. The ECS task definition reads these."
  value       = { for k, r in aws_ecr_repository.app : k => r.repository_url }
}
