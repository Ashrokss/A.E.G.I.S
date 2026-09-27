output "cluster_name" {
  description = "ECS cluster."
  value       = aws_ecs_cluster.main.name
}

output "service_name" {
  description = "ECS service."
  value       = aws_ecs_service.app.name
}
