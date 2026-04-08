from __future__ import annotations

import base64
import logging

from django.conf import settings

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

logger = logging.getLogger("daiv.core")

_fernet: Fernet | None = None


def get_encryption_key() -> bytes:
    """
    Get the Fernet encryption key.

    Uses ``DAIV_ENCRYPTION_KEY`` from ``core.conf`` when set (either a raw
    Fernet key or an arbitrary passphrase), otherwise derives a deterministic
    key from ``DJANGO_SECRET_KEY`` via HKDF.

    Returns:
        A URL-safe base64-encoded Fernet key (44 bytes, encoding 32 raw bytes).
    """
    from core.conf import settings as core_settings

    if core_settings.ENCRYPTION_KEY is not None:
        raw = core_settings.ENCRYPTION_KEY.get_secret_value()
        key_bytes = raw.encode() if isinstance(raw, str) else raw
        try:
            Fernet(key_bytes)
            return key_bytes
        except ValueError:
            # Not a valid Fernet key — derive one from the passphrase via HKDF
            logger.info("DAIV_ENCRYPTION_KEY is not a raw Fernet key; deriving via HKDF.")
            return _derive_key(key_bytes)

    # Derive from DJANGO_SECRET_KEY
    secret = settings.SECRET_KEY
    if not secret:
        raise RuntimeError("Neither DAIV_ENCRYPTION_KEY nor DJANGO_SECRET_KEY is set.")
    return _derive_key(secret.encode() if isinstance(secret, str) else secret)


def _derive_key(source: bytes) -> bytes:
    """Derive a Fernet key from arbitrary bytes using HKDF-SHA256."""
    hkdf = HKDF(algorithm=SHA256(), length=32, salt=b"daiv-site-configuration", info=b"fernet-key")
    return base64.urlsafe_b64encode(hkdf.derive(source))


def get_fernet() -> Fernet:
    """
    Return a cached :class:`Fernet` instance for encrypting/decrypting values.
    """
    global _fernet  # noqa: PLW0603
    if _fernet is None:
        _fernet = Fernet(get_encryption_key())
    return _fernet


def encrypt_value(plaintext: str) -> str:
    """
    Encrypt a plaintext string.

    Args:
        plaintext: The string to encrypt.

    Returns:
        The Fernet token as a UTF-8 string.
    """
    return get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_value(ciphertext: str) -> str:
    """
    Decrypt a Fernet token back to plaintext.

    Args:
        ciphertext: The Fernet token string.

    Returns:
        The decrypted plaintext string.

    Raises:
        InvalidToken: If the token is invalid or corrupted.
    """
    return get_fernet().decrypt(ciphertext.encode()).decode()


def mask_secret(value: str, *, visible_prefix: int = 3, visible_suffix: int = 3) -> str:
    """
    Return a masked version of a secret for display purposes.

    Args:
        value: The secret value to mask.
        visible_prefix: Number of characters to show at the start.
        visible_suffix: Number of characters to show at the end.

    Returns:
        A masked string like ``sk-...abc``, or bullet characters for very short values.
    """
    if len(value) <= visible_prefix + visible_suffix + 3:
        return "\u2022" * min(len(value), 8)
    return f"{value[:visible_prefix]}...{value[-visible_suffix:]}"
