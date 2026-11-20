from unittest.mock import patch

from django.core.cache import cache
from django.db import connections, transaction

import pytest

from core.encryption import DecryptionError
from core.models import (
    PROVIDERS_CACHE_KEY,
    SITE_CONFIGURATION_CACHE_KEY,
    WEB_FETCH_AUTH_HEADERS_CACHE_KEY,
    EncryptedJSONFieldDescriptor,
    Provider,
    SiteConfiguration,
    WebFetchAuthHeader,
)

COLD_CACHE_LOADS = [
    pytest.param(SITE_CONFIGURATION_CACHE_KEY, SiteConfiguration.get_cached, id="site-configuration"),
    pytest.param(WEB_FETCH_AUTH_HEADERS_CACHE_KEY, WebFetchAuthHeader.get_cached, id="web-fetch-auth-headers"),
    pytest.param(PROVIDERS_CACHE_KEY, Provider.get_cached_rows, id="providers"),
]


class _DummyJSONHost:
    """Minimal stand-in for a Django model holding the encrypted column."""

    _env_vars_encrypted = None
    env_vars = EncryptedJSONFieldDescriptor("env_vars")


def test_encrypted_json_descriptor_round_trip():
    host = _DummyJSONHost()
    host.env_vars = [{"name": "FOO", "value": "bar", "is_secret": True}]
    # Stored form is an encrypted string, not the original payload.
    assert isinstance(host._env_vars_encrypted, str)
    assert "FOO" not in host._env_vars_encrypted
    # Read returns the original payload structure.
    assert host.env_vars == [{"name": "FOO", "value": "bar", "is_secret": True}]


def test_encrypted_json_descriptor_clear_with_none():
    host = _DummyJSONHost()
    host.env_vars = [{"name": "FOO", "value": "bar", "is_secret": False}]
    host.env_vars = None
    assert host._env_vars_encrypted is None
    assert host.env_vars is None


def test_encrypted_json_descriptor_handles_empty_list():
    host = _DummyJSONHost()
    host.env_vars = []
    assert host.env_vars == []


def test_encrypted_json_descriptor_raises_decryption_error_on_corrupt_ciphertext():
    """Garbage ciphertext must raise DecryptionError so callers can distinguish
    "decrypt failed" from "no value stored" — and never silently round-trip
    over still-valid data."""
    host = _DummyJSONHost()
    host._env_vars_encrypted = "not-a-valid-fernet-token"
    with pytest.raises(DecryptionError):
        _ = host.env_vars


@pytest.mark.django_db
@pytest.mark.parametrize(("cache_key", "load"), COLD_CACHE_LOADS)
def test_cold_cache_load_keeps_the_connection_of_a_transaction(cache_key, load):
    cache.delete(cache_key)
    conn = connections["default"]
    with transaction.atomic(), patch.object(conn, "close", wraps=conn.close) as close:
        load()
    close.assert_not_called()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(("cache_key", "load"), COLD_CACHE_LOADS)
def test_cold_cache_load_returns_its_connection_outside_a_transaction(cache_key, load):
    cache.delete(cache_key)
    conn = connections["default"]
    with patch.object(conn, "close", wraps=conn.close) as close:
        load()
    close.assert_called()
