"""The Telegram binding invariants, in one place.

This sits on the notifications side of the transport boundary because it reads and writes
``UserChannelBinding``. ``notifications/telegram/`` must not import it.
"""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from notifications.choices import ChannelType
from notifications.models import UserChannelBinding


def _telegram_rows():
    return UserChannelBinding.objects.filter(channel_type=ChannelType.TELEGRAM)


def binding_state(user) -> tuple[str, str]:
    """The ``(address, verified_at)`` pair the link-token HMAC folds in.

    Both are empty strings when the user has no Telegram binding, so mint and verify resolve
    them identically. The pair is unambiguous because ``bind_chat`` keeps at most one row.
    """
    return binding_state_for_pk(user.pk)


def binding_state_for_pk(user_pk: int) -> tuple[str, str]:
    binding = _telegram_rows().filter(user_id=user_pk).order_by("-modified").first()
    if binding is None:
        return "", ""
    return binding.address, binding.verified_at.isoformat() if binding.verified_at else ""


@transaction.atomic
def bind_chat(user, *, chat_id: str, handle: str) -> None:
    """Bind ``chat_id`` to ``user``, enforcing both invariants in one transaction.

    ``user_channel_binding_unique`` is per-(user, channel_type, address), so it permits both
    duplicates that matter here. Re-pointing a chat already bound elsewhere is safe: only the
    chat's owner can send from it.
    """
    _telegram_rows().filter(address=chat_id).exclude(user=user).delete()
    _telegram_rows().filter(user=user).exclude(address=chat_id).delete()
    UserChannelBinding.objects.update_or_create(
        user=user,
        channel_type=ChannelType.TELEGRAM,
        address=chat_id,
        defaults={"extra_config": {"handle": handle}, "is_verified": True, "verified_at": timezone.now()},
    )


def unbind_chat(chat_id: str) -> int:
    """Remove the binding for ``chat_id`` — exactly one row, per the invariants."""
    deleted, _by_model = _telegram_rows().filter(address=chat_id).delete()
    return deleted


def unbind_user(user) -> int:
    """Remove ``user``'s binding, whichever chat it points at."""
    deleted, _by_model = _telegram_rows().filter(user=user).delete()
    return deleted


def unverify_binding(user_id, address: str) -> int:
    """Mark a binding unusable without deleting it, so the channels page can prompt a reconnect.

    ``resolve_address`` filters on ``is_verified=True``, so later notifications record as
    skipped instead of burning three retries each. ``modified`` is set explicitly because
    ``.update()`` bypasses the ``TimeStampedModel`` auto-now.
    """
    rows = _telegram_rows().filter(user_id=user_id, address=address, is_verified=True)
    return rows.update(is_verified=False, modified=timezone.now())
