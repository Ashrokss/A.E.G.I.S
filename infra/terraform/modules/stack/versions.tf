terraform {
  # 1.11 is the floor for S3 native locking (use_lockfile in backend.tf).
  # On an older CLI that argument is silently ignored, which is the one way
  # this bites: two applies would race with no lock and no warning.
  required_version = ">= 1.11.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}
