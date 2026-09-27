# AEGIS production — the only environment for now (add environments/dev the
# same way when needed). Sizing is literal and committed; per-account values
# live in the gitignored terraform.tfvars.
#
# Rough monthly cost (us-east-1, before Bedrock usage):
#   Fargate ARM64 1 vCPU / 3 GB on-demand ...... ~31
#   RDS db.t4g.micro single-AZ + 20 GB gp3 ...... ~15
#   Internal ALB ................................ ~17
#   EFS, Secrets, logs, backup, CloudFront ...... ~5-8
#   Task public IPv4 (egress only) .............. ~3.6
#   ---------------------------------------------------
#                                               ~72

module "stack" {
  source = "../../modules/stack"

  project    = "aegis"
  aws_region = var.aws_region
  account_id = var.account_id
  image_tag  = var.image_tag

  provisioner_template_path = "${path.root}/../../../onboarding/aegis-provisioner-role.yaml"

  vpc_cidr            = "10.40.0.0/16"
  public_subnet_cidrs = ["10.40.0.0/24", "10.40.1.0/24"]
  app_subnet_cidrs    = ["10.40.10.0/24", "10.40.11.0/24"]
  data_subnet_cidrs   = ["10.40.20.0/24", "10.40.21.0/24"]
  enable_nat_gateway  = false

  task_cpu    = 1024
  task_memory = 3072

  db_instance_class        = "db.t4g.micro"
  db_multi_az              = false
  db_name                  = "aegis"
  db_username              = "aegis"
  db_backup_retention_days = 7
  db_deletion_protection   = true

  cloudfront_price_class      = "PriceClass_100"
  enable_waf                  = false
  cognito_extra_callback_urls = ["http://localhost:5173/auth/callback"]
  cognito_extra_logout_urls   = ["http://localhost:5173"]

  log_retention_days      = 14
  alarm_email             = var.alarm_email
  monthly_budget_usd      = 80
  efs_storage_alarm_bytes = 20 * 1024 * 1024 * 1024
  enable_guardduty        = false

  # Cross-region inference profiles (the "us." prefix).
  bedrock_model            = var.bedrock_model_codegen
  bedrock_model_chat       = var.bedrock_model_chat
  bedrock_model_codegen    = var.bedrock_model_codegen
  bedrock_model_heavy      = var.bedrock_model_codegen
  bedrock_embed_model      = var.bedrock_embed_model
  bedrock_embed_dimensions = 1024
  model_max_output_tokens  = 8192
}
