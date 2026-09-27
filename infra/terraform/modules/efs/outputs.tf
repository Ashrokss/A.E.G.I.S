output "id" {
  description = "File system id, for the ECS volume definition."
  value       = aws_efs_file_system.state.id
}

output "arn" {
  description = "File system ARN, for the task role's mount policy."
  value       = aws_efs_file_system.state.arn
}

output "access_points" {
  description = "One access point per writer, keyed by purpose."
  value       = aws_efs_access_point.state
}
