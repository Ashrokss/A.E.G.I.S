resource "aws_cloudwatch_log_group" "app" {
  # One group, four stream prefixes. All four containers of one incident land
  # in one place, filterable by prefix, with one retention policy to get wrong.
  name              = "/ecs/${var.name}"
  retention_in_days = var.log_retention_days
}

resource "aws_sns_topic" "alarms" {
  name = "${var.name}-alarms"
}

resource "aws_sns_topic_subscription" "alarms_email" {
  count     = var.alarm_email == "" ? 0 : 1
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

# ── The deep health check that the target group deliberately does not do ──────
#
# alb.tf checks "/" so an RDS failover cannot delete the only task. That leaves
# "Postgres is unreachable" observed rather than acted on, and THIS is what
# observes it: server.js logs this exact string when db.ping() throws.
resource "aws_cloudwatch_log_metric_filter" "db_unreachable" {
  name           = "${var.name}-db-unreachable"
  log_group_name = aws_cloudwatch_log_group.app.name
  pattern        = "\"[Health] PostgreSQL unreachable\""

  metric_transformation {
    name      = "DatabaseUnreachable"
    namespace = var.name
    value     = "1"
    unit      = "Count"
  }
}

resource "aws_cloudwatch_metric_alarm" "db_unreachable" {
  alarm_name          = "${var.name}-database-unreachable"
  alarm_description   = "The app cannot reach Postgres. Every authenticated request is failing; the site still answers 200."
  namespace           = var.name
  metric_name         = "DatabaseUnreachable"
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 2
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
}

# ── The one that guards the single-writer invariant ───────────────────────────
#
# runs.db is SQLite on NFS and the Terraform workspaces are instance-bound. A
# second task -- from a manual update-service, or a stuck deployment -- is a
# corruption risk, not merely a duplicate-ticket one. Nothing in the
# application would notice; this does.
resource "aws_cloudwatch_metric_alarm" "efs_growth" {
  alarm_name          = "${var.name}-efs-storage"
  alarm_description   = "State filesystem past 50 GB. Almost certainly un-pruned Terraform workspaces -- prune old runs under /agent/runs."
  namespace           = "AWS/EFS"
  metric_name         = "StorageBytes"
  statistic           = "Average"
  period              = 3600
  evaluation_periods  = 1
  threshold           = var.efs_storage_alarm_bytes
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]

  dimensions = {
    FileSystemId = var.efs_id
    StorageClass = "Total"
  }
}

# ── Ordinary service health ───────────────────────────────────────────────────

resource "aws_cloudwatch_metric_alarm" "no_healthy_targets" {
  for_each            = var.target_group_arn_suffixes
  alarm_name          = "${var.name}-${each.key}-no-healthy-targets"
  alarm_description   = "The ALB has no healthy ${each.key} target. That half of AEGIS is down."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HealthyHostCount"
  statistic           = "Minimum"
  period              = 60
  evaluation_periods  = 3
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  # Deploys are stop-then-start, so a gap here is expected during one.
  treat_missing_data = "breaching"
  alarm_actions      = [aws_sns_topic.alarms.arn]

  dimensions = {
    LoadBalancer = var.alb_arn_suffix
    TargetGroup  = each.value
  }
}

resource "aws_cloudwatch_metric_alarm" "rds_storage" {
  alarm_name          = "${var.name}-rds-free-storage"
  alarm_description   = "RDS free storage below 2 GB."
  namespace           = "AWS/RDS"
  metric_name         = "FreeStorageSpace"
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 2
  threshold           = 2147483648 # 2 GiB
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]

  dimensions = {
    DBInstanceIdentifier = var.db_identifier
  }
}

resource "aws_cloudwatch_metric_alarm" "rds_cpu" {
  alarm_name          = "${var.name}-rds-cpu"
  alarm_description   = "RDS CPU sustained above 80%."
  namespace           = "AWS/RDS"
  metric_name         = "CPUUtilization"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 3
  threshold           = 80
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]

  dimensions = {
    DBInstanceIdentifier = var.db_identifier
  }
}

# ── Cost guardrail ────────────────────────────────────────────────────────────
# A personal account's most likely incident is a surprise bill (a Bedrock loop,
# a forgotten resource the agent applied). Forecast AND actual, to the same
# address as the operational alarms.
resource "aws_budgets_budget" "monthly" {
  count        = var.monthly_budget_usd > 0 && var.alarm_email != "" ? 1 : 0
  name         = "${var.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alarm_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alarm_email]
  }
}
