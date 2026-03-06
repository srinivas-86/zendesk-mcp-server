output "ecr_repository_url" {
  value = aws_ecr_repository.mcp.repository_url
}

output "alb_dns_name" {
  value = aws_lb.mcp.dns_name
}

output "mcp_url" {
  value = "https://${var.domain}/mcp"
}

output "keys_cli_command" {
  description = "Manage API keys inside the running task"
  value       = "aws ecs execute-command --cluster zendesk-mcp --task <task-id> --container zendesk-mcp --interactive --command 'zendesk-keys list'"
}
