"""SQLite-backed internal API key store.

Keys look like ``zmk_<random>``. Only the SHA-256 hash is persisted; the
plaintext key is shown exactly once at creation time.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass

KEY_PREFIX = "zmk_"

# Known scopes. "*" is a full-access wildcard reserved for admin keys.
VALID_SCOPES = {"tickets:read", "tickets:write", "kb:read", "*"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    scopes TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER,
    revoked INTEGER NOT NULL DEFAULT 0,
    last_used_at INTEGER,
    tenant_id INTEGER
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    key_id INTEGER,
    key_name TEXT,
    event TEXT NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS tenants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    subdomain TEXT NOT NULL,
    api_token TEXT NOT NULL,
    read_email TEXT NOT NULL,
    write_email TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0
);
"""

# Migration for databases created before multi-tenancy.
_MIGRATIONS = [
    "ALTER TABLE api_keys ADD COLUMN tenant_id INTEGER",
]


def generate_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


@dataclass
class KeyInfo:
    id: int
    name: str
    scopes: frozenset
    expires_at: int | None
    tenant_id: int | None = None


@dataclass
class Tenant:
    id: int
    name: str
    subdomain: str
    api_token: str
    read_email: str
    write_email: str


class KeyStore:
    def __init__(self, path: str):
        self.path = path
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
            for migration in _MIGRATIONS:
                try:
                    conn.execute(migration)
                except sqlite3.OperationalError:
                    pass  # column already exists

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    # -- key management -------------------------------------------------

    def create(self, name: str, scopes: list[str], expires_days: int | None = None,
               tenant_id: int | None = None) -> tuple[str, int]:
        """Create a key. Returns (plaintext_key, key_id). Plaintext is never stored."""
        bad = set(scopes) - VALID_SCOPES
        if bad:
            raise ValueError(f"Unknown scopes: {sorted(bad)}. Valid: {sorted(VALID_SCOPES)}")
        if not scopes:
            raise ValueError("At least one scope is required")

        key = generate_key()
        now = int(time.time())
        expires_at = now + expires_days * 86400 if expires_days else None
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO api_keys (name, key_hash, scopes, created_at, expires_at, tenant_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (name, hash_key(key), ",".join(sorted(set(scopes))), now, expires_at, tenant_id),
            )
            key_id = cur.lastrowid
            conn.execute(
                "INSERT INTO audit_log (ts, key_id, key_name, event, detail) VALUES (?, ?, ?, ?, ?)",
                (now, key_id, name, "key_created",
                 ",".join(sorted(set(scopes))) + (f" tenant={tenant_id}" if tenant_id else "")),
            )
        return key, key_id

    def verify(self, key: str) -> KeyInfo | None:
        """Constant-time-ish verification via hash lookup. Returns None if invalid."""
        if not key.startswith(KEY_PREFIX):
            return None
        digest = hash_key(key)
        now = int(time.time())
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id, name, scopes, expires_at, revoked, tenant_id "
                "FROM api_keys WHERE key_hash = ?",
                (digest,),
            ).fetchone()
            if row is None:
                return None
            key_id, name, scopes, expires_at, revoked, tenant_id = row
            if revoked:
                return None
            if expires_at is not None and now > expires_at:
                return None
            conn.execute(
                "UPDATE api_keys SET last_used_at = ? WHERE id = ?", (now, key_id)
            )
        return KeyInfo(
            id=key_id,
            name=name,
            scopes=frozenset(scopes.split(",")),
            expires_at=expires_at,
            tenant_id=tenant_id,
        )

    def list(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, name, scopes, created_at, expires_at, revoked, last_used_at, tenant_id "
                "FROM api_keys ORDER BY id"
            ).fetchall()
        return [
            {
                "id": r[0],
                "name": r[1],
                "scopes": r[2],
                "created_at": r[3],
                "expires_at": r[4],
                "revoked": bool(r[5]),
                "last_used_at": r[6],
                "tenant_id": r[7],
            }
            for r in rows
        ]

    # -- tenants ----------------------------------------------------------

    def tenant_create(self, name: str, subdomain: str, api_token: str,
                      read_email: str, write_email: str | None = None) -> int:
        if not all([name, subdomain, api_token, read_email]):
            raise ValueError("name, subdomain, api_token and read_email are required")
        now = int(time.time())
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO tenants (name, subdomain, api_token, read_email, write_email, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (name, subdomain, api_token, read_email, write_email or read_email, now),
            )
            tenant_id = cur.lastrowid
            conn.execute(
                "INSERT INTO audit_log (ts, key_id, key_name, event, detail) VALUES (?, ?, ?, ?, ?)",
                (now, None, None, "tenant_created", f"id={tenant_id} name={name} subdomain={subdomain}"),
            )
        return tenant_id

    def tenant_get(self, tenant_id: int) -> Tenant | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id, name, subdomain, api_token, read_email, write_email "
                "FROM tenants WHERE id = ? AND deleted = 0",
                (tenant_id,),
            ).fetchone()
        return Tenant(*row) if row else None

    def tenant_find_by_name(self, name: str) -> Tenant | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id, name, subdomain, api_token, read_email, write_email "
                "FROM tenants WHERE name = ? AND deleted = 0",
                (name,),
            ).fetchone()
        return Tenant(*row) if row else None

    def tenant_list(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, name, subdomain, read_email, write_email, created_at "
                "FROM tenants WHERE deleted = 0 ORDER BY id"
            ).fetchall()
        return [
            {"id": r[0], "name": r[1], "subdomain": r[2], "read_email": r[3],
             "write_email": r[4], "created_at": r[5]}
            for r in rows
        ]

    def tenant_delete(self, tenant_id: int) -> bool:
        now = int(time.time())
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE tenants SET deleted = 1 WHERE id = ? AND deleted = 0", (tenant_id,)
            )
            if cur.rowcount:
                # Orphaned keys fall back to the default tenant — revoke instead for safety.
                conn.execute(
                    "UPDATE api_keys SET revoked = 1 WHERE tenant_id = ? AND revoked = 0",
                    (tenant_id,),
                )
                conn.execute(
                    "INSERT INTO audit_log (ts, key_id, event, detail) VALUES (?, ?, ?, ?)",
                    (now, None, "tenant_deleted", f"id={tenant_id} (its keys revoked)"),
                )
        return bool(cur.rowcount)

    def revoke(self, key_id: int) -> bool:
        now = int(time.time())
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE api_keys SET revoked = 1 WHERE id = ? AND revoked = 0", (key_id,)
            )
            if cur.rowcount:
                conn.execute(
                    "INSERT INTO audit_log (ts, key_id, event) VALUES (?, ?, ?)",
                    (now, key_id, "key_revoked"),
                )
        return bool(cur.rowcount)

    # -- audit -----------------------------------------------------------

    def audit(self, key_id: int | None, key_name: str | None, event: str, detail: str = "") -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO audit_log (ts, key_id, key_name, event, detail) VALUES (?, ?, ?, ?, ?)",
                (int(time.time()), key_id, key_name, event, detail),
            )
