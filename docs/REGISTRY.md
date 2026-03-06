# Publishing to the official MCP Registry

The MCP Registry (registry.modelcontextprotocol.io) is the public directory MCP
clients use to discover servers. Publishing makes `zendesk-mcp-server`
discoverable by name from Claude, and other registry-aware clients.

## Prerequisites

1. A deployed, publicly reachable server (`https://<your-domain>/mcp`) — EC2
   (`terraform/`) or ECS (`terraform/ecs/`).
2. **OAuth mode enabled** (`MCP_AUTH_MODE=oauth` or `both` + `MCP_PUBLIC_URL`).
   Registry-listed remote servers are expected to speak the standard OAuth
   discovery flow (RFC 9728), which this server serves at
   `/.well-known/oauth-protected-resource/mcp`. Internal API keys alone are not
   discoverable by strangers' clients.
3. Decide tenancy: public users connecting their own Zendesk need a tenant row
   (admin console) mapped from your IdP's tenant claim (`MCP_OAUTH_TENANT_CLAIM`).

## Steps

1. Edit `server.json` in the repo root:
   - `name`: `io.github.<your-github-user>/zendesk-mcp-server` (namespace is
     proven via GitHub login).
   - `remotes[0].url`: your real `https://.../mcp` URL.
   - Keep `version` in sync with `pyproject.toml`.

2. Install the publisher CLI and authenticate (GitHub OAuth proves the
   `io.github.<user>` namespace):

   ```bash
   brew install mcp-publisher        # or download from github.com/modelcontextprotocol/registry
   mcp-publisher login github
   ```

3. Validate and publish:

   ```bash
   mcp-publisher validate
   mcp-publisher publish
   ```

4. Verify:

   ```bash
   curl "https://registry.modelcontextprotocol.io/v0/servers?search=zendesk"
   ```

## Maintenance

- Republish on every release (bump `version` in both files). Add it to CI after
  the Docker build step once stable.
- The registry probes the remote URL with an MCP `initialize` request — keep
  `/health` and the ALB healthy or the listing shows as unreachable.
- To delist, publish with `"status": "deprecated"` in `server.json`.
