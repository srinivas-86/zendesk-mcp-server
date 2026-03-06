variable "region" {
  type    = string
  default = "us-east-1"
}

variable "domain" {
  description = "Public DNS name (e.g. mcp.example.com)"
  type        = string
}

variable "route53_zone_id" {
  description = "Route53 hosted zone for the domain"
  type        = string
}

variable "certificate_arn" {
  description = "ACM certificate ARN for the domain (must be in the same region)"
  type        = string
}

variable "secrets_arn" {
  description = "Secrets Manager secret ARN holding a JSON object with ZENDESK_SUBDOMAIN, ZENDESK_API_KEY, ZENDESK_EMAIL, ZENDESK_READ_EMAIL, ZENDESK_WRITE_EMAIL, MCP_ADMIN_PASSWORD"
  type        = string
}

variable "image_tag" {
  description = "Image tag in ECR to deploy (e.g. git SHA)"
  type        = string
  default     = "latest"
}

variable "cpu" {
  type    = string
  default = "256"
}

variable "memory" {
  type    = string
  default = "512"
}

variable "desired_count" {
  description = "Task count. Keep 1 while the key store is SQLite-on-EFS; migrate keys to DynamoDB before scaling out."
  type        = number
  default     = 1
}

variable "auth_mode" {
  description = "keys | oauth | both"
  type        = string
  default     = "keys"
}

variable "oauth_issuer" {
  description = "OIDC issuer URL (required for auth_mode oauth|both)"
  type        = string
  default     = ""
}

variable "oauth_audience" {
  description = "OAuth audience (defaults to MCP_PUBLIC_URL in-app when empty)"
  type        = string
  default     = ""
}
