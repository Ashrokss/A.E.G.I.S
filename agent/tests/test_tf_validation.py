"""Pre-write validation applies wherever a file lives. A nested providers.tf
used to match none of the filename checks, so the provider-pin, single-provider
and hardcoded-credential checks silently stopped applying.
Run: python test_tf_validation.py"""
from agent import ProvisioningAgent

a = ProvisioningAgent()

GOOD = '''
terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = var.region
}
'''

# Valid content passes at either depth.
assert a._validate_tf_file_content("providers.tf", GOOD) == [], a._validate_tf_file_content("providers.tf", GOOD)
nested_good = a._validate_tf_file_content("environments/dev/providers.tf", GOOD)
assert nested_good == [], nested_good

# A providers.tf with no terraform{} block is caught at either depth.
BAD = 'provider "aws" {\n  region = "us-east-1"\n}\n'
assert a._validate_tf_file_content("providers.tf", BAD), "flat providers.tf was not checked"
nested_bad = a._validate_tf_file_content("environments/dev/providers.tf", BAD)
assert nested_bad, "nested providers.tf silently skipped validation"

# versions.tf stays refused wherever it lives (relaxed later, in sub-project E).
assert a._validate_tf_file_content("versions.tf", "terraform {}")
assert a._validate_tf_file_content("modules/vpc/versions.tf", "terraform {}")

# The credential ban is unconditional and applies to any path.
CREDS = 'provider "aws" {\n  access_key = "AKIAIOSFODNN7EXAMPLE"\n  region = "us-east-1"\n}\n'
for path in ("main.tf", "modules/vpc/main.tf", "environments/dev/main.tf"):
    errs = a._validate_tf_file_content(path, CREDS)
    assert any("credentials" in e for e in errs), (path, errs)

# Backslash-separated paths normalise too — the model sometimes emits them.
assert a._validate_tf_file_content("modules\\vpc\\versions.tf", "terraform {}")

print("ok: pre-write validation is directory-independent")
