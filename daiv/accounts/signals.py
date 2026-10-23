import logging

from django.dispatch import receiver

from allauth.account.signals import user_logged_in

logger = logging.getLogger(__name__)


@receiver(user_logged_in)
def capture_platform_credential(sender, request, user, sociallogin=None, **kwargs) -> None:
    """Hand a social login's grant to the adapter; ``user_logged_in`` fires for new and returning users alike."""
    if sociallogin is None:
        return
    from allauth.socialaccount.adapter import get_adapter

    capture = getattr(get_adapter(), "capture_platform_credential", None)
    if capture is None:
        return
    try:
        capture(sociallogin)
    except Exception as exc:
        # No exc_info or message: Sentry would attach this frame's locals, and the text can echo a token.
        logger.error(
            "Failed to capture the platform credential for user pk=%s (%s)",
            getattr(user, "pk", None),
            type(exc).__name__,
        )
