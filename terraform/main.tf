# Zendesk MCP Server — single-instance EC2 deployment (plan §5)
#
#   terraform init
#   terraform apply -var domain=mcp.example.com -var repo_url=https://github.com/you/zendesk-mcp-server.git
#
# After apply: point your DNS A record at the output `public_ip` (or set
# route53_zone_id to have it created), SSM into the instance, create .env,
# and run `docker compose up -d`. See docs/DEPLOYMENT.md.

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.region
}

# --- Networking (default VPC keeps this minimal; move to a private VPC + ALB for scale-out) ---

data "aws_vpc" "default" {
  default = true
}

data "aws_ami" "al2023" {
  most_recent = true
  owners      = ["amazon"]
  filter {
    name   = "name"
    values = ["al2023-ami-*-x86_64"]
  }
}

resource "aws_security_group" "mcp" {
  name_prefix = "zendesk-mcp-"
  description = "Zendesk MCP server - HTTPS only. No SSH (use SSM). Admin console is NOT exposed."
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "HTTP (Caddy ACME challenge + redirect to HTTPS)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "HTTPS (MCP endpoint via Caddy)"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  lifecycle {
    create_before_destroy = true
  }
}

# --- IAM: SSM access (no SSH keys) + optional Secrets Manager read ---

resource "aws_iam_role" "mcp" {
  name_prefix = "zendesk-mcp-"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.mcp.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "mcp" {
  name_prefix = "zendesk-mcp-"
  role        = aws_iam_role.mcp.name
}

# --- EC2 instance ---

resource "aws_instance" "mcp" {
  ami                    = data.aws_ami.al2023.id
  instance_type          = var.instance_type
  vpc_security_group_ids = [aws_security_group.mcp.id]
  iam_instance_profile   = aws_iam_instance_profile.mcp.name

  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    repo_url = var.repo_url
    domain   = var.domain
  })

  root_block_device {
    volume_size = 20
    volume_type = "gp3"
    encrypted   = true
  }

  metadata_options {
    http_tokens = "required" # IMDSv2 only
  }

  tags = {
    Name = "zendesk-mcp-server"
  }
}

resource "aws_eip" "mcp" {
  instance = aws_instance.mcp.id
  domain   = "vpc"
  tags     = { Name = "zendesk-mcp-server" }
}

# --- Optional DNS record ---

resource "aws_route53_record" "mcp" {
  count   = var.route53_zone_id != "" ? 1 : 0
  zone_id = var.route53_zone_id
  name    = var.domain
  type    = "A"
  ttl     = 300
  records = [aws_eip.mcp.public_ip]
}
