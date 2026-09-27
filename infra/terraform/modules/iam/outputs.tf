output "execution_role_arn" {
  description = "ECS task execution role."
  value       = aws_iam_role.execution.arn
}

output "task_role_arn" {
  description = "The runtime identity. The provisioner template trusts exactly this ARN."
  value       = aws_iam_role.task.arn
}

output "backup_role_arn" {
  description = "AWS Backup service role."
  value       = aws_iam_role.backup.arn
}
