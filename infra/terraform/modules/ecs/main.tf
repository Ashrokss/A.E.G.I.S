resource "aws_ecs_cluster" "main" {
  name = var.name

  # Off: about 3 USD/mo per task for metrics nobody reads on a personal stack.
  setting {
    name  = "containerInsights"
    value = "disabled"
  }
}

locals {
  image = { for k, url in var.repository_urls : k => "${url}:${var.image_tag}" }

  cognito_env = {
    AWS_COGNITO_USER_POOL_ID = var.cognito_user_pool_id
    AWS_COGNITO_CLIENT_ID    = var.cognito_client_id
    AWS_COGNITO_ISSUER_URL   = var.cognito_issuer_url
    AWS_COGNITO_JWKS_URL     = var.cognito_jwks_url
    # ABSOLUTE URL: the api builds `${AWS_COGNITO_DOMAIN}/oauth2/token` from it.
    AWS_COGNITO_DOMAIN = var.cognito_domain_url
  }

  bedrock_env = {
    BEDROCK_REGION           = var.aws_region
    BEDROCK_MODEL            = var.bedrock_model
    BEDROCK_MODEL_CHAT       = var.bedrock_model_chat
    BEDROCK_MODEL_CODEGEN    = var.bedrock_model_codegen
    BEDROCK_MODEL_HEAVY      = var.bedrock_model_heavy
    BEDROCK_EMBED_MODEL      = var.bedrock_embed_model
    BEDROCK_EMBED_DIMENSIONS = tostring(var.bedrock_embed_dimensions)
    MODEL_MAX_OUTPUT_TOKENS  = tostring(var.model_max_output_tokens)
  }

  log = {
    logDriver = "awslogs"
    options = {
      "awslogs-group"  = var.log_group_name
      "awslogs-region" = var.aws_region
    }
  }

  env = { for c, m in {
    api = merge(local.cognito_env, {
      PORT       = "3005"
      AWS_REGION = var.aws_region

      POSTGRES_HOST = var.db_endpoint
      POSTGRES_PORT = "5432"
      POSTGRES_DB   = var.db_name
      POSTGRES_USER = var.db_username
      POSTGRES_SSL  = "true"
      # Baked into the api image (api/Dockerfile).
      POSTGRES_SSL_CA = "/app/rds-ca.pem"
      # Read per new connection, so an RDS password rotation never strands
      # the running task (src/db/pool.js).
      POSTGRES_PASSWORD_SECRET_ARN = var.db_master_secret_arn

      AWS_ONBOARDING_TEMPLATE_URL = var.onboarding_template_url
      AEGIS_TASK_ROLE_ARN         = var.task_role_arn
    })
    agent = merge(local.cognito_env, local.bedrock_env, {
      AWS_REGION           = var.aws_region
      BACKEND_INTERNAL_URL = "http://127.0.0.1:3005" # same task, same network namespace

      AGENT_DB_PATH       = "/mnt/agent/runs.db"
      AGENT_WORKSPACE_DIR = "/mnt/agent/runs"
      # Terraform SILENTLY ignores this when the directory is absent; main.py
      # creates it. Without it every run pulls the AWS provider onto EFS.
      TF_PLUGIN_CACHE_DIR = "/mnt/agent/tf-plugin-cache"
      TF_PLUGIN_TIMEOUT   = "120"

      MANDATORY_DOC_FETCH_STRICT = "true"
      JIRA_POLL_SECONDS          = "0"
    })
  } : c => [for k, v in m : { name = k, value = v }] }
}

resource "aws_ecs_task_definition" "app" {
  family                   = var.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = var.execution_role_arn
  task_role_arn            = var.task_role_arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64" # Graviton: about 20% cheaper; both images build for arm64
  }

  volume {
    name = "agent-state"
    efs_volume_configuration {
      file_system_id     = var.efs_id
      transit_encryption = "ENABLED"
      authorization_config {
        access_point_id = var.efs_access_points["agent"].id
        iam             = "ENABLED"
      }
    }
  }

  container_definitions = jsonencode([
    {
      name         = "api"
      image        = local.image["api"]
      essential    = true
      portMappings = [{ containerPort = 3005, protocol = "tcp" }]
      environment  = local.env["api"]
      secrets = [
        { name = "POSTGRES_PASSWORD", valueFrom = var.db_password_value_from },
        { name = "AWS_ONBOARDING_ENC_KEY", valueFrom = var.secret_value_from["AWS_ONBOARDING_ENC_KEY"] },
        { name = "INTEGRATIONS_ENC_KEY", valueFrom = var.secret_value_from["INTEGRATIONS_ENC_KEY"] },
      ]
      logConfiguration = merge(local.log, {
        options = merge(local.log.options, { "awslogs-stream-prefix" = "api" })
      })
    },
    {
      name         = "agent"
      image        = local.image["agent"]
      essential    = true
      portMappings = [{ containerPort = 8000, protocol = "tcp" }]
      environment  = local.env["agent"]
      secrets = [for k in ["TAVILY_API_KEY", "JIRA_API_TOKEN", "JIRA_WEBHOOK_SECRET"] :
      { name = k, valueFrom = var.secret_value_from[k] }]
      mountPoints = [{ sourceVolume = "agent-state", containerPath = "/mnt/agent", readOnly = false }]
      dependsOn   = [{ containerName = "api", condition = "START" }]
      logConfiguration = merge(local.log, {
        options = merge(local.log.options, { "awslogs-stream-prefix" = "agent" })
      })
    },
  ])
}

resource "aws_ecs_service" "app" {
  name             = var.name
  cluster          = aws_ecs_cluster.main.id
  task_definition  = aws_ecs_task_definition.app.arn
  launch_type      = "FARGATE" # not Spot: a reclaim mid-apply loses local run state
  platform_version = "1.4.0"   # EFS volumes need >= 1.4.0

  # ONE, by design: per-run STS creds, SSE queues and run locks are
  # per-process, and runs.db is SQLite on EFS with a single writer.
  desired_count = 1
  # Stop-then-start, so two tasks never write runs.db at once. Deploys have a
  # short gap; do them when no run is applying.
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100
  health_check_grace_period_seconds  = 120
  enable_execute_command             = true

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = var.subnet_ids
    security_groups  = [var.app_security_group_id]
    assign_public_ip = var.assign_public_ip # egress only; the SG admits nothing but the ALB
  }

  load_balancer {
    target_group_arn = var.target_group_arns["api"]
    container_name   = "api"
    container_port   = 3005
  }

  load_balancer {
    target_group_arn = var.target_group_arns["agent"]
    container_name   = "agent"
    container_port   = 8000
  }

  depends_on = [var.listener_arn]
}
