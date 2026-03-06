"""Tool/prompt/resource parity tests using the in-memory FastMCP client."""
import json

import pytest
from fastmcp import Client

from zendesk_mcp_server.permissions import TOOL_PERMISSIONS
from zendesk_mcp_server.server import build_server

EXPECTED_TOOLS = {
    "get_ticket",
    "get_tickets",
    "get_ticket_comments",
    "search_tickets",
    "get_ticket_attachment",
    "get_user",
    "search_users",
    "list_groups",
    "list_ticket_fields",
    "search_articles",
    "create_ticket",
    "update_ticket",
    "create_ticket_comment",
    "upload_attachment",
}


@pytest.fixture
def mcp(settings, fake_client):
    return build_server(settings, client=fake_client)


async def test_all_tools_registered(mcp):
    async with Client(mcp) as client:
        tools = {t.name for t in await client.list_tools()}
    assert tools == EXPECTED_TOOLS


async def test_every_tool_has_permission_entry(mcp):
    """Fail-closed guarantee: every registered tool must be classified."""
    async with Client(mcp) as client:
        tools = {t.name for t in await client.list_tools()}
    unclassified = tools - set(TOOL_PERMISSIONS)
    assert not unclassified, f"Tools without permission classification: {unclassified}"


async def test_read_only_annotations(mcp):
    async with Client(mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}
    for name, required in TOOL_PERMISSIONS.items():
        annotations = tools[name].annotations
        assert annotations is not None, f"{name} missing annotations"
        expected_read_only = required in ("tickets:read", "kb:read")
        assert annotations.readOnlyHint == expected_read_only, name


async def test_get_ticket(mcp, fake_client):
    async with Client(mcp) as client:
        result = await client.call_tool("get_ticket", {"ticket_id": 42})
    assert result.data["id"] == 42
    assert ("get_ticket", 42) in fake_client.calls


async def test_search_tickets(mcp, fake_client):
    async with Client(mcp) as client:
        result = await client.call_tool("search_tickets", {"query": "status:open"})
    assert ("search_tickets", "status:open") in fake_client.calls


async def test_create_and_update_ticket(mcp, fake_client):
    async with Client(mcp) as client:
        created = await client.call_tool(
            "create_ticket", {"subject": "New", "description": "Body"}
        )
        assert created.data["id"] == 99
        updated = await client.call_tool(
            "update_ticket", {"ticket_id": 99, "status": "solved"}
        )
        assert updated.data["status"] == "solved"


async def test_create_comment(mcp, fake_client):
    async with Client(mcp) as client:
        result = await client.call_tool(
            "create_ticket_comment",
            {"ticket_id": 7, "comment": "Hi", "public": False},
        )
    assert ("post_comment", 7, False) in fake_client.calls


async def test_prompts(mcp):
    async with Client(mcp) as client:
        prompts = {p.name for p in await client.list_prompts()}
        assert prompts == {"analyze-ticket", "draft-ticket-response"}
        result = await client.get_prompt("analyze-ticket", {"ticket_id": 5})
        assert "#5" in result.messages[0].content.text


async def test_new_tier1_tools(mcp, fake_client):
    async with Client(mcp) as client:
        user = await client.call_tool("get_user", {"user_id": 3})
        assert user.data["email"] == "jane@example.com"
        await client.call_tool("search_users", {"query": "jane"})
        await client.call_tool("list_groups", {})
        await client.call_tool("list_ticket_fields", {})
        await client.call_tool("search_articles", {"query": "reset"})
        upload = await client.call_tool(
            "upload_attachment", {"file_name": "log.txt", "data_base64": "aGk="}
        )
        assert upload.data["token"] == "tok123"
    assert ("search_articles", "reset") in fake_client.calls


async def test_comments_cursor_pagination(mcp, fake_client):
    async with Client(mcp) as client:
        result = await client.call_tool("get_ticket_comments", {"ticket_id": 8})
    assert result.data["has_more"] is False
    assert result.data["count"] == 1


async def test_knowledge_base_resource(mcp, fake_client):
    async with Client(mcp) as client:
        resources = await client.list_resources()
        uris = {str(r.uri) for r in resources}
        assert "zendesk://knowledge-base" in uris
        content = await client.read_resource("zendesk://knowledge-base")
        payload = json.loads(content[0].text)
        assert payload["metadata"]["sections"] == 1
