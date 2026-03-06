output "public_ip" {
  description = "Elastic IP — point your DNS A record here if not using route53_zone_id"
  value       = aws_eip.mcp.public_ip
}

output "instance_id" {
  description = "Connect with: aws ssm start-session --target <instance_id>"
  value       = aws_instance.mcp.id
}

output "mcp_url" {
  description = "MCP endpoint clients connect to (after DNS + .env setup)"
  value       = "https://${var.domain}/mcp"
}

output "admin_tunnel_command" {
  description = "Access the admin console securely via SSM port-forward"
  value       = "aws ssm start-session --target ${aws_instance.mcp.id} --document-name AWS-StartPortForwardingSession --parameters '{\"portNumber\":[\"9000\"],\"localPortNumber\":[\"9000\"]}'"
}
