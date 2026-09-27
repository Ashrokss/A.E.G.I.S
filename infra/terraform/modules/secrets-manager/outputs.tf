output "app_secret_arn" {
  description = "The one JSON secret. Fill it with the CLI -- see README.md."
  value       = aws_secretsmanager_secret.app.arn
}

output "secret_value_from" {
  description = "ECS valueFrom reference per key, for the task definition."
  value       = { for k in var.secret_keys : k => "${aws_secretsmanager_secret.app.arn}:${k}::" }
}
