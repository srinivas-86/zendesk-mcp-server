# Zendesk MCP Server (Extended Edition)

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
![version](https://img.shields.io/badge/version-0.3.0-green.svg)

A production-grade Model Context Protocol (MCP) server for Zendesk — run it locally
with Claude Desktop over stdio, or deploy it to the cloud as a secure, multi-tenant,
OAuth-protected remote MCP server that any AI application can connect to.

> **Note — extension of the original project**
>
> This is an extended fork of [reminia/zendesk-mcp-server](https://github.com/reminia/zendesk-mcp-server).
> The original provides a local, stdio-only Zendesk MCP server with basic ticket
> tools. This edition keeps full backward compatibility with it (stdio mode,
> original tools, prompts, and knowledge-base resource) and extends it for
> remote, internet-facing production use.

## Issues with the original repo that this edition addresses

| # | Issue in original | How it's addressed here |
|---|---|---|
| 1 | **stdio transport only** — could not be reached over a network, so it only worked on the same machine as the AI client | Streamable HTTP transport (`/mcp` endpoint, MCP spec 2025-03-26+; SSE-as-transport is deprecated and intentionally not used) alongside stdio |
| 2 | **No authentication of any kind** — anyone who could reach the process could use it | Three auth modes: internal scoped API keys, OAuth 2.1 resource server (generic OIDC + RFC 9728 discovery), or both simultaneously |
| 3 | **No permission model** — every caller could read *and* write tickets | 4-layer read/write control: per-tool scopes (fail-closed), tool-list filtering, human-in-the-loop confirmation, dual Zendesk identity backstop |
| 4 | **Blocking I/O inside async handlers** — sync Zenpy calls froze the event loop under concurrent HTTP load | All Zendesk calls run in worker threads (`run_in_thread`) |
| 5 | **Deprecated offset pagination** — Zendesk is sunsetting it; comments were not paginated at all (context blowout on long tickets) | Cursor pagination (`page[size]`/`page[after]`) for tickets and comments |
| 6 | **Sparse Zendesk coverage** — only 5 tools; user/assignee IDs could not be resolved, custom fields were opaque, no search | 14 tools including search, users, groups, ticket-field metadata, KB article search, attachment upload |
| 7 | **Single Zendesk account hard-wired at startup** from `.env` | Multi-tenancy: per-tenant Zendesk credentials, keys/OAuth claims mapped to tenants, hot-swappable connection settings |
| 8 | **No deployment story** — no TLS, no health check, stdio-oriented Docker image | Docker (HTTP-first, healthcheck), docker-compose with Caddy auto-TLS, Terraform for EC2 and for ECS Fargate + ALB |
| 9 | **No admin tooling** — key rotation/credential changes required editing `.env` and restarting | Web admin console (separate port) + `zendesk-keys` CLI; connection hot-swap without restart |
| 10 | **No tests** | 38 unit tests + HTTP/admin/OAuth smoke test suites; tests run in CI |

## Feature overview

**Original features (retained):**

- Ticket tools: get ticket, list tickets, get comments, create ticket, update ticket, comment on ticket
- Image attachment download with security hardening (MIME allowlist, magic-byte validation, 10 MB cap)
- Prompts: `analyze-ticket`, `draft-ticket-response`
- Resource: `zendesk://knowledge-base` (all Help Center articles, cached 1 h)
- stdio transport for Claude Desktop / Claude Code local use

**New in this edition:**

- Streamable HTTP transport with `/health` endpoint
- Internal API keys: `zmk_` prefix, SHA-256 hashed at rest, scopes, expiry, instant revocation, audit log
- OAuth 2.1 resource server: JWKS/issuer/audience JWT validation, RFC 9728 Protected Resource
  Metadata at `/.well-known/oauth-protected-resource/mcp` — works with Auth0, Descope, Cognito, Keycloak, WorkOS
- Scope model: `tickets:read`, `tickets:write`, `kb:read`, `*` — enforced per tool, fail-closed, with tools/list filtering
- Optional elicitation: in-client approve/decline before posting public (customer-visible) comments
- Dual Zendesk identity: reads via a restricted user (e.g. light agent), writes via a full agent
- Multi-tenancy with per-tenant Zendesk credentials
- Web admin console: connection settings (hot-swap + test), tenant management, key lifecycle
- New tools: `search_tickets`, `get_user`, `search_users`, `list_groups`, `list_ticket_fields`,
  `search_articles`, `upload_attachment`
- Tool annotations (`readOnlyHint` / `destructiveHint`) and structured output on all tools
- Deployment: Dockerfile, docker-compose + Caddy (auto-TLS), Terraform for EC2 and ECS Fargate
- MCP Registry manifest (`server.json`) and publishing guide

## Tools

| Tool | Scope | Description |
|---|---|---|
| `get_ticket` | tickets:read | Get a ticket by ID |
| `get_tickets` | tickets:read | List tickets (cursor pagination, sortable) |
| `get_ticket_comments` | tickets:read | Ticket comments incl. attachment metadata (cursor pagination) |
| `search_tickets` | tickets:read | Zendesk search syntax, e.g. `status:open priority:high` |
| `get_ticket_attachment` | tickets:read | Download an image attachment (base64, validated) |
| `get_user` | tickets:read | Resolve a user ID to name/email/role |
| `search_users` | tickets:read | Search users by name or email |
| `list_groups` | tickets:read | Agent groups (teams) for routing |
| `list_ticket_fields` | tickets:read | Field metadata — interpret/set `custom_fields` `{id, value}` |
| `search_articles` | kb:read | Search Help Center articles (context-safe) |
| `create_ticket` | tickets:write | Create a ticket |
| `update_ticket` | tickets:write | Update status/priority/assignee/tags/custom fields/due date |
| `create_ticket_comment` | tickets:write | Comment on a ticket (optional elicitation for public comments; supports attachments) |
| `upload_attachment` | tickets:write | Upload a file (≤10 MB), returns token for comment attachment |

Prompts: `analyze-ticket`, `draft-ticket-response`. Resource: `zendesk://knowledge-base` (kb:read).

## Quick start (local, stdio — same as the original)

```bash
git clone <this-repo>
cd zendesk-mcp-server
uv venv && uv pip install -e .
cp .env.example .env   # fill in ZENDESK_SUBDOMAIN, ZENDESK_EMAIL, ZENDESK_API_KEY
```

Claude Desktop config:

```json
{
  "mcpServers": {
    "zendesk": {
      "command": "uv",
      "args": ["--directory", "/path/to/zendesk-mcp-server", "run", "zendesk"]
    }
  }
}
```

stdio mode is trusted-local: no auth, full access, identical behavior to the original repo.

## Remote mode (Streamable HTTP)

```bash
MCP_TRANSPORT=http zendesk        # serves http://0.0.0.0:8000/mcp  +  /health
```

Create scoped API keys (shown once, stored hashed):

```bash
zendesk-keys create --name "reader"  --scopes tickets:read,kb:read
zendesk-keys create --name "agent"   --scopes tickets:read,tickets:write,kb:read --expires-days 30
zendesk-keys create --name "admin"   --scopes "*"
zendesk-keys list
zendesk-keys revoke --id 2
```

Connect a client:

```bash
claude mcp add zendesk --transport http https://mcp.example.com/mcp \
  --header "Authorization: Bearer zmk_..."
```

Read-only keys never see write tools in `tools/list`; write calls without
`tickets:write` are denied; unknown tools are denied by default (fail closed).

## Configuration reference

All configuration is via environment variables (or `.env`; the admin console can
override connection settings at runtime, persisted in the key-store DB).

### Zendesk connection

| Variable | Required | Default | Description |
|---|---|---|---|
| `ZENDESK_SUBDOMAIN` | yes | — | `<subdomain>.zendesk.com` |
| `ZENDESK_API_KEY` | yes | — | Zendesk API token |
| `ZENDESK_EMAIL` | yes* | — | Single-identity mode: agent email paired with the token |
| `ZENDESK_READ_EMAIL` | no | `ZENDESK_EMAIL` | Dual identity: restricted user (light agent) for all reads |
| `ZENDESK_WRITE_EMAIL` | no | `ZENDESK_EMAIL` | Dual identity: full agent for all writes |

*Either `ZENDESK_EMAIL` or both `ZENDESK_READ_EMAIL`/`ZENDESK_WRITE_EMAIL`.
Zendesk roles live on the *user*, not the token — pairing the same token with a
restricted user email yields restricted permissions (Layer 4 backstop).

### Transport

| Variable | Default | Description |
|---|---|---|
| `MCP_TRANSPORT` | `stdio` | `stdio` (local, trusted) or `http` (remote) |
| `MCP_HOST` | `0.0.0.0` | HTTP bind address |
| `MCP_PORT` | `8000` | HTTP port; MCP endpoint is `/mcp` |

### Authentication

| Variable | Default | Description |
|---|---|---|
| `MCP_AUTH_ENABLED` | `true` | Set `false` only for trusted private networks |
| `MCP_AUTH_MODE` | `keys` | `keys` \| `oauth` \| `both` |
| `MCP_KEYS_DB` | `data/keys.db` | SQLite store for keys, tenants, config, audit log |
| `MCP_PUBLIC_URL` | — | Public base URL (required for oauth/both; used in RFC 9728 metadata) |
| `MCP_OAUTH_ISSUER` | — | OIDC issuer, e.g. `https://your-tenant.auth0.com/` |
| `MCP_OAUTH_AUDIENCE` | `MCP_PUBLIC_URL` | Audience/identifier of this server at the IdP |
| `MCP_OAUTH_JWKS_URI` | `<issuer>/.well-known/jwks.json` | Override if your IdP differs |
| `MCP_OAUTH_AUTH_SERVERS` | issuer | Comma-separated authorization server URLs |
| `MCP_OAUTH_TENANT_CLAIM` | `zendesk_tenant` | JWT claim naming the caller's tenant (id or name) |

### Admin console & safety

| Variable | Default | Description |
|---|---|---|
| `MCP_ADMIN_PASSWORD` | — (disabled) | Setting it enables the admin console |
| `MCP_ADMIN_HOST` | `127.0.0.1` | Keep loopback; reach via SSH/SSM tunnel |
| `MCP_ADMIN_PORT` | `9000` | Admin console port (never expose publicly) |
| `MCP_WRITE_CONFIRMATION` | `false` | Elicit user approval before PUBLIC comments (Layer 3) |

## Security model

| Layer | Mechanism |
|---|---|
| Edge | TLS 1.2+ (Caddy or ALB/ACM), security headers, 80/443 only |
| AuthN | API keys (hashed, expiring, revocable) and/or OAuth 2.1 JWTs (PKCE at the IdP) |
| L1 AuthZ | Central `TOOL_PERMISSIONS` map, enforced pre-dispatch, fail-closed |
| L2 Visibility | `tools/list` filtered to caller's scopes — models can't attempt what they can't see |
| L3 Confirmation | `destructiveHint` annotations + optional elicitation for public comments |
| L4 Zendesk | Dual identity — reads through a restricted Zendesk user, writes through a full agent |
| Admin | Separate loopback port, password + CSRF, secrets never re-displayed |
| Audit | Append-only log: key lifecycle, admin actions, logins, writes |

## Multi-tenancy

By default every caller uses the server's own Zendesk connection. To let other
teams/customers connect **their** Zendesk:

1. Admin console → *Tenants* → add name, subdomain, API token, read/write emails.
2. Bind credentials to the tenant: create an API key with that tenant selected
   (or `zendesk-keys create ... --tenant-id N`), or configure your IdP to issue
   the tenant's name/id in the `MCP_OAUTH_TENANT_CLAIM` JWT claim.
3. All tool calls from that identity are routed to the tenant's Zendesk.
   Deleting a tenant revokes its keys immediately.

## Web admin console

Enable with `MCP_ADMIN_PASSWORD`. Reach it via tunnel — never expose it:

```bash
ssh -L 9000:localhost:9000 user@host          # or the SSM equivalent, see docs/DEPLOYMENT.md
# open http://localhost:9000
```

Provides: Zendesk connection editor with hot-swap (no restart) and "test connection",
tenant management, API key create/revoke with scope checkboxes and expiry, and key
usage visibility. Stored tokens are never re-displayed; new keys are shown exactly once.

## Deployment

**Docker (single container):**

```bash
docker build -t zendesk-mcp-server .
docker run --rm -p 8000:8000 --env-file .env -v zmcp-keys:/data zendesk-mcp-server
```

**Docker Compose + automatic TLS (recommended single-box):** set `MCP_DOMAIN` in
`.env`, then `docker compose up -d` — Caddy terminates TLS with Let's Encrypt and
proxies to the server. Create keys with
`docker compose exec zendesk-mcp zendesk-keys create ...`.

**AWS EC2 (Terraform):** `terraform/` provisions EC2 (SSM access, no SSH, IMDSv2),
security group (80/443 only), Elastic IP, optional Route53, and bootstraps
Docker + the repo. Full runbook: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

**Windows Server without Docker:** native Python + Caddy with your own TLS
certificate + NSSM services, including a troubleshooting FAQ of real-world
Windows issues (Node/PATH, cert chains, arg mangling):
[docs/WINDOWS_SETUP.md](docs/WINDOWS_SETUP.md).

**AWS ECS Fargate + ALB (scale-out):** `terraform/ecs/` provisions ECR, Fargate
service, ALB with ACM/TLS 1.3, EFS-backed key store, Secrets Manager injection,
and CloudWatch. Keep `desired_count=1` until the key store is migrated off SQLite.

**MCP Registry:** fill in `server.json` and follow [docs/REGISTRY.md](docs/REGISTRY.md)
to publish your deployed server to registry.modelcontextprotocol.io.

## Development

```bash
uv sync --extra dev
uv run pytest tests/ -v        # 38 tests: tools, keystore, permissions, tenancy, auth modes
```

Project layout:

```
src/zendesk_mcp_server/
  server.py          # FastMCP app: tools, prompts, resources, transports
  zendesk_client.py  # Zendesk API client (dual identity, cursor pagination)
  auth.py            # API-key verifier, OIDC JWT verifier, auth-mode factory
  permissions.py     # Scope model + enforcement/filtering middleware
  keystore.py        # SQLite: keys, tenants, audit log
  runtime.py         # Client holder: hot-swap + per-tenant routing
  admin.py           # Web admin console (separate port)
  keys_cli.py        # zendesk-keys CLI
  config.py          # Env-based settings
terraform/           # EC2 deployment     terraform/ecs/  # Fargate deployment
docs/                # Architecture plan, deployment runbook, registry guide
```

Architecture decisions and full history: [docs/REMOTE_MCP_ARCHITECTURE_PLAN.md](docs/REMOTE_MCP_ARCHITECTURE_PLAN.md).

## License

Apache 2.0 — same as the original project. Original work by
[reminia](https://github.com/reminia/zendesk-mcp-server); extensions as described above.
