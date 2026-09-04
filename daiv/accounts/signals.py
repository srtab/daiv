"""Signal wiring for the accounts app."""

import logging

from django.dispatch import receiver

from allauth.account.signals import user_logged_in

logger = logging.getLogger(__name__)


@receiver(user_logged_in)
def capture_platform_credential(sender, request, user, sociallogin=None, **kwargs) -> None:
    """Persist the OAuth grant on every social login.

    ``user_logged_in`` is the only hook that fires for both a first signup and a returning user,
    and it fires after the user row exists. allauth's adapter has no equivalent.
    """
    if sociallogin is None:
        return
    from allauth.socialaccount.adapter import get_adapter

    adapter = get_adapter()
    capture = getattr(adapter, "capture_platform_credential", None)
    if capture is None:
        return
    try:
        capture(sociallogin)
    except Exception:
        logger.exception("Failed to capture the platform credential for user pk=%s", getattr(user, "pk", None))
