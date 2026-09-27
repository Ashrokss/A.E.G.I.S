output "state_bucket" {
  description = "Terraform state bucket (written into environments/prod/backend.hcl)."
  value       = aws_s3_bucket.state.bucket
}

output "deploy_role_arn" {
  description = "Set as the AWS_DEPLOY_ROLE_ARN secret of the GitHub `prod` environment."
  value       = aws_iam_role.deploy.arn
}
