"""Zendesk MCP server — FastMCP, Streamable HTTP + stdio.

Transports:
  MCP_TRANSPORT=stdio (default) — local, trusted, no auth.
  MCP_TRANSPORT=http            — Streamable HTTP on /mcp with internal
                                  API-key auth (Bearer zmk_...) and
                                  scope-based read/write permissions.

Optional (HTTP mode):
  MCP_ADMIN_PASSWORD=...        — enables the web admin console on
                                  MCP_ADMIN_HOST:MCP_ADMIN_PORT (default
                                  127.0.0.1:9000 — keep it off the internet).
  MCP_WRITE_CONFIRMATION=true   — elicit user approval before posting
                                  PUBLIC ticket comments (Layer 3).
"""
from __future__ import annotations

import logging

import anyio
from cachetools.func import ttl_cache
from dotenv import load_dotenv
from fastmcp import Context, FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from zendesk_mcp_server.config import Settings
from zendesk_mcp_server.permissions import ScopeMiddleware
from zendesk_mcp_server.runtime import ClientHolder
from zendesk_mcp_server.zendesk_client import ZendeskClient

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("zendesk-mcp-server")

TICKET_ANALYSIS_TEMPLATE = """
You are a helpful Zendesk support analyst. You've been asked to analyze ticket #{ticket_id}.

Please fetch the ticket info and comments to analyze it and provide:
1. A summary of the issue
2. The current status and timeline
3. Key points of interaction

Remember to be professional and focus on actionable insights.
"""

COMMENT_DRAFT_TEMPLATE = """
You are a helpful Zendesk support agent. You need to draft a response to ticket #{ticket_id}.

Please fetch the ticket info, comments and knowledge base to draft a professional and helpful response that:
1. Acknowledges the customer's concern
2. Addresses the specific issues raised
3. Provides clear next steps or ask for specific details need to proceed
4. Maintains a friendly and professional tone
5. Ask for confirmation before commenting on the ticket

The response should be formatted well and ready to be posted as a comment.
"""

READ_ONLY = {"readOnlyHint": True}
WRITE = {"readOnlyHint": False, "destructiveHint": False}
DESTRUCTIVE = {"readOnlyHint": False, "destructiveHint": True}


def build_server(settings: Settings, client: ZendeskClient | None = None) -> FastMCP:
    """Build the FastMCP server. `client` is injectable for tests."""
    holder = ClientHolder(settings, client=client)
    if holder.get().dual_identity:
        logger.info("Layer 4 active: dual Zendesk identity (read/write split)")

    auth = None
    if settings.transport == "http":
        from zendesk_mcp_server.auth import build_auth
        auth = build_auth(settings)
        if auth is None:
            logger.warning("MCP_AUTH_ENABLED=false — HTTP endpoint is UNAUTHENTICATED")

    mcp = FastMCP(
        name="Zendesk",
        instructions=(
            "Zendesk Support integration. Read tools require the tickets:read "
            "scope; write tools (create/update/comment/upload) require "
            "tickets:write. Knowledge-base access requires kb:read."
        ),
        auth=auth,
        middleware=[ScopeMiddleware()],
    )
    # Exposed for the admin console (main() wires it up).
    mcp._zendesk_holder = holder  # type: ignore[attr-defined]

    # -- read tools -----------------------------------------------------

    @mcp.tool(annotations=READ_ONLY, run_in_thread=True)
    def get_ticket(ticket_id: int) -> dict:
        """Retrieve a Zendesk ticket by its ID."""
        return holder.current().get_ticket(ticket_id)

    @mcp.tool(annotations=READ_ONLY, run_in_thread=True)
    def get_tickets(
        per_page: int = 25,
        cursor: str | None = None,
        sort_by: str = "created_at",
        sort_order: str = "desc",
    ) -> dict:
        """Fetch tickets with cursor pagination.

        Pass the returned after_cursor as `cursor` to get the next page.
        sort_by: created_at | updated_at | priority | status. per_page max 100.
        """
        return holder.current().get_tickets(
            per_page=per_page, cursor=cursor, sort_by=sort_by, sort_order=sort_order
        )

    @mcp.tool(annotations=READ_ONLY, run_in_thread=True)
    def get_ticket_comments(ticket_id: int, per_page: int = 50, cursor: str | None = None) -> dict:
        """Retrieve comments for a ticket (cursor-paginated), including attachment metadata."""
        return holder.current().get_ticket_comments(ticket_id, per_page=per_page, cursor=cursor)

    @mcp.tool(annotations=READ_ONLY, run_in_thread=True)
    def search_tickets(query: str, limit: int = 25) -> list:
        """Search tickets with Zendesk search syntax.

        Examples: 'status:open priority:high', 'requester:jane@example.com',
        'printer error created>2026-01-01'. limit max is 100.
        """
        return holder.current().search_tickets(query=query, limit=limit)

    @mcp.tool(annotations=READ_ONLY, run_in_thread=True)
    def get_ticket_attachment(content_url: str) -> dict:
        """Fetch an image attachment by its content_url (from get_ticket_comments)
        and return it as base64-encoded data. Only safe image types are allowed."""
        return holder.current().get_ticket_attachment(content_url)

    @mcp.tool(annotations=READ_ONLY, run_in_thread=True)
    def get_user(user_id: int) -> dict:
        """Get a Zendesk user by ID — resolve requester_id/assignee_id to name, email, role."""
        return holder.current().get_user(user_id)

    @mcp.tool(annotations=READ_ONLY, run_in_thread=True)
    def search_users(query: str, limit: int = 25) -> list:
        """Search Zendesk users by name or email."""
        return holder.current().search_users(query=query, limit=limit)

    @mcp.tool(annotations=READ_ONLY, run_in_thread=True)
    def list_groups() -> list:
        """List agent groups (teams) for routing tickets."""
        return holder.current().list_groups()

    @mcp.tool(annotations=READ_ONLY, run_in_thread=True)
    def list_ticket_fields() -> list:
        """List ticket fields including custom fields with their IDs, types, and options.
        Use this to interpret or set custom_fields {id, value} pairs on tickets."""
        return holder.current().list_ticket_fields()

    @mcp.tool(annotations=READ_ONLY, run_in_thread=True)
    def search_articles(query: str, limit: int = 10) -> list:
        """Search Help Center knowledge-base articles. Prefer this over reading the
        whole zendesk://knowledge-base resource."""
        return holder.current().search_articles(query=query, limit=limit)

    # -- privileged write tools ------------------------------------------

    @mcp.tool(annotations=WRITE, run_in_thread=True)
    def create_ticket(
        subject: str,
        description: str,
        requester_id: int | None = None,
        assignee_id: int | None = None,
        priority: str | None = None,
        type: str | None = None,
        tags: list[str] | None = None,
        custom_fields: list[dict] | None = None,
    ) -> dict:
        """Create a new Zendesk ticket. Requires the tickets:write scope.

        priority: low | normal | high | urgent.
        type: problem | incident | question | task.
        custom_fields: list of {id, value} objects (see list_ticket_fields).
        """
        return holder.current().create_ticket(
            subject=subject,
            description=description,
            requester_id=requester_id,
            assignee_id=assignee_id,
            priority=priority,
            type=type,
            tags=tags,
            custom_fields=custom_fields,
        )

    @mcp.tool(annotations=DESTRUCTIVE, run_in_thread=True)
    def update_ticket(
        ticket_id: int,
        subject: str | None = None,
        status: str | None = None,
        priority: str | None = None,
        type: str | None = None,
        assignee_id: int | None = None,
        requester_id: int | None = None,
        tags: list[str] | None = None,
        custom_fields: list[dict] | None = None,
        due_at: str | None = None,
    ) -> dict:
        """Update fields on an existing Zendesk ticket. Requires the tickets:write scope.

        status: new | open | pending | on-hold | solved | closed.
        priority: low | normal | high | urgent. due_at: ISO8601 datetime.
        """
        fields = {
            "subject": subject,
            "status": status,
            "priority": priority,
            "type": type,
            "assignee_id": assignee_id,
            "requester_id": requester_id,
            "tags": tags,
            "custom_fields": custom_fields,
            "due_at": due_at,
        }
        return holder.current().update_ticket(ticket_id=ticket_id, **fields)

    @mcp.tool(annotations=WRITE, run_in_thread=True)
    def upload_attachment(
        file_name: str,
        data_base64: str,
        content_type: str = "application/octet-stream",
    ) -> dict:
        """Upload a file (base64) to Zendesk. Returns an upload token to pass to
        create_ticket_comment's upload_tokens. Requires the tickets:write scope. Max 10 MB."""
        return holder.current().upload_attachment(
            file_name=file_name, data_base64=data_base64, content_type=content_type
        )

    @mcp.tool(annotations=DESTRUCTIVE)
    async def create_ticket_comment(
        ticket_id: int,
        comment: str,
        public: bool = True,
        upload_tokens: list[str] | None = None,
        ctx: Context = None,
    ) -> str:
        """Add a comment to an existing Zendesk ticket. Requires the tickets:write scope.

        WARNING: public=true comments are visible to the end customer.
        upload_tokens: tokens from upload_attachment to attach files.
        """
        # Layer 3: human-in-the-loop confirmation for customer-visible comments.
        if settings.write_confirmation and public and ctx is not None:
            try:
                result = await ctx.elicit(
                    f"Post this PUBLIC comment to ticket #{ticket_id}? "
                    f"It will be visible to the customer.\n\n---\n{comment[:500]}",
                    response_type=None,
                )
                accepted = type(result).__name__ == "AcceptedElicitation"
            except Exception as e:
                # Client doesn't support elicitation — fail safe for public comments.
                raise RuntimeError(
                    "This server requires user confirmation for public comments, but the "
                    "client does not support elicitation. Post as public=false, or ask the "
                    "administrator to disable MCP_WRITE_CONFIRMATION."
                ) from e
            if not accepted:
                return f"Comment NOT posted to ticket {ticket_id}: user declined confirmation."

        client_for_tenant = holder.current()

        def _post():
            return client_for_tenant.post_comment(
                ticket_id=ticket_id, comment=comment, public=public,
                upload_tokens=upload_tokens,
            )

        await anyio.to_thread.run_sync(_post)
        return f"Comment created successfully on ticket {ticket_id} (public={public})"

    # -- prompts ----------------------------------------------------------

    @mcp.prompt(name="analyze-ticket", description="Analyze a Zendesk ticket and provide insights")
    def analyze_ticket(ticket_id: int) -> str:
        return TICKET_ANALYSIS_TEMPLATE.format(ticket_id=ticket_id).strip()

    @mcp.prompt(name="draft-ticket-response", description="Draft a professional response to a Zendesk ticket")
    def draft_ticket_response(ticket_id: int) -> str:
        return COMMENT_DRAFT_TEMPLATE.format(ticket_id=ticket_id).strip()

    # -- resources ---------------------------------------------------------

    @ttl_cache(ttl=3600)
    def get_cached_kb():
        # Default tenant only — the shared KB resource is not tenant-scoped
        # (tenant callers should use the search_articles tool instead).
        return holder.get().get_all_articles()

    @mcp.resource(
        "zendesk://knowledge-base",
        name="Zendesk Knowledge Base",
        description="Access to Zendesk Help Center articles and sections",
        mime_type="application/json",
    )
    def knowledge_base() -> dict:
        kb_data = get_cached_kb()
        return {
            "knowledge_base": kb_data,
            "metadata": {
                "sections": len(kb_data),
                "total_articles": sum(len(s["articles"]) for s in kb_data.values()),
            },
        }

    # -- ops endpoints ------------------------------------------------------

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "service": "zendesk-mcp-server"})

    return mcp


def _start_admin(settings: Settings, holder: ClientHolder) -> None:
    """Run the admin console in a daemon thread on its own port."""
    import threading

    import uvicorn

    from zendesk_mcp_server.admin import build_admin_app
    from zendesk_mcp_server.keystore import KeyStore

    app = build_admin_app(
        holder=holder,
        store=KeyStore(settings.keys_db),
        admin_password=settings.admin_password,
    )

    def _run():
        uvicorn.run(app, host=settings.admin_host, port=settings.admin_port, log_level="warning")

    threading.Thread(target=_run, daemon=True, name="admin-console").start()
    logger.info(
        "Admin console on http://%s:%s (keep this OFF the public internet)",
        settings.admin_host, settings.admin_port,
    )


def main() -> None:
    load_dotenv()
    settings = Settings.from_env()
    mcp = build_server(settings)

    if settings.transport == "http":
        if settings.admin_password:
            _start_admin(settings, mcp._zendesk_holder)  # type: ignore[attr-defined]
        logger.info("Starting Streamable HTTP transport on %s:%s/mcp", settings.host, settings.port)
        mcp.run(transport="http", host=settings.host, port=settings.port)
    else:
        logger.info("Starting stdio transport")
        mcp.run()


if __name__ == "__main__":
    main()
