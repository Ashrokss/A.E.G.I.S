# The edge. One distribution, two origins:
#   default                        -> S3 web bucket (the React SPA), private, read via OAC
#   /api/*, /auth/aws/*, /agent-api/* -> the INTERNAL ALB, via a CloudFront VPC origin
#
# The VPC origin is why the ALB needs no public IP and no shared-secret header:
# the only network path to it is from CloudFront itself.

resource "aws_s3_bucket" "web" {
  bucket = "${var.name}-web-${var.account_id}"
}

resource "aws_s3_bucket_public_access_block" "web" {
  bucket                  = aws_s3_bucket.web.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "web" {
  bucket = aws_s3_bucket.web.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "web" {
  bucket = aws_s3_bucket.web.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "web" {
  bucket = aws_s3_bucket.web.id
  rule {
    id     = "expire-old-builds"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

resource "aws_cloudfront_origin_access_control" "web" {
  name                              = "${var.name}-web"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_s3_bucket_policy" "web" {
  bucket = aws_s3_bucket.web.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "CloudFrontReadOnly"
      Effect    = "Allow"
      Principal = { Service = "cloudfront.amazonaws.com" }
      Action    = "s3:GetObject"
      Resource  = "${aws_s3_bucket.web.arn}/*"
      Condition = { StringEquals = { "AWS:SourceArn" = aws_cloudfront_distribution.main.arn } }
      }, {
      Sid       = "TlsOnly"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.web.arn, "${aws_s3_bucket.web.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

resource "aws_cloudfront_vpc_origin" "alb" {
  vpc_origin_endpoint_config {
    name                   = "${var.name}-alb"
    arn                    = var.alb_arn
    http_port              = 80
    https_port             = 443
    origin_protocol_policy = "http-only"

    origin_ssl_protocols {
      items    = ["TLSv1.2"]
      quantity = 1
    }
  }
}

# Client-side routes (/runs/abc, /accounts) have no object in S3. They are
# rewritten to /index.html HERE, on the S3 behaviour only, rather than with a
# distribution-wide custom_error_response: that would also turn every API
# 403/404 into an HTML page.
resource "aws_cloudfront_function" "spa" {
  name    = "${var.name}-spa-routing"
  runtime = "cloudfront-js-2.0"
  publish = true
  code    = file("${path.module}/spa-routing.js")
}

resource "aws_wafv2_web_acl" "main" {
  count = var.enable_waf ? 1 : 0
  name  = "waf-${var.name}"
  scope = "CLOUDFRONT"

  default_action {
    allow {}
  }

  dynamic "rule" {
    for_each = {
      AWSManagedRulesCommonRuleSet          = 1
      AWSManagedRulesKnownBadInputsRuleSet  = 2
      AWSManagedRulesAmazonIpReputationList = 3
    }
    content {
      name     = rule.key
      priority = rule.value

      override_action {
        count {} # promote to none{} after a clean week
      }

      statement {
        managed_rule_group_statement {
          vendor_name = "AWS"
          name        = rule.key
        }
      }

      visibility_config {
        cloudwatch_metrics_enabled = true
        metric_name                = rule.key
        sampled_requests_enabled   = true
      }
    }
  }

  # This one DOES block from day one: it is volumetric, not signature-based, so
  # it cannot misfire on a large upload.
  rule {
    name     = "RateLimit"
    priority = 10

    action {
      block {}
    }

    statement {
      rate_based_statement {
        limit              = var.waf_rate_limit
        aggregate_key_type = "IP"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "RateLimit"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "waf-${var.name}"
    sampled_requests_enabled   = true
  }
}

# ── CloudFront ────────────────────────────────────────────────────────────────
# The SPA lives inside the nginx image, not in S3, so the origin is the ALB --
# a custom origin, not a bucket.

locals {
  # AWS managed policy ids (stable, documented).
  caching_optimized       = "658327ea-f89d-4fab-a63d-7e88639e58f6"
  caching_disabled        = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
  all_viewer_except_host  = "b689b0a8-53d0-40ab-baf2-68738e2966ac"
  security_headers_policy = "67f7725c-6f97-4210-82d7-5512b31e9d03"
  api_paths               = ["/api/*", "/auth/aws/*", "/agent-api/*"]
}

resource "aws_cloudfront_distribution" "main" {
  enabled             = true
  is_ipv6_enabled     = true
  comment             = var.name
  default_root_object = "index.html"
  web_acl_id          = var.enable_waf ? aws_wafv2_web_acl.main[0].arn : null
  price_class         = var.cloudfront_price_class

  origin {
    origin_id                = "web"
    domain_name              = aws_s3_bucket.web.bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.web.id
  }

  origin {
    origin_id   = "alb"
    domain_name = var.alb_dns_name
    vpc_origin_config {
      vpc_origin_id = aws_cloudfront_vpc_origin.alb.id
      # Per-packet, not per-request: the SSE log stream sends a keep-alive well
      # inside this (sse-starlette ping), so long plans stay connected.
      origin_read_timeout      = 60
      origin_keepalive_timeout = 60
    }
  }

  dynamic "ordered_cache_behavior" {
    for_each = local.api_paths
    content {
      path_pattern             = ordered_cache_behavior.value
      target_origin_id         = "alb"
      viewer_protocol_policy   = "https-only"
      allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
      cached_methods           = ["GET", "HEAD"]
      compress                 = true
      cache_policy_id          = local.caching_disabled
      origin_request_policy_id = local.all_viewer_except_host
    }
  }

  default_cache_behavior {
    target_origin_id           = "web"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD", "OPTIONS"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = true
    cache_policy_id            = local.caching_optimized
    response_headers_policy_id = local.security_headers_policy

    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.spa.arn
    }
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  # No custom domain yet, so the distribution uses the CloudFront default
  # certificate. Add an ACM cert (us-east-1) and aliases here once a domain
  # exists; see docs/DEPLOYMENT.md.
  viewer_certificate {
    cloudfront_default_certificate = true
  }
}
