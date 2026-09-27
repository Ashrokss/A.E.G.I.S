# ── EFS: the state that must survive a task replacement ───────────────────────
#
# Elastic throughput, so there is no provisioned size to get wrong -- "sizing"
# here means budgeting, and the number to watch is Terraform workspace growth
# (see the StorageBytes alarm in observability.tf).

resource "aws_efs_file_system" "state" {
  creation_token = "${var.name}-state"
  encrypted      = true

  # IA after 30 days, back to standard on first access. The bulk of what lands
  # here is old per-run workspaces nobody reads again.
  lifecycle_policy {
    transition_to_ia = "AFTER_30_DAYS"
  }
  lifecycle_policy {
    transition_to_primary_storage_class = "AFTER_1_ACCESS"
  }

  tags = { Name = "efs-${var.name}-state" }
}

resource "aws_efs_mount_target" "state" {
  count           = 2
  file_system_id  = aws_efs_file_system.state.id
  subnet_id       = var.app_subnet_ids[count.index]
  security_groups = [var.efs_security_group_id]
}

# Deny anything that is not a TLS mount from the task role. The task role
# policy narrows further, to these access points specifically.
resource "aws_efs_file_system_policy" "state" {
  file_system_id = aws_efs_file_system.state.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = var.task_role_arn }
      Action = [
        "elasticfilesystem:ClientMount",
        "elasticfilesystem:ClientWrite",
        "elasticfilesystem:ClientRootAccess",
      ]
      Resource  = aws_efs_file_system.state.arn
      Condition = { Bool = { "elasticfilesystem:AccessedViaMountTarget" = "true" } }
      }, {
      Effect    = "Deny"
      Principal = { AWS = "*" }
      Action    = "*"
      Resource  = aws_efs_file_system.state.arn
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

# One access point per writer. The agent is the only one (runs.db, run
# workspaces, provider cache); the api keeps its state in PostgreSQL.
# uid/gid 1000 = the non-root `aegis` user in agent/Dockerfile.
resource "aws_efs_access_point" "state" {
  for_each = var.access_points

  file_system_id = aws_efs_file_system.state.id

  posix_user {
    uid = 1000
    gid = 1000
  }

  # Created on first mount, which also means the parent directory for runs.db
  # exists before sqlite3.connect() -- which does not create one.
  root_directory {
    path = each.value
    creation_info {
      owner_uid   = 1000
      owner_gid   = 1000
      permissions = "0700"
    }
  }

  tags = { Name = "efs-ap-${each.key}-${var.name}" }
}

# Daily backup of the file system: runs.db holds every run's history and the
# knowledge base.
resource "aws_backup_vault" "state" {
  name = "${var.name}-state"
}

resource "aws_backup_plan" "state" {
  name = "${var.name}-state-daily"

  rule {
    rule_name         = "daily"
    target_vault_name = aws_backup_vault.state.name
    schedule          = "cron(0 5 ? * * *)"
    lifecycle {
      delete_after = 30
    }
  }
}

resource "aws_backup_selection" "state" {
  name         = "${var.name}-efs"
  plan_id      = aws_backup_plan.state.id
  iam_role_arn = var.backup_role_arn
  resources    = [aws_efs_file_system.state.arn]
}
