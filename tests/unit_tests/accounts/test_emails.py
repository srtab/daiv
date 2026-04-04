from unittest.mock import patch

from django.core import mail

import pytest

from accounts.emails import send_welcome_email
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
        assert "DAIV" in mail.outbox[0].subject

    def test_returns_false_on_send_failure(self, user):
        with patch("accounts.emails.send_mail", side_effect=OSError("SMTP connection refused")):
            result = send_welcome_email(user, "https://example.com/login/")
        assert result is False
        assert len(mail.outbox) == 0

    def test_returns_false_on_template_error(self, user):
        with patch("accounts.emails.render_to_string", side_effect=Exception("template error")):
            result = send_welcome_email(user, "https://example.com/login/")
        assert result is False
