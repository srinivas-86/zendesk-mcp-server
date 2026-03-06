"""Authentication providers.

Three modes (MCP_AUTH_MODE):
  keys  — internal ``zmk_`` API keys (default; zero external dependencies)
  oauth — OAuth 2.1 resource server: generic OIDC JWT validation (JWKS) +
          RFC 9728 Protected Resource Metadata, so MCP clients auto-discover
          the authorization server. Works with Auth0, Descope, WorkOS,
          Cognito, Keycloak, etc.
  both  — accept either credential type on the same endpoint.

Multi-tenancy: the resolved tenant id travels in AccessToken.claims
["tenant_id"] — from the key record (keys) or a configurable JWT claim (OAuth).
"""
from __future__ import annotations

import logging

from fastmcp.server.auth import AccessToken, AuthProvider, MultiAuth, RemoteAuthProvider, TokenVerifier
from fastmcp.server.auth.providers.jwt import JWTVerifier

from zendesk_mcp_server.config import Settings
from zendesk_mcp_server.keystore import KeyStore

logger = logging.getLogger("zendesk-mcp-server.auth")


class ApiKeyVerifier(TokenVerifier):
    """Verifies internal ``zmk_`` API keys against the SQLite key store."""

    def __init__(self, store: KeyStore):
        super().__init__()
        self.store = store

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token.startswith("zmk_"):
            return None  # not ours — lets MultiAuth try the JWT verifier
        info = self.store.verify(token)
        if info is None:
            logger.warning("Rejected invalid/revoked/expired API key")
            return None
        return AccessToken(
            token=token,
            client_id=f"key-{info.id}:{info.name}",
            scopes=sorted(info.scopes),
            expires_at=info.expires_at,
            claims={"tenant_id": info.tenant_id},
        )


class TenantClaimJWTVerifier(JWTVerifier):
    """JWTVerifier that maps a configurable IdP claim to our tenant_id claim.

    The claim value may be a tenant id (int) or tenant name (str) — resolution
    happens in runtime.py against the tenants table.
    """

    def __init__(self, *, tenant_claim: str, **kwargs):
        super().__init__(**kwargs)
        self._tenant_claim = tenant_claim

    async def verify_token(self, token: str) -> AccessToken | None:
        access = await super().verify_token(token)
        if access is None:
            return None
        claims = dict(access.claims or {})
        claims["tenant_id"] = claims.get(self._tenant_claim)
        access.claims = claims
        return access


def build_auth(settings: Settings) -> AuthProvider | TokenVerifier | None:
    """Build the auth provider for HTTP transport based on MCP_AUTH_MODE."""
    if not settings.auth_enabled:
        return None

    mode = settings.auth_mode
    if mode not in ("keys", "oauth", "both"):
        raise RuntimeError(f"Invalid MCP_AUTH_MODE '{mode}' (expected keys|oauth|both)")

    key_verifier = ApiKeyVerifier(KeyStore(settings.keys_db))
    if mode == "keys":
        logger.info("Auth mode: internal API keys")
        return key_verifier

    # OAuth pieces
    if not settings.oauth_issuer:
        raise RuntimeError("MCP_AUTH_MODE=oauth|both requires MCP_OAUTH_ISSUER")
    if not settings.public_url:
        raise RuntimeError("MCP_AUTH_MODE=oauth|both requires MCP_PUBLIC_URL "
                           "(the public base URL of this server, for RFC 9728 metadata)")

    issuer = settings.oauth_issuer.rstrip("/")
    jwks_uri = settings.oauth_jwks_uri or f"{issuer}/.well-known/jwks.json"
    jwt_verifier = TenantClaimJWTVerifier(
        tenant_claim=settings.oauth_tenant_claim,
        jwks_uri=jwks_uri,
        issuer=settings.oauth_issuer,
        audience=settings.oauth_audience or settings.public_url,
    )
    auth_servers = [
        s.strip() for s in (settings.oauth_auth_servers or settings.oauth_issuer).split(",")
        if s.strip()
    ]

    if mode == "oauth":
        logger.info("Auth mode: OAuth 2.1 (issuer=%s)", settings.oauth_issuer)
        return RemoteAuthProvider(
            token_verifier=jwt_verifier,
            authorization_servers=auth_servers,
            base_url=settings.public_url,
            resource_name="Zendesk MCP Server",
            scopes_supported=["tickets:read", "tickets:write", "kb:read"],
        )

    # both: RFC 9728 metadata + JWTs via the remote provider, plus internal keys
    logger.info("Auth mode: OAuth 2.1 + internal API keys (issuer=%s)", settings.oauth_issuer)
    remote = RemoteAuthProvider(
        token_verifier=jwt_verifier,
        authorization_servers=auth_servers,
        base_url=settings.public_url,
        resource_name="Zendesk MCP Server",
        scopes_supported=["tickets:read", "tickets:write", "kb:read"],
    )
    return MultiAuth(server=remote, verifiers=[key_verifier], base_url=settings.public_url)
