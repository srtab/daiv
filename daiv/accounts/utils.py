from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from accounts.models import User

logger = logging.getLogger("daiv.accounts")


async def resolve_user(provider: str, uid: int, *, username: str = "", email: str = "") -> User | None:
    """Resolve a DAIV user from an external git platform identity.

    Resolution order:
    1. Social account lookup by provider + uid (the verified platform identity link)
    2. Username match against DAIV user (only when no social account exists for the
       provider + uid, so a platform user sharing a victim's username cannot be
       misattributed to the victim)
    3. Email match against DAIV user (same fallback constraint as username)

    Returns None if no matching DAIV user is found.
    """
    from allauth.socialaccount.models import SocialAccount

    from accounts.models import User as UserModel

    try:
        # Resolve the verified platform identity first. The webhook-claimed
        # username/email are unverified attributes, so they must only be used when no
        # social account is linked for this provider + uid — otherwise an attacker who
        # shares a victim's username/email on the platform would have their runs
        # attributed to the victim.
        try:
            social = await SocialAccount.objects.select_related("user").aget(provider=provider, uid=str(uid))
            return social.user
        except SocialAccount.DoesNotExist:
            pass

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

        return None
    except Exception:
        logger.exception(
            "Failed to resolve user provider=%s uid=%s username=%s email=%s", provider, uid, username, email
        )
        return None
