# AEGIS local development: ONLY a Cognito user pool, so `docker compose up` +
# `npm run dev` can sign in for real. Nothing else is created (no VPC, no ECS,
# no database): the stack runs on your machine.
#
#   cd infra/terraform/environments/local
#   terraform init && terraform apply
#   terraform output -raw env        paste over the Cognito block in the root .env
#   terraform output -raw web_env  > ../../../../web/.env.local   (Vite, gitignored)
#
# State is local (terraform.tfstate here, gitignored): this pool is disposable,
# and `terraform destroy` removes it (deletion protection is off on purpose).
# Cost: nothing at this scale (Cognito's free tier).

terraform {
  required_version = ">= 1.11.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region  = var.aws_region
  profile = var.aws_profile

  default_tags {
    tags = {
      Project     = "aegis"
      Environment = "local"
      ManagedBy   = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {}

module "cognito" {
  source = "../../modules/cognito"

  name       = "aegis-local"
  aws_region = var.aws_region
  account_id = data.aws_caller_identity.current.account_id

  # The Vite dev server is the app.
  app_url             = "http://localhost:5173"
  extra_callback_urls = []
  extra_logout_urls   = []

  self_signup_enabled = var.self_signup_enabled
  deletion_protection = false
}

output "env" {
  description = "Lines for the root .env (api + agent)."
  value       = <<-EOT
    AWS_COGNITO_USER_POOL_ID=${module.cognito.user_pool_id}
    AWS_COGNITO_CLIENT_ID=${module.cognito.client_id}
    AWS_COGNITO_DOMAIN=${module.cognito.hosted_ui_url}
    AWS_COGNITO_ISSUER_URL=${module.cognito.issuer_url}
    AWS_COGNITO_JWKS_URL=${module.cognito.jwks_url}
    AEGIS_SELF_SIGNUP=${var.self_signup_enabled}
  EOT
}

output "web_env" {
  description = "web/.env.local for `npm run dev`."
  value       = <<-EOT
    VITE_COGNITO_DOMAIN=${module.cognito.hosted_ui_url}
    VITE_COGNITO_CLIENT_ID=${module.cognito.client_id}
    VITE_SELF_SIGNUP=${var.self_signup_enabled}
  EOT
}
