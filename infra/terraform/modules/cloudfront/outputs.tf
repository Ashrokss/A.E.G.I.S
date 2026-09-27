output "app_url" {
  description = "Public URL (the CloudFront *.cloudfront.net name, free HTTPS)."
  value       = "https://${aws_cloudfront_distribution.main.domain_name}"
}

output "distribution_id" {
  description = "For cache invalidation after a web deploy."
  value       = aws_cloudfront_distribution.main.id
}

output "distribution_arn" {
  description = "For the deploy role's invalidation permission."
  value       = aws_cloudfront_distribution.main.arn
}

output "web_bucket" {
  description = "S3 bucket the SPA build is synced to."
  value       = aws_s3_bucket.web.bucket
}

output "web_bucket_arn" {
  description = "Web bucket ARN."
  value       = aws_s3_bucket.web.arn
}
