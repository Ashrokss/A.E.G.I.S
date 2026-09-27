# ── ECR ───────────────────────────────────────────────────────────────────────

resource "aws_ecr_repository" "app" {
  for_each = toset(var.ecr_repositories)

  name                 = "${var.name}/${each.key}"
  image_tag_mutability = "IMMUTABLE" # tags are git SHAs; a tag always means one image

  image_scanning_configuration {
    scan_on_push = true
  }
}

# Untagged layers accumulate fast with four images and a rebuild per deploy.
resource "aws_ecr_lifecycle_policy" "app" {
  for_each   = aws_ecr_repository.app
  repository = each.value.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 30 most recent images (rollback depth)"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 30
      }
      action = { type = "expire" }
    }]
  })
}
