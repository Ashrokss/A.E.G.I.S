"""Module reuse against a real customer layout: aws/<account>/<env>/<workload>/
root modules plus modules/aws/<name> shared modules.
Run: python test_module_reuse.py"""
from tools.github_repo import RepoFile, RepoSnapshot, select_paths
from tools.repo_profile import (
    build_profile,
    module_source_for,
    render_prompt_block,
    target_dir_for,
    workload_slug,
)

PROVIDER = '''
terraform {
  required_version = ">= 1.3"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}
provider "aws" {
  region = local.region
  default_tags {
    tags = local.tags
  }
}
'''

EC2_MAIN = '''
locals {
  tags = { environment = "uat", purpose = "compute" }
}
module "ec2" {
  source        = "../../../../modules/aws/ec2-instance"
  instance_name = "uat-app-01"
}
'''

EC2_MODULE_VARS = '''
variable "instance_name" {
  type = string
}
variable "instance_type" {
  type    = string
  default = "t3.micro"
}
variable "subnet_id" {
  description = "Subnet to launch in"
  type        = string
}
'''

EC2_MODULE_MAIN = '''
resource "aws_instance" "this" {
  instance_type = var.instance_type
  tags          = { Name = var.instance_name }
}
resource "aws_ebs_volume" "data" {}
'''

AZ_MAIN = '''
locals { tags = { environment = "nonprod" } }
module "subscription" {
  source = "../../../modules/azure/subscription"
}
resource "azurerm_subnet" "x" {}
'''


def snap():
    s = RepoSnapshot(repo_full_name="example/existing-infra", base_branch="main", head_sha="f" * 40)
    s.all_paths = [
        "aws/account-landingzone-nonprod/uat/ec2/provider.tf",
        "aws/account-landingzone-nonprod/uat/ec2/main.tf",
        "aws/account-landingzone-nonprod/uat/lambda/provider.tf",
        "aws/account-landingzone-nonprod/uat/lambda/main.tf",
        "azure/sub-landingzone-nonprod/subscription/main.tf",
        "modules/aws/ec2-instance/main.tf",
        "modules/aws/ec2-instance/variables.tf",
        "modules/aws/lambda/main.tf",
        "modules/aws/cloudwatch/main.tf",
        "modules/azure/subscription/main.tf",
    ]
    s.files = [
        RepoFile("aws/account-landingzone-nonprod/uat/ec2/provider.tf", len(PROVIDER), PROVIDER),
        RepoFile("aws/account-landingzone-nonprod/uat/ec2/main.tf", len(EC2_MAIN), EC2_MAIN),
        RepoFile("aws/account-landingzone-nonprod/uat/lambda/provider.tf", len(PROVIDER), PROVIDER),
        RepoFile("azure/sub-landingzone-nonprod/subscription/main.tf", len(AZ_MAIN), AZ_MAIN),
        RepoFile("modules/aws/ec2-instance/variables.tf", len(EC2_MODULE_VARS), EC2_MODULE_VARS),
        RepoFile("modules/aws/ec2-instance/main.tf", len(EC2_MODULE_MAIN), EC2_MODULE_MAIN),
    ]
    return s


p = build_profile(snap())
layout = p["layout"]

# Leaf module dirs, not the `modules` / `modules/aws` containers.
paths = {m["path"] for m in layout["local_modules"]}
assert "modules/aws/ec2-instance" in paths, paths
assert "modules" not in paths and "modules/aws" not in paths, paths
assert layout["modules_dir"] == "modules/aws", layout["modules_dir"]

# Inputs are parsed with required-ness — "required inputs: none" for a module
# that clearly has required variables is what made the agent call it with no
# arguments.
ec2 = next(m for m in layout["local_modules"] if m["path"] == "modules/aws/ec2-instance")
req = {i["name"] for i in ec2["inputs"] if i["required"]}
opt = {i["name"] for i in ec2["inputs"] if not i["required"]}
assert req == {"instance_name", "subnet_id"}, req
assert opt == {"instance_type"}, opt
assert "aws_instance" in ec2["manages"], ec2["manages"]
assert ec2["interface_read"] is True

# A module whose files were never read must NOT claim it takes no inputs.
cw = next(m for m in layout["local_modules"] if m["path"] == "modules/aws/cloudwatch")
assert cw["interface_read"] is False, cw
assert "inputs: not read" in render_prompt_block(p), "unread module must be flagged"

# Per-workload layout is recognised, so a new workload gets a NEW sibling.
assert layout["per_workload_parent"] == "aws/account-landingzone-nonprod/uat", layout

# An S3 ticket must not land in the ec2 directory.
S3_GOAL = "Execute KAN-20: Create an S3 bucket named 'aegisdemostorage' with versioning and encryption enabled"
assert workload_slug(S3_GOAL) == "s3", workload_slug(S3_GOAL)
t = target_dir_for(p, S3_GOAL)
assert t == "aws/account-landingzone-nonprod/uat/s3", t

# An EC2 ticket reuses the existing workload directory.
assert target_dir_for(p, "Execute KAN-21: add another EC2 instance") == \
    "aws/account-landingzone-nonprod/uat/ec2"

# Unrecognised workload falls back rather than inventing a directory.
assert target_dir_for(p, "Execute KAN-22: do something vague") == layout["target_dir"]

# The source path depth must match the target directory depth: 4 segments -> 4 ups.
src = module_source_for("aws/account-landingzone-nonprod/uat/s3", "modules/aws/s3-bucket")
assert src == "../../../../modules/aws/s3-bucket", src
assert module_source_for("", "modules/aws/x") == "./modules/aws/x"

# The rendered block carries the exact source string and the reuse decision.
p["_target_dir"] = t
block = render_prompt_block(p)
assert '"../../../../modules/aws/ec2-instance"' in block, block
assert "aws/account-landingzone-nonprod/uat/s3" in block, block
assert "Decide FIRST" in block, block
assert "aws_instance" in block, block
# Azure content must not be offered as a style example on an AWS run.
assert "azurerm" not in block, block
assert "subscription" not in block, block

# Module interfaces get their own read budget, or reuse can never work.
sizes = {q: 100 for q in snap().all_paths}
picked = select_paths(snap().all_paths, sizes)
assert "modules/aws/ec2-instance/variables.tf" in picked, picked
assert "modules/aws/cloudwatch/main.tf" in picked, picked

print("ok: existing modules are matched, called with the right source, and new workloads get their own directory")
