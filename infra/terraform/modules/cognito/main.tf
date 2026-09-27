# Identity only. Cognito authenticates; PostgreSQL (users.role) authorizes —
# no groups are created or read.

resource "aws_cognito_user_pool" "main" {
  name                     = "${var.name}-users"
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  deletion_protection      = "ACTIVE"
  mfa_configuration        = "OPTIONAL"

  software_token_mfa_configuration {
    enabled = true
  }

  # Invite-only: accounts are created from the Admin page (Cognito + users row
  # together). Flip to false, and add a sign-up flow, when AEGIS goes public.
  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = true
    temporary_password_validity_days = 3
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }
}

# Hosted UI prefix must be globally unique; the account id makes it so.
resource "aws_cognito_user_pool_domain" "main" {
  domain       = "${var.name}-${var.account_id}"
  user_pool_id = aws_cognito_user_pool.main.id
}

# Public client: authorization code + PKCE, no secret (a browser cannot keep one).
resource "aws_cognito_user_pool_client" "web" {
  name         = "${var.name}-web"
  user_pool_id = aws_cognito_user_pool.main.id

  generate_secret                      = false
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "profile", "email"]
  allowed_oauth_flows_user_pool_client = true
  supported_identity_providers         = ["COGNITO"]
  explicit_auth_flows                  = ["ALLOW_REFRESH_TOKEN_AUTH", "ALLOW_USER_SRP_AUTH"]
  prevent_user_existence_errors        = "ENABLED"
  enable_token_revocation              = true

  access_token_validity  = 60
  id_token_validity      = 60
  refresh_token_validity = 30
  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }

  callback_urls = distinct(concat(["${var.app_url}/auth/callback"], var.extra_callback_urls))
  logout_urls   = distinct(concat([var.app_url], var.extra_logout_urls))
}

locals {
  issuer_url = "https://cognito-idp.${var.aws_region}.amazonaws.com/${aws_cognito_user_pool.main.id}"
}
