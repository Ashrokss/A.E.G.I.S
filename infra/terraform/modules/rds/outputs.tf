output "endpoint" {
  description = "Hostname for POSTGRES_HOST."
  value       = aws_db_instance.main.address
}

output "port" {
  description = "Port for POSTGRES_PORT."
  value       = aws_db_instance.main.port
}

output "identifier" {
  description = "Instance identifier, for CloudWatch alarm dimensions."
  value       = aws_db_instance.main.identifier
}

output "master_secret_arn" {
  description = "RDS-managed password secret. Terraform never sees the value."
  value       = aws_db_instance.main.master_user_secret[0].secret_arn
}

output "password_value_from" {
  description = "ECS secrets valueFrom reference for POSTGRES_PASSWORD."
  value       = "${aws_db_instance.main.master_user_secret[0].secret_arn}:password::"
}
