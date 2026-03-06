# Zendesk MCP Server — ECS Fargate + ALB scale-out deployment (plan §5.3)
#
# Prereqs: an ACM certificate for var.domain (DNS-validated) and a Route53 zone.
# Build & push the image first:
#   aws ecr get-login-password | docker login --username AWS --password-stdin <acct>.dkr.ecr.<region>.amazonaws.com
#   docker build -t zendesk-mcp-server . && docker tag ... && docker push ...
#
# Notes:
# - The SQLite key store lives on EFS so keys survive task restarts.
#   Run desired_count=1 with SQLite; for >1 tasks migrate the key store to
#   DynamoDB (roadmap) or accept eventual key-DB divergence.
# - Zendesk credentials come from Secrets Manager (var.secrets_arn expected
#   to hold a JSON object with the ZENDESK_* / MCP_* values).

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}

provider "aws" {
  region = var.region
}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

# --- ECR ---

resource "aws_ecr_repository" "mcp" {
  name                 = "zendesk-mcp-server"
  image_scanning_configuration { scan_on_push = true }
  force_delete = true
}

# --- Logs ---

resource "aws_cloudwatch_log_group" "mcp" {
  name              = "/ecs/zendesk-mcp-server"
  retention_in_days = 30
}

# --- Security groups ---

resource "aws_security_group" "alb" {
  name_prefix = "zendesk-mcp-alb-"
  vpc_id      = data.aws_vpc.default.id
  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "task" {
  name_prefix = "zendesk-mcp-task-"
  vpc_id      = data.aws_vpc.default.id
  ingress {
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "efs" {
  name_prefix = "zendesk-mcp-efs-"
  vpc_id      = data.aws_vpc.default.id
  ingress {
    from_port       = 2049
    to_port         = 2049
    protocol        = "tcp"
    security_groups = [aws_security_group.task.id]
  }
}

# --- EFS for the key store ---

resource "aws_efs_file_system" "keys" {
  encrypted = true
  tags      = { Name = "zendesk-mcp-keys" }
}

resource "aws_efs_mount_target" "keys" {
  for_each        = toset(data.aws_subnets.default.ids)
  file_system_id  = aws_efs_file_system.keys.id
  subnet_id       = each.value
  security_groups = [aws_security_group.efs.id]
}

resource "aws_efs_access_point" "keys" {
  file_system_id = aws_efs_file_system.keys.id
  posix_user {
    uid = 999
    gid = 999
  }
  root_directory {
    path = "/keys"
    creation_info {
      owner_uid   = 999
      owner_gid   = 999
      permissions = "750"
    }
  }
}

# --- ALB ---

resource "aws_lb" "mcp" {
  name               = "zendesk-mcp"
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = data.aws_subnets.default.ids
  idle_timeout       = 300 # long-lived SSE streams
}

resource "aws_lb_target_group" "mcp" {
  name        = "zendesk-mcp"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = data.aws_vpc.default.id
  target_type = "ip"
  health_check {
    path                = "/health"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    interval            = 30
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.mcp.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.mcp.arn
  }
}

resource "aws_lb_listener" "http_redirect" {
  load_balancer_arn = aws_lb.mcp.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

# --- ECS ---

resource "aws_ecs_cluster" "mcp" {
  name = "zendesk-mcp"
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_iam_role" "execution" {
  name_prefix = "zendesk-mcp-exec-"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "secrets" {
  name_prefix = "secrets-"
  role        = aws_iam_role.execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action   = ["secretsmanager:GetSecretValue"]
      Effect   = "Allow"
      Resource = [var.secrets_arn]
    }]
  })
}

resource "aws_iam_role" "task" {
  name_prefix = "zendesk-mcp-task-"
  assume_role_policy = aws_iam_role.execution.assume_role_policy
}

locals {
  secret_keys = [
    "ZENDESK_SUBDOMAIN", "ZENDESK_API_KEY", "ZENDESK_EMAIL",
    "ZENDESK_READ_EMAIL", "ZENDESK_WRITE_EMAIL", "MCP_ADMIN_PASSWORD",
  ]
}

resource "aws_ecs_task_definition" "mcp" {
  family                   = "zendesk-mcp-server"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.cpu
  memory                   = var.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  volume {
    name = "keys"
    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.keys.id
      transit_encryption = "ENABLED"
      authorization_config {
        access_point_id = aws_efs_access_point.keys.id
        iam             = "DISABLED"
      }
    }
  }

  container_definitions = jsonencode([{
    name      = "zendesk-mcp"
    image     = "${aws_ecr_repository.mcp.repository_url}:${var.image_tag}"
    essential = true
    portMappings = [{ containerPort = 8000, protocol = "tcp" }]
    environment = [
      { name = "MCP_TRANSPORT", value = "http" },
      { name = "MCP_HOST", value = "0.0.0.0" },
      { name = "MCP_PORT", value = "8000" },
      { name = "MCP_KEYS_DB", value = "/data/keys.db" },
      { name = "MCP_PUBLIC_URL", value = "https://${var.domain}" },
      { name = "MCP_AUTH_MODE", value = var.auth_mode },
      { name = "MCP_OAUTH_ISSUER", value = var.oauth_issuer },
      { name = "MCP_OAUTH_AUDIENCE", value = var.oauth_audience },
    ]
    secrets = [
      for k in local.secret_keys : {
        name      = k
        valueFrom = "${var.secrets_arn}:${k}::"
      }
    ]
    mountPoints = [{ sourceVolume = "keys", containerPath = "/data" }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.mcp.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "mcp"
      }
    }
  }])
}

resource "aws_ecs_service" "mcp" {
  name            = "zendesk-mcp"
  cluster         = aws_ecs_cluster.mcp.id
  task_definition = aws_ecs_task_definition.mcp.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = data.aws_subnets.default.ids
    security_groups  = [aws_security_group.task.id]
    assign_public_ip = true # default VPC; use private subnets + NAT in a hardened VPC
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.mcp.arn
    container_name   = "zendesk-mcp"
    container_port   = 8000
  }

  enable_execute_command = true # `aws ecs execute-command` for zendesk-keys CLI / admin tunnel
}

# --- DNS ---

resource "aws_route53_record" "mcp" {
  zone_id = var.route53_zone_id
  name    = var.domain
  type    = "A"
  alias {
    name                   = aws_lb.mcp.dns_name
    zone_id                = aws_lb.mcp.zone_id
    evaluate_target_health = true
  }
}
