from __future__ import annotations

import logging
from typing import TYPE_CHECKING, NamedTuple

from codebase.base import GitPlatform

if TYPE_CHECKING:
    from accounts.models import User

logger = logging.getLogger("daiv.accounts")

_PLATFORM_USERNAME_KEYS = {GitPlatform.GITLAB: "username", GitPlatform.GITHUB: "login"}


async def resolve_user(provider: str, uid: int, *, username: str = "", email: str = "") -> User | None:
    """Resolve a DAIV user from an external git platform identity.

    Resolution order:
    1. Username match against DAIV user (most common match for orgs where platform and DAIV usernames align)
    2. Email match against DAIV user (when provided)
    3. Social account lookup by provider + uid (allauth fallback)

    Returns None if no matching DAIV user is found.
    """
    from allauth.socialaccount.models import SocialAccount

    from accounts.models import User as UserModel

    try:
        if username:
            try:
                return await UserModel.objects.aget(username=username)
            except UserModel.DoesNotExist:
                pass

        if email:
            try:
                return await UserModel.objects.aget(email=email)
            except UserModel.DoesNotExist:
                pass

        try:
            social = await SocialAccount.objects.select_related("user").aget(provider=provider, uid=str(uid))
        except SocialAccount.DoesNotExist:
            return None
        return social.user
    except Exception:
        logger.exception(
            "Failed to resolve user provider=%s uid=%s username=%s email=%s", provider, uid, username, email
        )
        return None


class PlatformIdentity(NamedTuple):
    """The two things a git platform addresses a user by: GitLab a numeric id, GitHub a login."""

    uid: int | None
    username: str | None


async def aget_platform_identity(*, user_id: int, provider: GitPlatform) -> PlatformIdentity | None:
    """The platform identity a DAIV user is linked to on ``provider``, or ``None`` when unlinked.

    The reverse of :func:`resolve_user`, for run-triggered work that only knows the DAIV user. The
    handle lives under a provider-specific key in the OAuth payload allauth stored at login.

    allauth is unique on ``(provider, uid)``, not ``(user, provider)``, so a re-linked user holds
    several rows; the oldest wins, deterministically.

    Best-effort: the sole consumer assigns merge requests with this, so a failed read is a ``None``
    rather than an exception that would abort a publish which has already pushed. It is logged
    here, so callers must not report ``None`` as "not linked".
    """
    from allauth.socialaccount.models import SocialAccount

    try:
        account = (
            await SocialAccount.objects
            .filter(user_id=user_id, provider=provider)
            .only("uid", "extra_data")
            .order_by("pk")
            .afirst()
        )
    except Exception:
        logger.exception("Failed to read platform identity for user_id=%s provider=%s", user_id, provider)
        return None
    if account is None:
        return None
    # A legacy or hand-written row can hold a non-dict, and a non-str handle would reach the
    # platform as a garbage assignee.
    extra_data = account.extra_data if isinstance(account.extra_data, dict) else {}
    key = _PLATFORM_USERNAME_KEYS.get(provider)
    handle = extra_data.get(key) if key else None
    return PlatformIdentity(
        # isdecimal, not int() in a try: it also rejects the sign and whitespace int() accepts.
        uid=int(account.uid) if account.uid.isdecimal() else None,
        username=handle if isinstance(handle, str) and handle else None,
    )
