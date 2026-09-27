output "vpc_id" {
  description = "VPC id."
  value       = aws_vpc.main.id
}

output "public_subnet_ids" {
  description = "Public subnets, for the ALB."
  value       = aws_subnet.public[*].id
}

output "app_subnet_ids" {
  description = "Private app subnets, for the Fargate task and EFS mount targets."
  value       = aws_subnet.app[*].id
}

output "data_subnet_ids" {
  description = "Private data subnets, for the RDS subnet group."
  value       = aws_subnet.data[*].id
}

output "alb_security_group_id" {
  description = "The only group reachable from the internet."
  value       = aws_security_group.alb.id
}

output "app_security_group_id" {
  description = "Fargate task security group."
  value       = aws_security_group.app.id
}

output "db_security_group_id" {
  description = "RDS security group. Ingress from the app group only."
  value       = aws_security_group.db.id
}

output "efs_security_group_id" {
  description = "EFS mount target security group."
  value       = aws_security_group.efs.id
}

output "tasks_need_public_ip" {
  description = <<-EOT
    True when there is no NAT gateway: the task needs a public IP to reach the
    internet gateway. The compute module passes this to assign_public_ip.
  EOT
  value       = !var.enable_nat_gateway
}

output "private_route_table_ids" {
  description = "App and data route tables -- what the S3 gateway endpoint attaches to."
  value       = [aws_route_table.app.id, aws_route_table.data.id]
}
