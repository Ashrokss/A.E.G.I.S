# Internal ALB. Nothing on the internet can reach it: CloudFront connects via a
# VPC origin, and the security group admits only CloudFront's origin-facing
# prefix list (modules/networking). No public IP, no origin-verify secret.
#
# Path routing replaces the old nginx sidecar:
#   /api/*/internal/*  -> 404   (agent -> api credential lookups stay on localhost)
#   /agent-api/*       -> agent (the agent maps the prefix onto its /api routes)
#   everything else    -> api   (/api/*, /auth/*, /health)

resource "aws_lb" "main" {
  name                       = "${var.name}-alb"
  internal                   = true
  load_balancer_type         = "application"
  security_groups            = [var.alb_security_group_id]
  subnets                    = var.subnet_ids
  drop_invalid_header_fields = true
  # Long enough for an SSE log stream between keep-alive pings, and for a
  # slow Terraform plan to answer.
  idle_timeout = 300
}

locals {
  targets = {
    api   = { port = 3005 }
    agent = { port = 8000 }
  }
}

resource "aws_lb_target_group" "app" {
  for_each             = local.targets
  name                 = "${var.name}-${each.key}"
  port                 = each.value.port
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = var.vpc_id
  deregistration_delay = 30

  health_check {
    path     = "/health"
    interval = 30
    timeout  = 5
    # 503 counts as alive for the api: its /health reports 503 while Postgres is
    # unreachable, and replacing the task would not fix the database — it would
    # only add a restart loop on top. The alarm on "PostgreSQL unreachable"
    # (modules/cloudwatch) is what reports that condition.
    matcher             = each.key == "api" ? "200,503" : "200"
    healthy_threshold   = 2
    unhealthy_threshold = 5
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP" # CloudFront -> VPC origin stays on the AWS network

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app["api"].arn
  }
}

resource "aws_lb_listener_rule" "deny_internal" {
  listener_arn = aws_lb_listener.http.arn
  priority     = 10

  condition {
    path_pattern {
      values = ["/api/*/internal/*", "/agent-api/*/internal/*"]
    }
  }

  action {
    type = "fixed-response"
    fixed_response {
      content_type = "application/json"
      status_code  = "404"
      message_body = "{\"error\":\"Not found\"}"
    }
  }
}

resource "aws_lb_listener_rule" "agent" {
  listener_arn = aws_lb_listener.http.arn
  priority     = 20

  condition {
    path_pattern {
      values = ["/agent-api/*"]
    }
  }

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app["agent"].arn
  }
}
