"""Scope middleware unit tests: enforcement (Layer 1) and filtering (Layer 2)."""
from types import SimpleNamespace

import pytest
from fastmcp.exceptions import ToolError

import zendesk_mcp_server.permissions as perms
from zendesk_mcp_server.permissions import ScopeMiddleware


def _ctx(tool_name):
    return SimpleNamespace(message=SimpleNamespace(name=tool_name))


async def _passthrough(ctx):
    return "EXECUTED"


@pytest.fixture
def mw():
    return ScopeMiddleware()


def _set_scopes(monkeypatch, scopes):
    monkeypatch.setattr(perms, "current_scopes", lambda: scopes)


async def test_read_scope_allows_read_tool(mw, monkeypatch):
    _set_scopes(monkeypatch, frozenset({"tickets:read"}))
    assert await mw.on_call_tool(_ctx("get_ticket"), _passthrough) == "EXECUTED"


async def test_read_scope_denies_write_tool(mw, monkeypatch):
    _set_scopes(monkeypatch, frozenset({"tickets:read"}))
    with pytest.raises(ToolError, match="tickets:write"):
        await mw.on_call_tool(_ctx("update_ticket"), _passthrough)


async def test_write_scope_allows_write_tool(mw, monkeypatch):
    _set_scopes(monkeypatch, frozenset({"tickets:read", "tickets:write"}))
    assert await mw.on_call_tool(_ctx("create_ticket"), _passthrough) == "EXECUTED"


async def test_unknown_tool_fails_closed(mw, monkeypatch):
    _set_scopes(monkeypatch, frozenset({"tickets:read", "tickets:write", "kb:read"}))
    with pytest.raises(ToolError):
        await mw.on_call_tool(_ctx("delete_everything"), _passthrough)


async def test_wildcard_allows_everything(mw, monkeypatch):
    _set_scopes(monkeypatch, frozenset({"*"}))
    assert await mw.on_call_tool(_ctx("update_ticket"), _passthrough) == "EXECUTED"


async def test_no_auth_local_mode_allows_everything(mw, monkeypatch):
    _set_scopes(monkeypatch, None)
    assert await mw.on_call_tool(_ctx("update_ticket"), _passthrough) == "EXECUTED"


async def test_list_tools_filtered_for_read_only_key(mw, monkeypatch):
    _set_scopes(monkeypatch, frozenset({"tickets:read"}))
    all_tools = [
        SimpleNamespace(name="get_ticket"),
        SimpleNamespace(name="search_tickets"),
        SimpleNamespace(name="update_ticket"),
        SimpleNamespace(name="create_ticket"),
    ]

    async def _next(ctx):
        return all_tools

    visible = await mw.on_list_tools(_ctx(None), _next)
    names = {t.name for t in visible}
    assert names == {"get_ticket", "search_tickets"}


async def test_resource_denied_without_kb_scope(mw, monkeypatch):
    _set_scopes(monkeypatch, frozenset({"tickets:read"}))
    ctx = SimpleNamespace(message=SimpleNamespace(uri="zendesk://knowledge-base"))
    with pytest.raises(ToolError, match="kb:read"):
        await mw.on_read_resource(ctx, _passthrough)


async def test_resource_allowed_with_kb_scope(mw, monkeypatch):
    _set_scopes(monkeypatch, frozenset({"kb:read"}))
    ctx = SimpleNamespace(message=SimpleNamespace(uri="zendesk://knowledge-base"))
    assert await mw.on_read_resource(ctx, _passthrough) == "EXECUTED"
