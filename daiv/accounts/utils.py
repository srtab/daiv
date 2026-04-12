from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from accounts.models import User

logger = logging.getLogger("daiv.accounts")


async def resolve_user_from_social(provider: str, uid: int) -> User | None:
    """Resolve a DAIV user from an external social account (GitLab/GitHub).

    Looks up the allauth SocialAccount by provider and external user ID.
    Returns None if no matching DAIV user is found or if the lookup fails.
    """
    from allauth.socialaccount.models import SocialAccount

    try:
        social = await SocialAccount.objects.select_related("user").aget(provider=provider, uid=str(uid))
    except SocialAccount.DoesNotExist:
        return None
    except Exception:
        logger.exception("Failed to resolve user from social account provider=%s uid=%s", provider, uid)
        return None
    return social.user
