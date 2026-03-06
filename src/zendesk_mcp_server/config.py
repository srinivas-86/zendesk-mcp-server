"""Runtime configuration for the Zendesk MCP server."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    # Zendesk connection
    subdomain: str
    api_token: str
    read_email: str
    write_email: str

    # Transport
    transport: str = "stdio"  # "stdio" | "http"
    host: str = "0.0.0.0"
    port: int = 8000

    # Auth (HTTP only)
    auth_enabled: bool = True
    auth_mode: str = "keys"  # "keys" | "oauth" | "both"
    keys_db: str = "data/keys.db"

    # OAuth 2.1 / generic OIDC (auth_mode oauth|both).
    # Works with Auth0, Descope, WorkOS, Cognito, Keycloak, etc.
    oauth_issuer: str = ""            # e.g. https://your-tenant.auth0.com/
    oauth_jwks_uri: str = ""          # default: <issuer>/.well-known/jwks.json
    oauth_audience: str = ""          # this server's identifier at the IdP
    oauth_auth_servers: str = ""      # comma-separated; default: issuer
    oauth_tenant_claim: str = "zendesk_tenant"  # JWT claim carrying tenant id
    public_url: str = ""              # e.g. https://mcp.example.com (RFC 9728 metadata)

    # Admin console (HTTP only; disabled unless a password is set)
    admin_password: str = ""
    admin_host: str = "127.0.0.1"
    admin_port: int = 9000

    # Layer 3: elicit user confirmation before posting PUBLIC comments
    write_confirmation: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        subdomain = os.getenv("ZENDESK_SUBDOMAIN", "")
        api_token = os.getenv("ZENDESK_API_KEY", "")
        base_email = os.getenv("ZENDESK_EMAIL", "")
        # Layer-4 dual identity: fall back to the single email when
        # dedicated read/write identities are not configured.
        read_email = os.getenv("ZENDESK_READ_EMAIL", base_email)
        write_email = os.getenv("ZENDESK_WRITE_EMAIL", base_email)

        missing = [
            name
            for name, val in (
                ("ZENDESK_SUBDOMAIN", subdomain),
                ("ZENDESK_API_KEY", api_token),
                ("ZENDESK_EMAIL or ZENDESK_READ_EMAIL/ZENDESK_WRITE_EMAIL", read_email and write_email),
            )
            if not val
        ]
        if missing:
            raise RuntimeError(
                "Missing required environment variables: " + ", ".join(missing)
            )

        return cls(
            subdomain=subdomain,
            api_token=api_token,
            read_email=read_email,
            write_email=write_email,
            transport=os.getenv("MCP_TRANSPORT", "stdio").strip().lower(),
            host=os.getenv("MCP_HOST", "0.0.0.0"),
            port=int(os.getenv("MCP_PORT", "8000")),
            auth_enabled=_env_bool("MCP_AUTH_ENABLED", True),
            auth_mode=os.getenv("MCP_AUTH_MODE", "keys").strip().lower(),
            keys_db=os.getenv("MCP_KEYS_DB", "data/keys.db"),
            oauth_issuer=os.getenv("MCP_OAUTH_ISSUER", "").strip(),
            oauth_jwks_uri=os.getenv("MCP_OAUTH_JWKS_URI", "").strip(),
            oauth_audience=os.getenv("MCP_OAUTH_AUDIENCE", "").strip(),
            oauth_auth_servers=os.getenv("MCP_OAUTH_AUTH_SERVERS", "").strip(),
            oauth_tenant_claim=os.getenv("MCP_OAUTH_TENANT_CLAIM", "zendesk_tenant").strip(),
            public_url=os.getenv("MCP_PUBLIC_URL", "").strip(),
            admin_password=os.getenv("MCP_ADMIN_PASSWORD", ""),
            admin_host=os.getenv("MCP_ADMIN_HOST", "127.0.0.1"),
            admin_port=int(os.getenv("MCP_ADMIN_PORT", "9000")),
            write_confirmation=_env_bool("MCP_WRITE_CONFIRMATION", False),
        )
