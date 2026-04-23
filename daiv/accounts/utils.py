from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from accounts.models import User

logger = logging.getLogger("daiv.accounts")


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
