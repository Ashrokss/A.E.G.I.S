# GuardDuty: continuous threat detection on VPC flow logs, DNS and CloudTrail.
#
# Off by default. It is account-wide, not VPC-scoped, so turning it on in two
# environments that share an AWS account is one detector, not two -- the second
# apply fails with a detector-already-exists error. Enable it in ONE
# environment per account, which in practice means prod.
#
# Cost scales with analysed volume rather than a standing fee: single-digit
# dollars for a quiet account, more once the scanners are busy. Findings land
# on the same SNS topic as the CloudWatch alarms, so there is one place to look.

resource "aws_guardduty_detector" "main" {
  count = var.enabled ? 1 : 0

  enable                       = true
  finding_publishing_frequency = var.finding_publishing_frequency

  tags = { Name = var.name }
}

# Only findings that warrant waking someone. GuardDuty severity is 1-8.9;
# 7.0 and up is its HIGH band. Below that is a dashboard item, and paging on
# LOW findings is how a pager gets ignored.
#
# Gated on var.enabled ALONE. An earlier version also required a non-empty
# alarm_topic_arn, which cannot work: the topic ARN is known only after apply,
# so it cannot decide a count. modules/cloudwatch always creates the topic --
# a blank alarm_email means no SUBSCRIPTION, not no topic -- so there is
# nothing to guard against.
resource "aws_cloudwatch_event_rule" "findings" {
  count = var.enabled ? 1 : 0

  name        = "${var.name}-guardduty-findings"
  description = "GuardDuty findings at or above severity ${var.min_severity}."

  event_pattern = jsonencode({
    source      = ["aws.guardduty"]
    detail-type = ["GuardDuty Finding"]
    detail = {
      severity = [{ numeric = [">=", var.min_severity] }]
    }
  })
}

resource "aws_cloudwatch_event_target" "findings_to_sns" {
  count = var.enabled ? 1 : 0

  rule      = aws_cloudwatch_event_rule.findings[0].name
  target_id = "sns"
  arn       = var.alarm_topic_arn
}
