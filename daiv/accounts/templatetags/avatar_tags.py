from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from django import template

if TYPE_CHECKING:
    from accounts.models import User

register = template.Library()

PALETTE_SIZE = 10


def _label(user: User | None) -> str:
    if user is None:
        return ""
    name = getattr(user, "name", None) or getattr(user, "email", None) or getattr(user, "username", None) or ""
    return name.strip()


@register.simple_tag
def user_initials(user: User | None) -> str:
    """Return up to 2 uppercase characters identifying ``user`` (e.g. "Ada Lovelace" → "AL")."""
    name = (getattr(user, "name", None) or "").strip() if user is not None else ""
    if name:
        parts = name.split()
        if len(parts) >= 2:
            return (parts[0][:1] + parts[-1][:1]).upper()
        return parts[0][:2].upper()

    email = (getattr(user, "email", None) or "").strip() if user is not None else ""
    if email:
        local = email.split("@", 1)[0]
        if local:
            return local[:2].upper()

    username = (getattr(user, "username", None) or "").strip() if user is not None else ""
    if username:
        return username[:2].upper()

    return "?"


@register.simple_tag
def user_color_index(user: User | None) -> int:
    """Stable palette index in ``[0, PALETTE_SIZE)`` for ``user``.

    Keyed on ``username`` (unique and stable) with fallbacks. Same input always
    yields the same index, so two people with the same first letter still get
    distinct gradients across the UI.
    """
    if user is None:
        return 0
    key = (getattr(user, "username", None) or getattr(user, "email", None) or _label(user)).lower().strip()
    if not key:
        return 0
    digest = hashlib.md5(key.encode("utf-8"), usedforsecurity=False).digest()
    return digest[0] % PALETTE_SIZE
