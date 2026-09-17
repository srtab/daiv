from unittest.mock import patch

from django.core import mail
from django.template.loader import render_to_string

import pytest

from accounts.emails import send_passkey_notification_email, send_welcome_email
from accounts.models import Role, User


@pytest.fixture
def user(db):
    return User.objects.create_user(
        username="testuser",
        email="test@example.com",
        password="testpass123",  # noqa: S106
        role=Role.MEMBER,
    )


@pytest.mark.django_db
class TestSendWelcomeEmail:
    def test_sends_email_successfully(self, user):
        result = send_welcome_email(user, "https://example.com/login/")
        assert result is True
        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == ["test@example.com"]
        assert mail.outbox[0].subject.startswith("[")
        assert "You've been invited" in mail.outbox[0].subject

    def test_returns_false_on_send_failure(self, user):
        with patch("accounts.emails.send_mail", side_effect=OSError("SMTP connection refused")):
            result = send_welcome_email(user, "https://example.com/login/")
        assert result is False
        assert len(mail.outbox) == 0

    def test_returns_false_on_template_error(self, user):
        with patch("accounts.emails.render_to_string", side_effect=Exception("template error")):
            result = send_welcome_email(user, "https://example.com/login/")
        assert result is False


def _security_context():
    from django.utils import timezone

    return {"timestamp": timezone.now(), "ip": "203.0.113.10", "user_agent": "test-agent"}


@pytest.mark.django_db
class TestSendPasskeyNotificationEmail:
    def test_added_email_sends_daiv_styled_content(self, user):
        result = send_passkey_notification_email(user, "mfa/email/webauthn_added", _security_context())
        assert result is True
        assert len(mail.outbox) == 1
        message = mail.outbox[0]
        assert message.to == ["test@example.com"]
        assert "A new passkey was added" in message.subject
        assert "Touch ID, Windows Hello" in message.body
        assert "Touch ID, Windows Hello" in message.alternatives[0][0]

    def test_removed_email_sends_daiv_styled_content(self, user):
        result = send_passkey_notification_email(user, "mfa/email/webauthn_removed", _security_context())
        assert result is True
        assert len(mail.outbox) == 1
        message = mail.outbox[0]
        assert "A passkey was removed" in message.subject
        assert "IP address" in message.body

    def test_returns_false_on_send_failure(self, user):
        # The passkey operation must succeed even if SMTP is down.
        with patch("accounts.emails.send_mail", side_effect=OSError("SMTP connection refused")):
            result = send_passkey_notification_email(user, "mfa/email/webauthn_added", _security_context())
        assert result is False
        assert len(mail.outbox) == 0

    def test_email_templates_render(self, user):
        ctx = {"user": user, **_security_context()}
        for template in ("passkey_added", "passkey_removed"):
            assert render_to_string(f"accounts/emails/{template}.txt", ctx)
            assert render_to_string(f"accounts/emails/{template}.html", ctx)
