"""Read/write permission control (plan §10.5, Layers 1 & 2).

Layer 1: scope enforcement before tool dispatch (fail-closed: unknown
tools require the reserved "admin" permission no key can hold except "*").
Layer 2: tools/list filtering so callers never see tools they cannot call.
"""
from __future__ import annotations

import logging

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware

logger = logging.getLogger("zendesk-mcp-server.permissions")

SCOPE_TICKETS_READ = "tickets:read"
SCOPE_TICKETS_WRITE = "tickets:write"
SCOPE_KB_READ = "kb:read"

# Reserved permission that no issuable scope satisfies (only "*" does).
ADMIN = "admin"

TOOL_PERMISSIONS: dict[str, str] = {
    # read tools
    "get_ticket": SCOPE_TICKETS_READ,
    "get_tickets": SCOPE_TICKETS_READ,
    "get_ticket_comments": SCOPE_TICKETS_READ,
    "get_ticket_attachment": SCOPE_TICKETS_READ,
    "search_tickets": SCOPE_TICKETS_READ,
    "get_user": SCOPE_TICKETS_READ,
    "search_users": SCOPE_TICKETS_READ,
    "list_groups": SCOPE_TICKETS_READ,
    "list_ticket_fields": SCOPE_TICKETS_READ,
    "search_articles": SCOPE_KB_READ,
    # privileged write tools
    "create_ticket": SCOPE_TICKETS_WRITE,
    "update_ticket": SCOPE_TICKETS_WRITE,
    "create_ticket_comment": SCOPE_TICKETS_WRITE,
    "upload_attachment": SCOPE_TICKETS_WRITE,
}

RESOURCE_PERMISSIONS: dict[str, str] = {
    "zendesk://knowledge-base": SCOPE_KB_READ,
}


def current_scopes() -> frozenset | None:
    """Scopes of the calling identity.

    Returns None when no auth is configured (stdio / trusted local mode),
    which grants full access.
    """
    try:
        token = get_access_token()
    except Exception:
        token = None
    if token is None:
        return None
    return frozenset(token.scopes or [])


def _client_id() -> str:
    try:
        token = get_access_token()
        return token.client_id if token else "local"
    except Exception:
        return "local"


def _allowed(required: str, scopes: frozenset | None) -> bool:
    if scopes is None:  # trusted local mode
        return True
    return "*" in scopes or required in scopes


class ScopeMiddleware(Middleware):
    """Enforce TOOL_PERMISSIONS / RESOURCE_PERMISSIONS per caller scope."""

    async def on_call_tool(self, context, call_next):
        name = context.message.name
        required = TOOL_PERMISSIONS.get(name, ADMIN)
        scopes = current_scopes()
        if not _allowed(required, scopes):
            logger.warning(
                "DENY tool=%s caller=%s required=%s scopes=%s",
                name, _client_id(), required, sorted(scopes or []),
            )
            raise ToolError(
                f"Permission denied: tool '{name}' requires scope '{required}'. "
                "Ask the server administrator to grant this scope to your API key."
            )
        if required == SCOPE_TICKETS_WRITE:
            logger.info("WRITE tool=%s caller=%s", name, _client_id())
        return await call_next(context)

    async def on_list_tools(self, context, call_next):
        tools = await call_next(context)
        scopes = current_scopes()
        if scopes is None or "*" in scopes:
            return tools
        return [
            t for t in tools
            if _allowed(TOOL_PERMISSIONS.get(t.name, ADMIN), scopes)
        ]

    async def on_read_resource(self, context, call_next):
        uri = str(context.message.uri)
        required = RESOURCE_PERMISSIONS.get(uri, ADMIN)
        scopes = current_scopes()
        if not _allowed(required, scopes):
            raise ToolError(
                f"Permission denied: resource '{uri}' requires scope '{required}'."
            )
        return await call_next(context)

    async def on_list_resources(self, context, call_next):
        resources = await call_next(context)
        scopes = current_scopes()
        if scopes is None or "*" in scopes:
            return resources
        return [
            r for r in resources
            if _allowed(RESOURCE_PERMISSIONS.get(str(r.uri), ADMIN), scopes)
        ]
