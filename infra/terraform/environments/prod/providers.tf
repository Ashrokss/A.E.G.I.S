provider "aws" {
  region = var.aws_region
  # A mistyped profile can never apply this to the wrong account.
  allowed_account_ids = [var.account_id]

  default_tags {
    tags = {
      Project     = "aegis"
      Environment = "prod"
      ManagedBy   = "terraform"
      Source      = "infra/terraform/environments/prod"
    }
  }
}
