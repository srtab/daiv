from django.core import mail
from django.template.loader import render_to_string
from django.test import Client
from django.urls import NoReverseMatch, resolve, reverse

import pytest
from allauth.mfa.models import Authenticator

from accounts.models import Role, User


def _create_passkey(user, name="My key"):
    return Authenticator.objects.create(
        user=user, type=Authenticator.Type.WEBAUTHN, data={"name": name, "credential": {}}
    )


@pytest.fixture
def user(db):
    return User.objects.create_user(username="alice", email="alice@test.com", password="testpass123")  # noqa: S106


@pytest.fixture
def admin(db):
    return User.objects.create_user(
        username="admin",
        email="admin@test.com",
        password="testpass123",  # noqa: S106
        role=Role.ADMIN,
    )


@pytest.fixture
def admin_client(admin):
    client = Client()
    client.force_login(admin)
    return client


@pytest.mark.django_db
class TestPasskeyUrls:
    def test_mfa_url_wiring(self):
        assert reverse("mfa_login_webauthn") == "/accounts/mfa/webauthn/login/"
        assert reverse("mfa_list_webauthn") == "/accounts/mfa/webauthn/"
        assert reverse("mfa_add_webauthn") == "/accounts/mfa/webauthn/add/"

    def test_mfa_index_redirects_to_passkey_list(self, user):
        client = Client()
        client.force_login(user)
        response = client.get("/accounts/mfa/")
        assert response.status_code == 302
        assert response.url == reverse("mfa_list_webauthn")

    def test_passkey_signup_not_mounted(self):
        # Passkey signup stays off: users are created by admins.
        with pytest.raises(NoReverseMatch):
            reverse("mfa_signup_webauthn")

    def test_login_page_shows_passkey_button(self):
        response = Client().get(reverse("account_login"))
        assert response.status_code == 200
        content = response.content.decode()
        assert 'id="passkey_login"' in content
        assert "mfa_login" in content  # hidden credential form posted by allauth's JS

    def test_passkey_login_url_does_not_require_auth(self):
        assert resolve("/accounts/mfa/webauthn/login/").url_name == "mfa_login_webauthn"


@pytest.mark.django_db
class TestPasskeyManagementPages:
    def test_list_requires_login(self):
        response = Client().get(reverse("mfa_list_webauthn"))
        assert response.status_code == 302
        assert reverse("account_login") in response.url

    def test_list_shows_own_passkeys(self, user):
        _create_passkey(user, name="alice-key")
        client = Client()
        client.force_login(user)
        response = client.get(reverse("mfa_list_webauthn"))
        assert response.status_code == 200
        content = response.content.decode()
        assert "alice-key" in content
        assert reverse("mfa_add_webauthn") in content

    def test_add_requires_login(self):
        response = Client().get(reverse("mfa_add_webauthn"))
        assert response.status_code == 302


@pytest.mark.django_db
class TestPasskeyNotificationEmails:
    def _notify(self, user, prefix):
        from django.test import RequestFactory

        from allauth.account.adapter import get_adapter
        from allauth.core import context as allauth_context

        # allauth reads the request from its thread-local context; the views
        # always run inside a real request, so emulate one here.
        request = RequestFactory().get("/")
        with allauth_context.request_context(request):
            get_adapter().send_notification_mail(prefix, user)

    def test_added_notification_uses_daiv_templates(self, user):
        self._notify(user, "mfa/email/webauthn_added")
        assert len(mail.outbox) == 1
        message = mail.outbox[0]
        assert "example.com" in message.subject
        assert "A new passkey was added" in message.subject
        assert message.to == [user.email]
        assert "Touch ID, Windows Hello" in message.body
        assert "Touch ID, Windows Hello" in message.alternatives[0][0]

    def test_removed_notification_uses_daiv_templates(self, user):
        self._notify(user, "mfa/email/webauthn_removed")
        assert len(mail.outbox) == 1
        message = mail.outbox[0]
        assert "A passkey was removed" in message.subject
        assert "IP address" in message.body

    def test_non_passkey_prefix_falls_through_to_allauth(self, user):
        # Non-passkey prefixes (TOTP/recovery codes, not mounted in this repo) go to
        # allauth's default implementation: its own plain-text templates, not ours.
        self._notify(user, "mfa/email/totp_activated")
        assert len(mail.outbox) == 1
        message = mail.outbox[0]
        assert "Authenticator App Activated" in message.subject
        assert "Authenticator app activated" in message.body
        assert "Touch ID" not in message.body

    def test_notification_email_failure_is_swallowed(self, user):
        from unittest.mock import patch

        with patch("accounts.emails.send_mail", side_effect=RuntimeError("SMTP down")):
            self._notify(user, "mfa/email/webauthn_added")
        # No exception raised — the passkey operation must succeed regardless.

    def test_email_templates_render(self, user):
        from django.utils import timezone

        ctx = {"user": user, "timestamp": timezone.now(), "ip": "127.0.0.1", "user_agent": "test-agent"}
        for template in ("passkey_added", "passkey_removed"):
            assert render_to_string(f"accounts/emails/{template}.txt", ctx)
            assert render_to_string(f"accounts/emails/{template}.html", ctx)


@pytest.mark.django_db
class TestAdminPasskeyManagement:
    def test_admin_removes_user_passkey(self, admin_client, user):
        passkey = _create_passkey(user, name="alice-key")
        response = admin_client.post(reverse("user_passkey_remove", args=[user.pk, passkey.pk]))
        assert response.status_code == 302
        assert not Authenticator.objects.filter(pk=passkey.pk).exists()
        # The passkey owner — not the admin — is notified.
        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == [user.email]

    def test_admin_resets_all_user_passkeys(self, admin_client, user):
        _create_passkey(user, name="key-1")
        _create_passkey(user, name="key-2")
        response = admin_client.post(reverse("user_passkeys_reset", args=[user.pk]))
        assert response.status_code == 302
        assert not Authenticator.objects.filter(user=user).exists()
        assert len(mail.outbox) == 2

    def test_removal_scoped_to_target_user(self, admin_client, admin, user):
        # An authenticator of another user cannot be deleted via the wrong user URL.
        passkey = _create_passkey(user, name="alice-key")
        response = admin_client.post(reverse("user_passkey_remove", args=[admin.pk, passkey.pk]))
        assert response.status_code == 404
        assert Authenticator.objects.filter(pk=passkey.pk).exists()

    def test_non_admin_cannot_remove(self, user):
        client = Client()
        client.force_login(user)
        passkey = _create_passkey(user, name="alice-key")
        response = client.post(reverse("user_passkey_remove", args=[user.pk, passkey.pk]))
        assert response.status_code == 403
        assert Authenticator.objects.filter(pk=passkey.pk).exists()

    def test_user_update_page_shows_passkeys(self, admin_client, user):
        _create_passkey(user, name="alice-key")
        response = admin_client.get(reverse("user_update", args=[user.pk]))
        assert response.status_code == 200
        assert "alice-key" in response.content.decode()
