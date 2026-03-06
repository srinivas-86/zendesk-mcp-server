"""API key management CLI.

Usage:
    zendesk-keys create --name "alice-laptop" --scopes tickets:read,kb:read
    zendesk-keys create --name "automation" --scopes tickets:read,tickets:write --expires-days 30
    zendesk-keys list
    zendesk-keys revoke --id 3
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

from zendesk_mcp_server.keystore import KeyStore, VALID_SCOPES


def _fmt_ts(ts: int | None) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def main() -> None:
    parser = argparse.ArgumentParser(prog="zendesk-keys", description="Manage internal MCP API keys")
    parser.add_argument("--db", default=os.getenv("MCP_KEYS_DB", "data/keys.db"), help="Path to key store DB")
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create", help="Create a new API key")
    p_create.add_argument("--name", required=True, help="Human-readable key name")
    p_create.add_argument(
        "--scopes", required=True,
        help=f"Comma-separated scopes. Valid: {', '.join(sorted(VALID_SCOPES))}. "
             "Keys are read-only unless tickets:write is explicitly granted.",
    )
    p_create.add_argument("--expires-days", type=int, default=None, help="Expiry in days (default: never)")
    p_create.add_argument("--tenant-id", type=int, default=None,
                          help="Bind key to a tenant (see admin console); default: server's own Zendesk connection")

    sub.add_parser("list", help="List all keys")

    p_revoke = sub.add_parser("revoke", help="Revoke a key by id")
    p_revoke.add_argument("--id", type=int, required=True)

    args = parser.parse_args()
    store = KeyStore(args.db)

    if args.command == "create":
        scopes = [s.strip() for s in args.scopes.split(",") if s.strip()]
        key, key_id = store.create(args.name, scopes, args.expires_days, tenant_id=args.tenant_id)
        print(f"Key #{key_id} created for '{args.name}' with scopes: {', '.join(scopes)}")
        if "tickets:write" in scopes or "*" in scopes:
            print("NOTE: this key holds WRITE permission on Zendesk.")
        print("\nAPI key (shown ONCE, store it securely):\n")
        print(f"  {key}\n")
        print('Client usage:  Authorization: Bearer ' + key[:8] + "...")

    elif args.command == "list":
        keys = store.list()
        if not keys:
            print("No keys.")
            return
        print(f"{'id':>4}  {'name':<24} {'scopes':<36} {'created':<20} {'expires':<20} {'last used':<20} status")
        for k in keys:
            status = "REVOKED" if k["revoked"] else "active"
            print(
                f"{k['id']:>4}  {k['name']:<24} {k['scopes']:<36} "
                f"{_fmt_ts(k['created_at']):<20} {_fmt_ts(k['expires_at']):<20} "
                f"{_fmt_ts(k['last_used_at']):<20} {status}"
            )

    elif args.command == "revoke":
        if store.revoke(args.id):
            print(f"Key #{args.id} revoked.")
        else:
            print(f"Key #{args.id} not found or already revoked.", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
