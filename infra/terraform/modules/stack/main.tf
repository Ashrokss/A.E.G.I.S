# The whole platform, wired together. Creates nothing itself; environments/*
# differ only in the values they pass.

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  name = var.project
  azs  = slice(data.aws_availability_zones.available.names, 0, 2)

  # efs needs the task role (filesystem policy, backup selection) and iam needs
  # the EFS access points: a real cycle. The role ARNs are deterministic from
  # the names modules/iam assigns, so they are computed here.
  task_role_arn   = "arn:aws:iam::${var.account_id}:role/${local.name}-task"
  backup_role_arn = "arn:aws:iam::${var.account_id}:role/${local.name}-backup"
}

module "networking" {
  source = "../networking"

  name                = local.name
  vpc_cidr            = var.vpc_cidr
  availability_zones  = local.azs
  public_subnet_cidrs = var.public_subnet_cidrs
  app_subnet_cidrs    = var.app_subnet_cidrs
  data_subnet_cidrs   = var.data_subnet_cidrs
  enable_nat_gateway  = var.enable_nat_gateway
}

module "ecr" {
  source = "../ecr"
  name   = local.name
}

module "efs" {
  source = "../efs"

  name                  = local.name
  app_subnet_ids        = module.networking.app_subnet_ids
  efs_security_group_id = module.networking.efs_security_group_id
  task_role_arn         = local.task_role_arn
  backup_role_arn       = local.backup_role_arn
}

module "s3" {
  source = "../s3"

  name                      = local.name
  account_id                = var.account_id
  provisioner_template_path = var.provisioner_template_path
}

module "rds" {
  source = "../rds"

  name                  = local.name
  data_subnet_ids       = module.networking.data_subnet_ids
  db_security_group_id  = module.networking.db_security_group_id
  instance_class        = var.db_instance_class
  multi_az              = var.db_multi_az
  db_name               = var.db_name
  username              = var.db_username
  backup_retention_days = var.db_backup_retention_days
  deletion_protection   = var.db_deletion_protection
}

module "alb" {
  source = "../alb"

  name                  = local.name
  vpc_id                = module.networking.vpc_id
  subnet_ids            = module.networking.app_subnet_ids
  alb_security_group_id = module.networking.alb_security_group_id
}

module "cloudfront" {
  source = "../cloudfront"

  name                   = local.name
  account_id             = var.account_id
  alb_arn                = module.alb.arn
  alb_dns_name           = module.alb.dns_name
  cloudfront_price_class = var.cloudfront_price_class
  enable_waf             = var.enable_waf
}

module "cognito" {
  source = "../cognito"

  name                = local.name
  aws_region          = var.aws_region
  account_id          = var.account_id
  app_url             = module.cloudfront.app_url
  extra_callback_urls = var.cognito_extra_callback_urls
  extra_logout_urls   = var.cognito_extra_logout_urls
  self_signup_enabled = var.self_signup_enabled
}

module "cloudwatch" {
  source = "../cloudwatch"

  name                      = local.name
  log_retention_days        = var.log_retention_days
  alarm_email               = var.alarm_email
  monthly_budget_usd        = var.monthly_budget_usd
  efs_id                    = module.efs.id
  alb_arn_suffix            = module.alb.arn_suffix
  target_group_arn_suffixes = module.alb.target_group_arn_suffixes
  db_identifier             = module.rds.identifier
  efs_storage_alarm_bytes   = var.efs_storage_alarm_bytes
}

module "guardduty" {
  source = "../guardduty"

  name            = local.name
  enabled         = var.enable_guardduty
  alarm_topic_arn = module.cloudwatch.alarm_topic_arn
}

module "secrets_manager" {
  source = "../secrets-manager"
  name   = local.name
}

module "iam" {
  source = "../iam"

  name                  = local.name
  aws_region            = var.aws_region
  account_id            = var.account_id
  app_secret_arn        = module.secrets_manager.app_secret_arn
  db_master_secret_arn  = module.rds.master_secret_arn
  efs_arn               = module.efs.arn
  efs_access_point_arns = [for ap in module.efs.access_points : ap.arn]
  user_pool_arn         = module.cognito.user_pool_arn
}

module "ecs" {
  source = "../ecs"

  name       = local.name
  aws_region = var.aws_region

  repository_urls = module.ecr.repository_urls
  image_tag       = var.image_tag
  task_cpu        = var.task_cpu
  task_memory     = var.task_memory

  subnet_ids            = module.networking.app_subnet_ids
  app_security_group_id = module.networking.app_security_group_id
  assign_public_ip      = module.networking.tasks_need_public_ip
  target_group_arns     = module.alb.target_group_arns
  listener_arn          = module.alb.listener_arn

  execution_role_arn = module.iam.execution_role_arn
  task_role_arn      = module.iam.task_role_arn

  efs_id            = module.efs.id
  efs_access_points = module.efs.access_points
  log_group_name    = module.cloudwatch.log_group_name

  db_endpoint            = module.rds.endpoint
  db_name                = var.db_name
  db_username            = var.db_username
  db_password_value_from = module.rds.password_value_from
  db_master_secret_arn   = module.rds.master_secret_arn
  secret_value_from      = module.secrets_manager.secret_value_from
  self_signup_enabled    = var.self_signup_enabled

  cognito_user_pool_id = module.cognito.user_pool_id
  cognito_client_id    = module.cognito.client_id
  cognito_domain_url   = module.cognito.hosted_ui_url
  cognito_issuer_url   = module.cognito.issuer_url
  cognito_jwks_url     = module.cognito.jwks_url

  onboarding_template_url = module.s3.provisioner_template_url

  bedrock_model            = var.bedrock_model
  bedrock_model_chat       = var.bedrock_model_chat
  bedrock_model_codegen    = var.bedrock_model_codegen
  bedrock_model_heavy      = var.bedrock_model_heavy
  bedrock_embed_model      = var.bedrock_embed_model
  bedrock_embed_dimensions = var.bedrock_embed_dimensions
  model_max_output_tokens  = var.model_max_output_tokens
}
