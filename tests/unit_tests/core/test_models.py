import pytest

from core.encryption import DecryptionError
from core.models import EncryptedJSONFieldDescriptor


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
