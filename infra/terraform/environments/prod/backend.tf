# Partial configuration: the bucket (which names the account) comes from the
# gitignored backend.hcl that infra/terraform/bootstrap writes:
#   terraform init -backend-config=backend.hcl
terraform {
  backend "s3" {
    key          = "aegis/prod.tfstate"
    encrypt      = true
    use_lockfile = true # S3 native locking; needs Terraform >= 1.11
  }
}
