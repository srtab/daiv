from unittest.mock import patch

import pytest

from core.encryption import decrypt_value, encrypt_value, get_encryption_key, mask_secret


class TestEncryptDecryptRoundTrip:
    def test_round_trip(self):
        plaintext = "sk-test-api-key-12345"
        encrypted = encrypt_value(plaintext)
        assert encrypted != plaintext
        assert decrypt_value(encrypted) == plaintext

    def test_different_plaintexts_produce_different_ciphertexts(self):
        a = encrypt_value("key-a")
        b = encrypt_value("key-b")
        assert a != b

    def test_encrypting_same_value_twice_produces_different_tokens(self):
        """Fernet includes a timestamp, so encrypting the same value twice produces different tokens."""
        a = encrypt_value("same")
        b = encrypt_value("same")
        assert a != b
        assert decrypt_value(a) == decrypt_value(b) == "same"


class TestGetEncryptionKey:
    def test_derives_key_from_secret_key(self):
        key = get_encryption_key()
        assert isinstance(key, bytes)
        assert len(key) == 44  # base64-encoded 32-byte key

    def test_derived_key_is_deterministic(self):
        key1 = get_encryption_key()
        key2 = get_encryption_key()
        assert key1 == key2

    @pytest.mark.django_db
    def test_raises_when_no_secret_key(self):
        import core.encryption

        original_fernet = core.encryption._fernet
        core.encryption._fernet = None

        try:
            with (
                patch("core.conf.settings") as mock_core_settings,
                patch("core.encryption.settings") as mock_django_settings,
                pytest.raises(RuntimeError, match="Neither"),
            ):
                mock_core_settings.ENCRYPTION_KEY = None
                mock_django_settings.SECRET_KEY = None
                get_encryption_key()
        finally:
            core.encryption._fernet = original_fernet


class TestMaskSecret:
    def test_mask_normal_length(self):
        assert mask_secret("sk-abc123456xyz") == "sk-...xyz"

    def test_mask_short_value(self):
        result = mask_secret("abc")
        assert "\u2022" in result

    def test_mask_custom_visibility(self):
        assert mask_secret("abcdefghijklm", visible_prefix=4, visible_suffix=2) == "abcd...lm"

    def test_mask_empty_string(self):
        result = mask_secret("")
        assert result == ""
