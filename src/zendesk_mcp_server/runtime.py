"""Runtime holder for the Zendesk client — supports hot-swap from the admin console.

Connection settings persisted via the admin console are stored in the same
SQLite database as the API keys (table `app_config`) and take precedence
over environment variables on startup.
"""
from __future__ import annotations

import logging
import sqlite3
import threading

from zendesk_mcp_server.config import Settings
from zendesk_mcp_server.zendesk_client import ZendeskClient

logger = logging.getLogger("zendesk-mcp-server.runtime")

_CONFIG_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_CONN_KEYS = ("subdomain", "api_token", "read_email", "write_email")


class ClientHolder:
    """Thread-safe holder so tools always call the current client."""

    def __init__(self, settings: Settings, client: ZendeskClient | None = None):
        self._lock = threading.Lock()
        self._settings = settings
        self._db_path = settings.keys_db
        self._tenant_cache: dict = {}
        self._client = client
        if self._client is None:
            conn_cfg = self._load_persisted() or {
                "subdomain": settings.subdomain,
                "api_token": settings.api_token,
                "read_email": settings.read_email,
                "write_email": settings.write_email,
            }
            self._conn_cfg = conn_cfg
            self._client = self._build(conn_cfg)
        else:
            self._conn_cfg = {
                "subdomain": settings.subdomain,
                "api_token": settings.api_token,
                "read_email": settings.read_email,
                "write_email": settings.write_email,
            }

    @staticmethod
    def _build(cfg: dict) -> ZendeskClient:
        return ZendeskClient(
            subdomain=cfg["subdomain"],
            token=cfg["api_token"],
            read_email=cfg["read_email"],
            write_email=cfg["write_email"],
        )

    def get(self) -> ZendeskClient:
        """Default-tenant client (env / admin-persisted config)."""
        with self._lock:
            return self._client

    # -- multi-tenancy ----------------------------------------------------

    def current(self) -> ZendeskClient:
        """Client for the calling identity's tenant.

        Tenant id comes from AccessToken.claims["tenant_id"] (set by
        ApiKeyVerifier from the key record, or by the OAuth JWT tenant claim).
        Falls back to the default client when there is no token or no tenant.
        """
        tenant_ref = self._current_tenant_ref()
        if tenant_ref is None:
            return self.get()
        client = self._tenant_client(tenant_ref)
        if client is None:
            raise RuntimeError(
                f"Tenant '{tenant_ref}' is not configured on this server. "
                "Ask the administrator to add it in the admin console."
            )
        return client

    @staticmethod
    def _current_tenant_ref():
        try:
            from fastmcp.server.dependencies import get_access_token
            token = get_access_token()
        except Exception:
            return None
        if token is None or not token.claims:
            return None
        return token.claims.get("tenant_id")

    def _tenant_client(self, tenant_ref) -> ZendeskClient | None:
        with self._lock:
            cached = self._tenant_cache.get(tenant_ref)
            if cached is not None:
                return cached

        from zendesk_mcp_server.keystore import KeyStore
        store = KeyStore(self._db_path)
        tenant = None
        try:
            tenant = store.tenant_get(int(tenant_ref))
        except (TypeError, ValueError):
            pass
        if tenant is None and isinstance(tenant_ref, str):
            tenant = store.tenant_find_by_name(tenant_ref)
        if tenant is None:
            return None

        client = ZendeskClient(
            subdomain=tenant.subdomain,
            token=tenant.api_token,
            read_email=tenant.read_email,
            write_email=tenant.write_email,
        )
        with self._lock:
            self._tenant_cache[tenant_ref] = client
        logger.info("Built Zendesk client for tenant '%s' (subdomain=%s)", tenant.name, tenant.subdomain)
        return client

    def invalidate_tenants(self) -> None:
        """Drop cached tenant clients (call after tenant config changes)."""
        with self._lock:
            self._tenant_cache.clear()

    def connection_info(self) -> dict:
        """Connection info for display — never exposes the token."""
        with self._lock:
            return {
                "subdomain": self._conn_cfg["subdomain"],
                "read_email": self._conn_cfg["read_email"],
                "write_email": self._conn_cfg["write_email"],
                "dual_identity": self._client.dual_identity,
            }

    def reconfigure(self, subdomain: str, read_email: str, write_email: str,
                    api_token: str | None = None) -> None:
        """Build and swap a new client; persist config. Empty token keeps the old one."""
        with self._lock:
            cfg = {
                "subdomain": subdomain.strip(),
                "api_token": (api_token or "").strip() or self._conn_cfg["api_token"],
                "read_email": read_email.strip(),
                "write_email": (write_email or read_email).strip(),
            }
            new_client = self._build(cfg)
            self._client = new_client
            self._conn_cfg = cfg
            self._persist(cfg)
            logger.info("Zendesk connection reconfigured (subdomain=%s)", cfg["subdomain"])

    def test_connection(self) -> dict:
        """Light API call to verify credentials (read identity)."""
        client = self.get()
        try:
            me = client._read.users.me()
            return {"ok": True, "user": getattr(me, "email", None), "role": getattr(me, "role", None)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # -- persistence -----------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.executescript(_CONFIG_SCHEMA)
        return conn

    def _persist(self, cfg: dict) -> None:
        try:
            with self._conn() as conn:
                for k in _CONN_KEYS:
                    conn.execute(
                        "INSERT INTO app_config (key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (f"zendesk.{k}", cfg[k]),
                    )
        except Exception as e:
            logger.error("Failed to persist connection config: %s", e)

    def _load_persisted(self) -> dict | None:
        try:
            with self._conn() as conn:
                rows = dict(
                    conn.execute(
                        "SELECT key, value FROM app_config WHERE key LIKE 'zendesk.%'"
                    ).fetchall()
                )
        except Exception:
            return None
        cfg = {k: rows.get(f"zendesk.{k}") for k in _CONN_KEYS}
        if all(cfg.values()):
            logger.info("Loaded Zendesk connection from admin-persisted config")
            return cfg
        return None
