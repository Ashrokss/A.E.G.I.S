output "arn" {
  description = "ALB ARN — the CloudFront VPC origin endpoint."
  value       = aws_lb.main.arn
}

output "dns_name" {
  description = "Internal DNS name (resolvable only inside the VPC)."
  value       = aws_lb.main.dns_name
}

output "arn_suffix" {
  description = "For CloudWatch dimensions."
  value       = aws_lb.main.arn_suffix
}

output "listener_arn" {
  description = "HTTP listener; the ECS service depends on it."
  value       = aws_lb_listener.http.arn
}

output "target_group_arns" {
  description = "Target group ARNs by name (api, agent)."
  value       = { for k, tg in aws_lb_target_group.app : k => tg.arn }
}

output "target_group_arn_suffixes" {
  description = "Target group ARN suffixes by name, for CloudWatch."
  value       = { for k, tg in aws_lb_target_group.app : k => tg.arn_suffix }
}
