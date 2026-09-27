# Roles and policies. There are NO IAM users and NO access keys in AEGIS: the
# ECS task role is the only runtime identity, and it cannot provision anything
# itself. Terraform runs as an AegisProvisioner-* role the user deploys into
# their target account (infra/onboarding/aegis-provisioner-role.yaml), assumed
# with an ExternalId the api generated.

data "aws_iam_policy_document" "ecs_tasks_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
    # Confused-deputy guard: only this account, only its ECS tasks.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:ecs:${var.aws_region}:${var.account_id}:*"]
    }
  }
}

# ── Execution role: what the ECS agent does on the task's behalf ──────────────

resource "aws_iam_role" "execution" {
  name               = "${var.name}-task-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "execution_secrets" {
  statement {
    sid       = "ReadTheTwoSecrets"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [var.app_secret_arn, var.db_master_secret_arn]
  }

  statement {
    sid       = "DecryptThoseSecretsOnly"
    actions   = ["kms:Decrypt"]
    resources = ["arn:aws:kms:${var.aws_region}:${var.account_id}:key/*"]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["secretsmanager.${var.aws_region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "execution_secrets" {
  name   = "ReadTaskSecrets"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution_secrets.json
}

# ── Task role: what the application code can do ───────────────────────────────

resource "aws_iam_role" "task" {
  name               = "${var.name}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

data "aws_iam_policy_document" "task" {
  statement {
    sid = "MountAgentState"
    actions = [
      "elasticfilesystem:ClientMount",
      "elasticfilesystem:ClientWrite",
    ]
    resources = [var.efs_arn]
    condition {
      test     = "StringEquals"
      variable = "elasticfilesystem:AccessPointArn"
      values   = var.efs_access_point_arns
    }
  }

  # Both ARN shapes: cross-region inference profiles ("us." model ids)
  # authorise against the profile AND the foundation model in every region the
  # profile routes to.
  statement {
    sid = "InvokeBedrock"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
      "bedrock:Converse",
      "bedrock:ConverseStream",
    ]
    resources = [
      "arn:aws:bedrock:*::foundation-model/*",
      "arn:aws:bedrock:*:${var.account_id}:inference-profile/*",
    ]
  }

  # The Admin page: create / look up / remove accounts in THIS pool only.
  statement {
    sid = "ManageAppUsers"
    actions = [
      "cognito-idp:AdminCreateUser",
      "cognito-idp:AdminDeleteUser",
      "cognito-idp:AdminGetUser",
      "cognito-idp:AdminSetUserPassword",
      "cognito-idp:ListUsers",
    ]
    resources = [var.user_pool_arn]
  }

  # Provisioning identity. Only roles named AegisProvisioner-*, and the target
  # role trust policy additionally demands this exact role AND the ExternalId.
  statement {
    sid       = "AssumeProvisionerRoles"
    actions   = ["sts:AssumeRole"]
    resources = ["arn:aws:iam::*:role/AegisProvisioner-*"]
  }

  # The api rotates-safe DB password read (src/db/pool.js).
  statement {
    sid       = "ReadDbPassword"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [var.db_master_secret_arn]
  }

  # ECS Exec: the way in for create-admin.js and inspecting runs.db.
  statement {
    sid = "EcsExec"
    actions = [
      "ssmmessages:CreateControlChannel",
      "ssmmessages:CreateDataChannel",
      "ssmmessages:OpenControlChannel",
      "ssmmessages:OpenDataChannel",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "task" {
  name   = "AppRuntime"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}

# ── AWS Backup ────────────────────────────────────────────────────────────────

data "aws_iam_policy_document" "backup_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["backup.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "backup" {
  name               = "${var.name}-backup"
  assume_role_policy = data.aws_iam_policy_document.backup_trust.json
}

resource "aws_iam_role_policy_attachment" "backup" {
  role       = aws_iam_role.backup.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForBackup"
}
