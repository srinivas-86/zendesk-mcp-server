variable "region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "instance_type" {
  description = "EC2 instance type (t3.small is sufficient for an I/O-bound MCP server)"
  type        = string
  default     = "t3.small"
}

variable "domain" {
  description = "Public DNS name for the MCP server (e.g. mcp.example.com). Caddy uses it for automatic TLS."
  type        = string
}

variable "repo_url" {
  description = "Git repository URL to clone on the instance"
  type        = string
}

variable "route53_zone_id" {
  description = "Optional Route53 hosted zone ID to create the A record in. Leave empty to manage DNS manually."
  type        = string
  default     = ""
}
