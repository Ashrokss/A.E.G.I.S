resource "aws_vpc" "main" {
  cidr_block = var.vpc_cidr
  # Both required for VPC endpoint private DNS and the RDS endpoint name.
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = { Name = "vpc-${var.name}" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "igw-${var.name}" }
}

# ── Subnets ───────────────────────────────────────────────────────────────────

resource "aws_subnet" "public" {
  count                   = 2
  vpc_id                  = aws_vpc.main.id
  cidr_block              = var.public_subnet_cidrs[count.index]
  availability_zone       = var.availability_zones[count.index]
  map_public_ip_on_launch = false # the ALB and NAT carry their own addresses

  tags = { Name = "sn-public-${count.index}", Tier = "public" }
}

resource "aws_subnet" "app" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  cidr_block        = var.app_subnet_cidrs[count.index]
  availability_zone = var.availability_zones[count.index]

  tags = { Name = "sn-app-${count.index}", Tier = "app" }
}

resource "aws_subnet" "data" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  cidr_block        = var.data_subnet_cidrs[count.index]
  availability_zone = var.availability_zones[count.index]

  tags = { Name = "sn-data-${count.index}", Tier = "data" }
}

# ── The one egress path ───────────────────────────────────────────────────────

# ── Egress: NAT gateway, or the internet gateway directly ────────────────────
# With enable_nat_gateway = false the app subnets route to the IGW and the task
# takes a public IP. That removes the NAT standing charge and its per-GB rate,
# the single biggest line on a demo deployment after compute.
#
# What it costs you: the task ENI has a public address. sg-app still allows
# ingress ONLY from the load balancer, so nothing is reachable -- but "no public
# IP at all" is a stronger position, and the architecture deck claims it.
resource "aws_eip" "nat" {
  count  = var.enable_nat_gateway ? 1 : 0
  domain = "vpc"
  tags   = { Name = "eip-nat-${var.name}" }
}

resource "aws_nat_gateway" "main" {
  count         = var.enable_nat_gateway ? 1 : 0
  allocation_id = aws_eip.nat[0].id
  subnet_id     = aws_subnet.public[0].id
  depends_on    = [aws_internet_gateway.main]

  tags = { Name = "nat-${var.name}" }
}

# ── Route tables ──────────────────────────────────────────────────────────────
# Three tables, and the whole egress story is readable from them: only the
# PUBLIC table has an IGW route. The app tier reaches the internet through the
# NAT or not at all, and the data tier not at all.

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
  tags = { Name = "rt-public-${var.name}" }
}

resource "aws_route_table" "app" {
  vpc_id = aws_vpc.main.id

  dynamic "route" {
    for_each = var.enable_nat_gateway ? [1] : []
    content {
      cidr_block     = "0.0.0.0/0"
      nat_gateway_id = aws_nat_gateway.main[0].id
    }
  }

  # No NAT: the app tier reaches AWS APIs through the internet gateway instead.
  dynamic "route" {
    for_each = var.enable_nat_gateway ? [] : [1]
    content {
      cidr_block = "0.0.0.0/0"
      gateway_id = aws_internet_gateway.main.id
    }
  }

  tags = { Name = "rt-app-${var.name}" }
}

# No default route at all. RDS needs no egress, and the absence is the control.
resource "aws_route_table" "data" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "rt-data-${var.name}" }
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table_association" "app" {
  count          = 2
  subnet_id      = aws_subnet.app[count.index].id
  route_table_id = aws_route_table.app.id
}

resource "aws_route_table_association" "data" {
  count          = 2
  subnet_id      = aws_subnet.data[count.index].id
  route_table_id = aws_route_table.data.id
}

# ── Security groups ───────────────────────────────────────────────────────────
# Each has exactly the rules named and nothing else. Every internal hop is by
# security-group id, never a CIDR.

# CloudFront's origin-facing address ranges. With a VPC origin the ALB is
# INTERNAL — no public IP, no internet route to it — and this prefix list is the
# only source allowed in.
data "aws_ec2_managed_prefix_list" "cloudfront" {
  name = "com.amazonaws.global.cloudfront.origin-facing"
}

resource "aws_security_group" "alb" {
  name        = "${var.name}-alb"
  description = "Internal ALB. Reachable only from CloudFront (VPC origin)."
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "sg-alb-${var.name}" }
}

resource "aws_vpc_security_group_ingress_rule" "alb_from_cloudfront" {
  security_group_id = aws_security_group.alb.id
  description       = "HTTP from CloudFront origin-facing ranges only"
  prefix_list_id    = data.aws_ec2_managed_prefix_list.cloudfront.id
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "alb_to_app" {
  for_each                     = toset(["3005", "8000"])
  security_group_id            = aws_security_group.alb.id
  description                  = "To the api (3005) and agent (8000) containers"
  referenced_security_group_id = aws_security_group.app.id
  from_port                    = tonumber(each.key)
  to_port                      = tonumber(each.key)
  ip_protocol                  = "tcp"
}

resource "aws_security_group" "app" {
  name        = "${var.name}-app"
  description = "Fargate task. Ingress from the ALB only."
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "sg-app-${var.name}" }
}

resource "aws_vpc_security_group_ingress_rule" "app_from_alb" {
  for_each                     = toset(["3005", "8000"])
  security_group_id            = aws_security_group.app.id
  description                  = "api / agent, from the load balancer only"
  referenced_security_group_id = aws_security_group.alb.id
  from_port                    = tonumber(each.key)
  to_port                      = tonumber(each.key)
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "app_https" {
  security_group_id = aws_security_group.app.id
  description       = "AWS APIs, Bedrock, Terraform registry, GitHub, Jira, Tavily"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "app_to_db" {
  security_group_id            = aws_security_group.app.id
  referenced_security_group_id = aws_security_group.db.id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "app_to_efs" {
  security_group_id            = aws_security_group.app.id
  referenced_security_group_id = aws_security_group.efs.id
  from_port                    = 2049
  to_port                      = 2049
  ip_protocol                  = "tcp"
}

resource "aws_security_group" "db" {
  name        = "${var.name}-db"
  description = "RDS. Reachable from the task and nothing else."
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "sg-db-${var.name}" }
}

# No CIDR ingress here, ever. Not for a laptop, not for a bastion.
resource "aws_vpc_security_group_ingress_rule" "db_from_app" {
  security_group_id            = aws_security_group.db.id
  description                  = "PostgreSQL from the Fargate task only"
  referenced_security_group_id = aws_security_group.app.id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}

resource "aws_security_group" "efs" {
  name        = "${var.name}-efs"
  description = "EFS mount targets."
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "sg-efs-${var.name}" }
}

resource "aws_vpc_security_group_ingress_rule" "efs_from_app" {
  security_group_id            = aws_security_group.efs.id
  referenced_security_group_id = aws_security_group.app.id
  from_port                    = 2049
  to_port                      = 2049
  ip_protocol                  = "tcp"
}
