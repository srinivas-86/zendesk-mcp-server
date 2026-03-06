import time

import pytest

from zendesk_mcp_server.keystore import KeyStore, generate_key


@pytest.fixture
def store(tmp_path):
    return KeyStore(str(tmp_path / "keys.db"))


def test_create_and_verify(store):
    key, key_id = store.create("test", ["tickets:read"])
    assert key.startswith("zmk_")
    info = store.verify(key)
    assert info is not None
    assert info.id == key_id
    assert info.scopes == frozenset({"tickets:read"})


def test_verify_unknown_key(store):
    assert store.verify(generate_key()) is None
    assert store.verify("not-a-key") is None
    assert store.verify("") is None


def test_revoke(store):
    key, key_id = store.create("test", ["tickets:read", "tickets:write"])
    assert store.verify(key) is not None
    assert store.revoke(key_id) is True
    assert store.verify(key) is None
    assert store.revoke(key_id) is False  # already revoked


def test_expiry(store, monkeypatch):
    key, _ = store.create("temp", ["tickets:read"], expires_days=1)
    assert store.verify(key) is not None
    # jump 2 days into the future
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 2 * 86400)
    assert store.verify(key) is None


def test_invalid_scopes_rejected(store):
    with pytest.raises(ValueError):
        store.create("bad", ["tickets:admin"])
    with pytest.raises(ValueError):
        store.create("empty", [])


def test_list_and_last_used(store):
    key, key_id = store.create("k1", ["kb:read"])
    store.verify(key)
    keys = store.list()
    assert len(keys) == 1
    assert keys[0]["id"] == key_id
    assert keys[0]["last_used_at"] is not None
    assert keys[0]["revoked"] is False
