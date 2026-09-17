from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string

if TYPE_CHECKING:
    from accounts.models import User

logger = logging.getLogger(__name__)


def send_welcome_email(user: User, login_url: str) -> bool:
    """
    Send a welcome email to a newly created user with a link to sign in.

    Failures are logged but not raised, so callers should check the return value
    to determine whether the email was delivered.

    Args:
        user: The newly created user.
        login_url: Absolute URL to the login page.

    Returns:
        True if the email was sent successfully, False otherwise.
    """
    from core.utils import prefixed_email_subject

    try:
        context = {"user": user, "login_url": login_url}
        subject = prefixed_email_subject("You've been invited")
        text_body = render_to_string("accounts/emails/welcome.txt", context)
        html_body = render_to_string("accounts/emails/welcome.html", context)
        send_mail(
            subject=subject,
            message=text_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            html_message=html_body,
        )
    except Exception:
        logger.exception("Failed to send welcome email to %s", user.email)
        return False
    return True
