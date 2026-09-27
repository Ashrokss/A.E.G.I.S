output "user_pool_id" {
  description = "AWS_COGNITO_USER_POOL_ID."
  value       = aws_cognito_user_pool.main.id
}

output "user_pool_arn" {
  description = "For the task role Cognito admin policy."
  value       = aws_cognito_user_pool.main.arn
}

output "client_id" {
  description = "AWS_COGNITO_CLIENT_ID (public PKCE client)."
  value       = aws_cognito_user_pool_client.web.id
}

output "hosted_ui_url" {
  description = "AWS_COGNITO_DOMAIN: ABSOLUTE URL of the hosted UI (both api and web need the full URL)."
  value       = "https://${aws_cognito_user_pool_domain.main.domain}.auth.${var.aws_region}.amazoncognito.com"
}

output "issuer_url" {
  description = "AWS_COGNITO_ISSUER_URL."
  value       = local.issuer_url
}

output "jwks_url" {
  description = "AWS_COGNITO_JWKS_URL."
  value       = "${local.issuer_url}/.well-known/jwks.json"
}
