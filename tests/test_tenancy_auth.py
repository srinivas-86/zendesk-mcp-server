"""Multi-tenancy and auth-mode tests."""
import pytest

from zendesk_mcp_server.auth import ApiKeyVerifier, build_auth
from zendesk_mcp_server.config import Settings
from zendesk_mcp_server.keystore import KeyStore
from zendesk_mcp_server.runtime import ClientHolder


@pytest.fixture
def store(tmp_path):
    return KeyStore(str(tmp_path / "keys.db"))


# -- tenants ---------------------------------------------------------------

def test_tenant_crud(store):
    tid = store.tenant_create("acme", "acme", "tok-acme", "reader@acme.com", "writer@acme.com")
    t = store.tenant_get(tid)
    assert t.name == "acme" and t.subdomain == "acme"
    assert store.tenant_find_by_name("acme").id == tid
    assert len(store.tenant_list()) == 1
    assert store.tenant_delete(tid) is True
    assert store.tenant_get(tid) is None


def test_tenant_delete_revokes_its_keys(store):
    tid = store.tenant_create("acme", "acme", "tok", "r@acme.com")
    key, _ = store.create("acme-key", ["tickets:read"], tenant_id=tid)
    assert store.verify(key) is not None
    store.tenant_delete(tid)
    assert store.verify(key) is None


def test_key_carries_tenant_id(store):
    tid = store.tenant_create("acme", "acme", "tok", "r@acme.com")
    key, _ = store.create("k", ["tickets:read"], tenant_id=tid)
    info = store.verify(key)
    assert info.tenant_id == tid


async def test_api_key_verifier_sets_tenant_claim(store):
    tid = store.tenant_create("acme", "acme", "tok", "r@acme.com")
    key, _ = store.create("k", ["tickets:read"], tenant_id=tid)
    verifier = ApiKeyVerifier(store)
    access = await verifier.verify_token(key)
    assert access.claims["tenant_id"] == tid
    # non-zmk tokens are passed over (lets MultiAuth try JWT verification)
    assert await verifier.verify_token("eyJhbGciOi...") is None


# -- tenant routing ----------------------------------------------------------

def test_holder_routes_by_tenant(settings, fake_client, monkeypatch, store):
    settings.keys_db = store.path
    tid = store.tenant_create("acme", "acme-sub", "tok", "r@acme.com", "w@acme.com")
    holder = ClientHolder(settings, client=fake_client)

    # no token -> default client
    monkeypatch.setattr(ClientHolder, "_current_tenant_ref", staticmethod(lambda: None))
    assert holder.current() is fake_client

    # tenant token -> per-tenant client with that tenant's subdomain
    monkeypatch.setattr(ClientHolder, "_current_tenant_ref", staticmethod(lambda: tid))
    client = holder.current()
    assert client is not fake_client
    assert client.subdomain == "acme-sub"
    assert client.dual_identity is True
    # cached on second call
    assert holder.current() is client

    # unknown tenant -> clear error
    monkeypatch.setattr(ClientHolder, "_current_tenant_ref", staticmethod(lambda: 9999))
    with pytest.raises(RuntimeError, match="not configured"):
        holder.current()


# -- auth mode factory ---------------------------------------------------------

def _settings(tmp_path, **kw):
    base = dict(
        subdomain="x", api_token="t", read_email="r@x.com", write_email="w@x.com",
        transport="http", keys_db=str(tmp_path / "k.db"),
    )
    base.update(kw)
    return Settings(**base)


def test_build_auth_keys_mode(tmp_path):
    auth = build_auth(_settings(tmp_path, auth_mode="keys"))
    assert isinstance(auth, ApiKeyVerifier)


def test_build_auth_disabled(tmp_path):
    assert build_auth(_settings(tmp_path, auth_enabled=False)) is None


def test_build_auth_oauth_requires_issuer(tmp_path):
    with pytest.raises(RuntimeError, match="MCP_OAUTH_ISSUER"):
        build_auth(_settings(tmp_path, auth_mode="oauth"))


def test_build_auth_oauth_requires_public_url(tmp_path):
    with pytest.raises(RuntimeError, match="MCP_PUBLIC_URL"):
        build_auth(_settings(tmp_path, auth_mode="oauth", oauth_issuer="https://idp.example.com"))


def test_build_auth_oauth_mode(tmp_path):
    from fastmcp.server.auth import RemoteAuthProvider
    auth = build_auth(_settings(
        tmp_path, auth_mode="oauth",
        oauth_issuer="https://idp.example.com",
        oauth_audience="https://mcp.example.com",
        public_url="https://mcp.example.com",
    ))
    assert isinstance(auth, RemoteAuthProvider)


def test_build_auth_both_mode(tmp_path):
    from fastmcp.server.auth import MultiAuth
    auth = build_auth(_settings(
        tmp_path, auth_mode="both",
        oauth_issuer="https://idp.example.com",
        public_url="https://mcp.example.com",
    ))
    assert isinstance(auth, MultiAuth)


def test_build_auth_invalid_mode(tmp_path):
    with pytest.raises(RuntimeError, match="Invalid MCP_AUTH_MODE"):
        build_auth(_settings(tmp_path, auth_mode="bogus"))
