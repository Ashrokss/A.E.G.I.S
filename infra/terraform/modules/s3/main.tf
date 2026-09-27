# The provisioner-role CloudFormation template. Quick-create links must point at
# an S3 URL the CloudFormation console can fetch, so ONE prefix (templates/) is
# public-read. Nothing else is ever written to this bucket.

resource "aws_s3_bucket" "templates" {
  bucket = "${var.name}-templates-${var.account_id}"
}

#checkov:skip=CKV_AWS_56:templates/ is deliberately public-read for CloudFormation quick-create
resource "aws_s3_bucket_public_access_block" "templates" {
  bucket                  = aws_s3_bucket.templates.id
  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = false
  restrict_public_buckets = false
}

resource "aws_s3_bucket_server_side_encryption_configuration" "templates" {
  bucket = aws_s3_bucket.templates.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "templates" {
  bucket = aws_s3_bucket.templates.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_policy" "templates" {
  bucket     = aws_s3_bucket.templates.id
  depends_on = [aws_s3_bucket_public_access_block.templates]
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "PublicReadTemplatesOnly"
      Effect    = "Allow"
      Principal = "*"
      Action    = "s3:GetObject"
      Resource  = "${aws_s3_bucket.templates.arn}/templates/*"
      }, {
      Sid       = "TlsOnly"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.templates.arn, "${aws_s3_bucket.templates.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

# Uploaded by Terraform, so the live template is always the committed one.
resource "aws_s3_object" "provisioner_template" {
  bucket       = aws_s3_bucket.templates.id
  key          = "templates/aegis-provisioner-role.yaml"
  source       = var.provisioner_template_path
  etag         = filemd5(var.provisioner_template_path)
  content_type = "application/x-yaml"
}
