output "log_group_name" {
  description = "One group, four stream prefixes."
  value       = aws_cloudwatch_log_group.app.name
}

output "log_group_arn" {
  description = "For the task role policy."
  value       = aws_cloudwatch_log_group.app.arn
}

output "alarm_topic_arn" {
  description = "SNS topic every alarm publishes to."
  value       = aws_sns_topic.alarms.arn
}
