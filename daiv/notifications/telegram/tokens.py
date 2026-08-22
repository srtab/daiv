from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time

from django.conf import settings

# Long enough to switch apps, short enough that a leaked link is mostly dead. There is no
# row to consume, so the TTL is one of only two replay levers; the other is the state fold.
TOKEN_TTL_SECONDS = 600

_MAX_TOKEN_CHARS = 64  # Telegram's deep-link ``start`` payload cap.
_PACK_FORMAT = ">QI"  # 8-byte big-endian pk (DEFAULT_AUTO_FIELD is BigAutoField) + 4-byte unix expiry.
_PACKED_SIZE = 12
_MAC_SIZE = 16  # 128-bit truncation — standard, and what the 64-character budget affords.
_TOKEN_BYTES = _PACKED_SIZE + _MAC_SIZE  # 28 bytes → exactly 38 unpadded base64url characters.
# ``>Q`` decodes up to 2**64-1, but the pk reaches a query before the MAC is checked and SQLite
# raises ``OverflowError`` on anything past a signed 64-bit integer.
_MAX_USER_PK = 2**63 - 1


def _mac(packed: bytes, address: str, verified_at: str) -> bytes:
    """HMAC over the payload plus the caller's binding state.

    Folding ``address`` *and* ``verified_at`` mirrors how Django's
    ``PasswordResetTokenGenerator`` invalidates on ``password``/``last_login``: any successful
    ``/start`` moves ``verified_at``, which kills every earlier token. The NUL separator keeps
    ``("a", "bc")`` from hashing the same as ``("ab", "c")``.
    """
    message = b"\x00".join([packed, address.encode(), verified_at.encode()])
    return hmac.new(settings.SECRET_KEY.encode(), message, hashlib.sha256).digest()[:_MAC_SIZE]


def mint_token(user_pk: int, *, address: str, verified_at: str, now: int | None = None) -> str:
    expiry = (int(time.time()) if now is None else now) + TOKEN_TTL_SECONDS
    packed = struct.pack(_PACK_FORMAT, user_pk, expiry)
    return base64.urlsafe_b64encode(packed + _mac(packed, address, verified_at)).decode().rstrip("=")


def _decode(token: str, now: int | None) -> tuple[int, bytes, bytes] | None:
    """``(user_pk, packed, mac)`` for a well-formed, unexpired token; ``None`` otherwise."""
    if not token or len(token) > _MAX_TOKEN_CHARS:
        return None
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except ValueError:  # binascii.Error is a ValueError subclass
        return None
    if len(raw) != _TOKEN_BYTES:
        return None
    packed, mac = raw[:_PACKED_SIZE], raw[_PACKED_SIZE:]
    user_pk, expiry = struct.unpack(_PACK_FORMAT, packed)
    if not 0 < user_pk <= _MAX_USER_PK:
        return None
    if expiry <= (int(time.time()) if now is None else now):
        return None
    return user_pk, packed, mac


def peek_user_pk(token: str, *, now: int | None = None) -> int | None:
    """The pk a well-formed, unexpired token *claims* — no MAC check, so not authenticated.

    Its only use is loading the binding state ``verify_token`` then authenticates against;
    that split is what keeps this module from importing the binding table.
    """
    decoded = _decode(token, now)
    return None if decoded is None else decoded[0]


def verify_token(token: str, *, address: str, verified_at: str, now: int | None = None) -> int | None:
    """The authenticated pk, or ``None`` for expired, malformed, or state-invalidated tokens."""
    decoded = _decode(token, now)
    if decoded is None:
        return None
    user_pk, packed, mac = decoded
    if not hmac.compare_digest(mac, _mac(packed, address, verified_at)):
        return None
    return user_pk
