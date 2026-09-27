"""The effective skill is per-customer: SKILL.md stays the invariant base and a
per-run conventions block overrides its stylistic defaults. Security rules never
bend. Run: python test_repo_conventions_prompt.py"""
import agent as agent_mod
from tools import repo_profile

a = agent_mod.ProvisioningAgent()

PROFILE = repo_profile.validate_profile({
    "repo_full_name": "acme/infra",
    "base_branch": "main",
    "head_sha": "a" * 40,
    "status": repo_profile.STATUS_OK,
    "analyzer_version": repo_profile.ANALYZER_VERSION,
    "layout": {"style": "modules_and_envs", "target_dir": "environments/dev",
               "modules_dir": "modules", "root_modules": ["environments/dev"],
               "local_modules": [{"path": "modules/vpc", "inputs": [
                   {"name": "name", "type": "string", "required": True},
                   {"name": "cidr_block", "type": "string", "required": True}]}]},
    "providers": {"required_version": ">= 1.6.0", "aws_version_constraint": "~> 5.60",
                  "regions": ["ap-southeast-1"]},
    "files": {"versions_file": "versions.tf", "observed": ["main.tf", "versions.tf"],
              "tfvars_files": ["dev.tfvars"]},
    "naming": {"resource_label_case": "snake_case", "physical_name_pattern": "${var.project}-x"},
    "tagging": {"mechanism": "locals_common_tags", "required_keys": ["Environment", "CostCenter"]},
    "backend": {"type": "s3"},
    "confidence": "high",
    "excerpts": [],
})

# --- the prompt block -------------------------------------------------------

block = a._repo_pattern_prompt_from(PROFILE)
assert "~> 5.60" in block, block
assert "environments/dev" in block, block
assert "versions.tf" in block, block
assert "CostCenter" in block, block
assert "modules/vpc" in block, block
# The agent must never emit a backend block even though the repo has one.
assert "Do NOT write a `backend`" in block, block

# No repo connected, or nothing cached: no block at all, so behaviour is
# byte-identical to before this feature.
assert a._repo_pattern_prompt_from(None) == ""
assert a._repo_pattern_prompt_from({"status": repo_profile.STATUS_ERROR}) == ""

# --- the validator follows the customer's pins ------------------------------

THEIRS = '''
terraform {
  required_version = ">= 1.6.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
  }
}
provider "aws" {
  region = var.region
}
'''

# Without a profile the platform default (~> 6.0) is still required.
assert any("~> 6.0" in e for e in a._validate_tf_file_content("providers.tf", THEIRS)), \
    a._validate_tf_file_content("providers.tf", THEIRS)

# With the profile, their pin is accepted and the platform default is not imposed.
assert a._validate_tf_file_content("providers.tf", THEIRS, profile=PROFILE) == [], \
    a._validate_tf_file_content("providers.tf", THEIRS, profile=PROFILE)

# ...and now the platform default is the one that's wrong for this repo.
PLATFORM = THEIRS.replace("~> 5.60", "~> 6.0")
assert any("~> 5.60" in e for e in a._validate_tf_file_content("providers.tf", PLATFORM, profile=PROFILE)), \
    a._validate_tf_file_content("providers.tf", PLATFORM, profile=PROFILE)

# versions.tf is allowed when the repo uses it, and still refused when it doesn't.
VERSIONS = 'terraform {\n  required_version = ">= 1.6.0"\n}\n'
assert a._validate_tf_file_content("versions.tf", VERSIONS, profile=PROFILE) == [], \
    a._validate_tf_file_content("versions.tf", VERSIONS, profile=PROFILE)
assert a._validate_tf_file_content("versions.tf", VERSIONS), "banned without a profile saying otherwise"
FLAT = dict(PROFILE, files=dict(PROFILE["files"], versions_file="providers.tf"))
assert a._validate_tf_file_content("versions.tf", VERSIONS, profile=FLAT), \
    "a repo that uses providers.tf must still refuse versions.tf"

# --- security never bends ---------------------------------------------------

# A backend block is refused with or without a profile: copying the customer's
# backend would make terraform init adopt their real state and an approved apply
# mutate production.
BACKEND = THEIRS.replace(
    'provider "aws" {',
    'terraform {\n  backend "s3" {\n    bucket = "acme-tfstate"\n  }\n}\nprovider "aws" {',
)
for prof in (None, PROFILE):
    errs = a._validate_tf_file_content("providers.tf", BACKEND, profile=prof)
    assert any("backend" in e.lower() for e in errs), (prof is not None, errs)

CLOUD = THEIRS + '\nterraform {\n  cloud {\n    organization = "acme"\n  }\n}\n'
assert any("backend" in e.lower() or "cloud" in e.lower()
           for e in a._validate_tf_file_content("main.tf", CLOUD, profile=PROFILE))

# Hardcoded credentials stay banned regardless of the profile.
CREDS = 'provider "aws" {\n  access_key = "AKIAIOSFODNN7EXAMPLE"\n  region = "us-east-1"\n}\n'
assert any("credentials" in e for e in a._validate_tf_file_content("main.tf", CREDS, profile=PROFILE))

# A profile cannot make a module source escape the workspace.
ESCAPE = 'module "x" {\n  source = "/etc/passwd"\n}\n'
assert any("source" in e.lower() for e in a._validate_tf_file_content("main.tf", ESCAPE, profile=PROFILE)), \
    a._validate_tf_file_content("main.tf", ESCAPE, profile=PROFILE)

# Whitespace inside the constraint is not significant: "~>5.60" is the same pin.
TIGHT = THEIRS.replace('"~> 5.60"', '"~>5.60"')
assert a._validate_tf_file_content("providers.tf", TIGHT, profile=PROFILE) == [], \
    a._validate_tf_file_content("providers.tf", TIGHT, profile=PROFILE)
NO_SPACE_DEFAULT = THEIRS.replace('"~> 5.60"', '"~>6.0"')
assert a._validate_tf_file_content("providers.tf", NO_SPACE_DEFAULT) == [], \
    a._validate_tf_file_content("providers.tf", NO_SPACE_DEFAULT)

print("ok: repo conventions override style, never security")
