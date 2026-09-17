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


# DAIV-styled security notifications sent by allauth's passkey flows.
# Maps the allauth notification template prefix to the email kind ("passkey_added" /
# "passkey_removed", also the template base name) and its subject line.
PASSKEY_NOTIFICATIONS: dict[str, tuple[str, str]] = {
    "mfa/email/webauthn_added": ("passkey_added", "A new passkey was added"),
    "mfa/email/webauthn_removed": ("passkey_removed", "A passkey was removed"),
}


def send_passkey_notification_email(
    user: User, template_prefix: str, context: dict, email: str | None = None, connection=None
) -> bool:
    """
    Send the passkey added/removed security notification in DAIV's email styling.

    Failures are logged but not raised, mirroring ``send_welcome_email``: the passkey
    operation must succeed even if SMTP is down.

    Args:
        user: The user whose passkey was added or removed.
        template_prefix: allauth notification prefix, a key of ``PASSKEY_NOTIFICATIONS``
            (``mfa/email/webauthn_added`` or ``mfa/email/webauthn_removed``).
        context: Security context (timestamp, ip, user_agent, and optionally
            ``passkey_name`` for removals) rendered in the notice.
        email: Recipient address; defaults to the user's address.
        connection: Optional shared email backend connection (see ``get_connection``),
            so bulk senders can reuse one SMTP session.

    Returns:
        True if the email was sent, False otherwise.
    """
    from core.utils import prefixed_email_subject

    kind, subject = PASSKEY_NOTIFICATIONS[template_prefix]
    try:
        ctx = {"user": user, **context}
        subject = prefixed_email_subject(subject)
        text_body = render_to_string(f"accounts/emails/{kind}.txt", ctx)
        html_body = render_to_string(f"accounts/emails/{kind}.html", ctx)
        send_mail(
            subject=subject,
            message=text_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[email or user.email],
            html_message=html_body,
            connection=connection,
        )
    except Exception:
        logger.exception("Failed to send %s email to %s", kind, user.email)
        return False
    return True
