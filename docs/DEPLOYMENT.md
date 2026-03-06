# Deploying the Zendesk MCP Server on AWS EC2

End-to-end guide: Terraform provisioning → configuration → key issuance → client connection.

## 1. Provision

```bash
cd terraform
terraform init
terraform apply \
  -var domain=mcp.example.com \
  -var repo_url=https://github.com/you/zendesk-mcp-server.git \
  -var region=us-east-1
  # optional: -var route53_zone_id=Z0123456789
```

Creates: EC2 (Amazon Linux 2023, t3.small, encrypted gp3, IMDSv2-only), security group
(80/443 only — **no SSH**, no admin port), Elastic IP, SSM instance role, optional Route53 A record.
The instance bootstraps Docker + compose and clones the repo to `/opt/zendesk-mcp`.

If you didn't use `route53_zone_id`, point an A record for your domain at the `public_ip` output.

## 2. Configure

Connect without SSH keys:

```bash
aws ssm start-session --target $(terraform output -raw instance_id)
```

On the instance:

```bash
cd /opt/zendesk-mcp
sudo cp .env.bootstrap .env
sudo vi .env       # fill in:
#   ZENDESK_SUBDOMAIN, ZENDESK_API_KEY
#   ZENDESK_READ_EMAIL / ZENDESK_WRITE_EMAIL   (Layer 4 dual identity)
#   MCP_ADMIN_PASSWORD                          (enables admin console)
#   MCP_WRITE_CONFIRMATION=true                 (optional Layer 3)
sudo docker compose up -d
```

Caddy obtains a Let's Encrypt certificate automatically for `MCP_DOMAIN`.
Verify: `curl https://mcp.example.com/health` → `{"status": "ok", ...}`.

## 3. Issue API keys

Via CLI on the instance:

```bash
sudo docker compose exec zendesk-mcp zendesk-keys create --name admin --scopes "*"
sudo docker compose exec zendesk-mcp zendesk-keys create --name reader --scopes tickets:read,kb:read
```

Or via the web admin console (connection settings + keys + revocation), tunneled — never public:

```bash
# from your laptop:
aws ssm start-session --target <instance_id> \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["9000"],"localPortNumber":["9000"]}'
# then open http://localhost:9000 and log in with MCP_ADMIN_PASSWORD
```

## 4. Connect clients

Claude Code:

```bash
claude mcp add zendesk --transport http https://mcp.example.com/mcp \
  --header "Authorization: Bearer zmk_..."
```

Claude Desktop / other MCP clients: add a remote HTTP server with URL
`https://mcp.example.com/mcp` and header `Authorization: Bearer zmk_...`.

## 5. Operations

- **Logs:** `sudo docker compose logs -f zendesk-mcp`
- **Update:** `git pull && sudo docker compose up -d --build`
- **Revoke a key:** admin console, or `zendesk-keys revoke --id N` (effective immediately)
- **Key DB backup:** volume `keys-data` (`/data/keys.db` in-container)
- **Health:** `/health` (used by the container HEALTHCHECK; wire to CloudWatch/uptime checks)

## Security model recap

| Layer | Mechanism |
|---|---|
| Edge | Caddy TLS 1.2+, security headers; SG allows only 80/443 |
| AuthN | Bearer API keys (`zmk_`, SHA-256 hashed at rest, expiry, instant revocation) |
| AuthZ (L1) | Per-tool scope enforcement, fail-closed |
| Visibility (L2) | tools/list filtered to the caller's scopes |
| Confirmation (L3) | `destructiveHint` annotations + optional elicitation for public comments |
| Zendesk (L4) | Dual identity: reads via restricted user, writes via full agent |
| Admin | Separate port, loopback-published only, password + CSRF, token never re-displayed |
| Audit | Append-only log of key lifecycle, admin actions, denied/allowed writes |

## Scale-out path (later)

Move the container to ECS Fargate behind an ALB (ACM cert, WAF), externalize the key store
to DynamoDB, and enable FastMCP stateless mode — the plan doc §5.3 covers this.
