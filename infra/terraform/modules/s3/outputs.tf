output "provisioner_template_url" {
  description = "AWS_ONBOARDING_TEMPLATE_URL — the quick-create templateURL."
  value       = "https://${aws_s3_bucket.templates.bucket_regional_domain_name}/${aws_s3_object.provisioner_template.key}"
}
