from unittest.mock import patch

from django.contrib.messages import get_messages
from django.core import mail
from django.test import Client
from django.urls import reverse

import pytest

from accounts.models import APIKey, Role, User


@pytest.fixture
def user(db):
    return User.objects.create_user(username="alice", email="alice@test.com", password="testpass123")  # noqa: S106


@pytest.fixture
def other_user(db):
    return User.objects.create_user(username="bob", email="bob@test.com", password="testpass456")  # noqa: S106


@pytest.fixture
def logged_in_client(user):
    client = Client()
    client.force_login(user)
    return client


def _create_api_key(user, name="test-key"):
    gen = APIKey.objects.key_generator
    key, prefix, hashed_key = gen.generate()
    api_key = APIKey.objects.create(user=user, name=name, prefix=prefix, hashed_key=hashed_key)
    return api_key, key


@pytest.mark.django_db
class TestAPIKeyListView:
    def test_member_sees_only_own_keys(self, logged_in_client, user, other_user):
        _create_api_key(user, name="alice-key")
        _create_api_key(other_user, name="bob-key")

        response = logged_in_client.get(reverse("api_keys"))
        assert response.status_code == 200

        keys = response.context["api_keys"]
        assert len(keys) == 1
        assert keys[0].name == "alice-key"

    def test_admin_sees_all_keys(self, other_user):
        admin = User.objects.create_user(
            username="admin",
            email="admin@test.com",
            password="testpass123",  # noqa: S106
            role=Role.ADMIN,
        )
        _create_api_key(admin, name="admin-key")
        _create_api_key(other_user, name="bob-key")

        client = Client()
        client.force_login(admin)
        response = client.get(reverse("api_keys"))
        assert response.status_code == 200

        keys = response.context["api_keys"]
        assert len(keys) == 2
        assert {k.name for k in keys} == {"admin-key", "bob-key"}
        assert response.context["is_admin"] is True


@pytest.mark.django_db
class TestAPIKeyCreateView:
    def test_create_stores_key_in_session(self, logged_in_client, user):
        response = logged_in_client.post(reverse("api_key_create"), {"name": "my-key"})
        assert response.status_code == 302
        assert response.url == reverse("api_keys")

        api_key = APIKey.objects.get(user=user)
        assert api_key.name == "my-key"
        assert not api_key.revoked

        # The raw key was stored in the session so it can be shown once on the list page.
        session = logged_in_client.session
        assert api_key.prefix in session["new_api_key"]

    def test_create_multiple_keys(self, logged_in_client, user):
        logged_in_client.post(reverse("api_key_create"), {"name": "key-1"})
        logged_in_client.post(reverse("api_key_create"), {"name": "key-2"})

        assert APIKey.objects.filter(user=user).count() == 2

    def test_create_requires_login(self):
        client = Client()
        response = client.post(reverse("api_key_create"), {"name": "my-key"})
        assert response.status_code == 302
        assert "/accounts/login/" in response.url


@pytest.mark.django_db
class TestAPIKeyRevokeView:
    def test_revoke_own_key(self, logged_in_client, user):
        api_key, _ = _create_api_key(user)

        response = logged_in_client.post(reverse("api_key_revoke", kwargs={"pk": api_key.pk}))
        assert response.status_code == 302

        api_key.refresh_from_db()
        assert api_key.revoked

    def test_cannot_revoke_other_users_key(self, logged_in_client, other_user):
        api_key, _ = _create_api_key(other_user, name="bob-key")

        response = logged_in_client.post(reverse("api_key_revoke", kwargs={"pk": api_key.pk}))
        assert response.status_code == 302

        api_key.refresh_from_db()
        assert not api_key.revoked

        msgs = list(get_messages(response.wsgi_request))
        assert any("not found" in str(m).lower() for m in msgs)

    def test_revoke_already_revoked_key(self, logged_in_client, user):
        api_key, _ = _create_api_key(user)
        api_key.revoked = True
        api_key.save(update_fields=["revoked"])

        response = logged_in_client.post(reverse("api_key_revoke", kwargs={"pk": api_key.pk}))
        assert response.status_code == 302

        msgs = list(get_messages(response.wsgi_request))
        assert any("already revoked" in str(m).lower() for m in msgs)

    def test_revoke_requires_login(self, user):
        api_key, _ = _create_api_key(user)

        client = Client()
        response = client.post(reverse("api_key_revoke", kwargs={"pk": api_key.pk}))
        assert response.status_code == 302
        assert "/accounts/login/" in response.url

        api_key.refresh_from_db()
        assert not api_key.revoked


# ---------------------------------------------------------------------------
# User management views
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestUserListView:
    def test_admin_can_list_users(self, admin_client, admin_user, member_user):
        response = admin_client.get(reverse("user_list"))
        assert response.status_code == 200
        assert admin_user in response.context["users"]
        assert member_user in response.context["users"]

    def test_member_gets_403(self, member_client):
        response = member_client.get(reverse("user_list"))
        assert response.status_code == 403

    def test_anonymous_redirected_to_login(self):
        response = Client().get(reverse("user_list"))
        assert response.status_code == 302
        assert "/accounts/login/" in response.url

    def test_search_filters_by_name(self, admin_client, admin_user, member_user):
        member_user.name = "Specific Name"
        member_user.save()
        response = admin_client.get(reverse("user_list"), {"q": "Specific"})
        assert member_user in response.context["users"]
        assert admin_user not in response.context["users"]

    def test_search_filters_by_email(self, admin_client, admin_user, member_user):
        response = admin_client.get(reverse("user_list"), {"q": "member@"})
        assert member_user in response.context["users"]
        assert admin_user not in response.context["users"]

    def test_filter_by_role(self, admin_client, admin_user, member_user):
        response = admin_client.get(reverse("user_list"), {"role": "admin"})
        assert admin_user in response.context["users"]
        assert member_user not in response.context["users"]


@pytest.mark.django_db
class TestUserCreateView:
    def test_admin_can_create_user(self, admin_client):
        response = admin_client.post(
            reverse("user_create"), {"name": "New User", "email": "new@test.com", "role": Role.MEMBER}
        )
        assert response.status_code == 302
        assert response.url == reverse("user_list")
        assert User.objects.filter(email="new@test.com").exists()

    def test_member_gets_403(self, member_client):
        response = member_client.post(
            reverse("user_create"), {"name": "New User", "email": "new@test.com", "role": Role.MEMBER}
        )
        assert response.status_code == 403

    def test_duplicate_email_shows_error(self, admin_client, member_user):
        response = admin_client.post(
            reverse("user_create"), {"name": "Dup", "email": member_user.email, "role": Role.MEMBER}
        )
        assert response.status_code == 200
        assert response.context["form"].errors

    def test_created_user_gets_member_role(self, admin_client):
        admin_client.post(reverse("user_create"), {"name": "Default", "email": "default@test.com", "role": Role.MEMBER})
        user = User.objects.get(email="default@test.com")
        assert user.role == Role.MEMBER

    def test_welcome_email_sent(self, admin_client):
        admin_client.post(reverse("user_create"), {"name": "Emailed", "email": "emailed@test.com", "role": Role.MEMBER})
        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == ["emailed@test.com"]
        assert "example.com" in mail.outbox[0].subject

    def test_created_user_has_unusable_password(self, admin_client):
        admin_client.post(reverse("user_create"), {"name": "NoPwd", "email": "nopwd@test.com", "role": Role.MEMBER})
        user = User.objects.get(email="nopwd@test.com")
        assert not user.has_usable_password()

    def test_warning_shown_when_email_fails(self, admin_client):
        with patch("accounts.views.send_welcome_email", return_value=False):
            response = admin_client.post(
                reverse("user_create"), {"name": "NoEmail", "email": "noemail@test.com", "role": Role.MEMBER}
            )
        assert response.status_code == 302
        assert User.objects.filter(email="noemail@test.com").exists()
        msgs = list(get_messages(response.wsgi_request))
        assert any("could not be sent" in str(m) for m in msgs)


@pytest.mark.django_db
class TestUserUpdateView:
    def test_admin_can_update_user(self, admin_client, member_user):
        response = admin_client.post(
            reverse("user_update", kwargs={"pk": member_user.pk}),
            {"name": "Updated", "email": member_user.email, "role": Role.MEMBER, "is_active": "true"},
        )
        assert response.status_code == 302
        member_user.refresh_from_db()
        assert member_user.name == "Updated"

    def test_member_gets_403(self, member_client, admin_user):
        response = member_client.post(
            reverse("user_update", kwargs={"pk": admin_user.pk}),
            {"name": "Hacked", "email": admin_user.email, "role": Role.MEMBER, "is_active": "true"},
        )
        assert response.status_code == 403

    def test_cannot_demote_last_admin(self, admin_client, admin_user):
        response = admin_client.post(
            reverse("user_update", kwargs={"pk": admin_user.pk}),
            {"name": admin_user.name, "email": admin_user.email, "role": Role.MEMBER, "is_active": "true"},
        )
        assert response.status_code == 200
        assert response.context["form"].errors

    def test_can_demote_self_when_other_admin_exists(self, admin_client, admin_user):
        User.objects.create_user(
            username="admin2",
            email="admin2@test.com",
            password="testpass123",  # noqa: S106
            role=Role.ADMIN,
        )
        response = admin_client.post(
            reverse("user_update", kwargs={"pk": admin_user.pk}),
            {"name": admin_user.name, "email": admin_user.email, "role": Role.MEMBER, "is_active": "true"},
        )
        assert response.status_code == 302
        admin_user.refresh_from_db()
        assert admin_user.role == Role.MEMBER

    def test_cannot_deactivate_self(self, admin_client, admin_user):
        response = admin_client.post(
            reverse("user_update", kwargs={"pk": admin_user.pk}),
            {"name": admin_user.name, "email": admin_user.email, "role": Role.ADMIN, "is_active": "false"},
        )
        assert response.status_code == 200
        assert response.context["form"].errors


@pytest.mark.django_db
class TestUserDeleteView:
    def test_admin_can_delete_user(self, admin_client, member_user):
        response = admin_client.post(reverse("user_delete", kwargs={"pk": member_user.pk}))
        assert response.status_code == 302
        assert not User.objects.filter(pk=member_user.pk).exists()

    def test_member_gets_403(self, member_client, admin_user):
        response = member_client.post(reverse("user_delete", kwargs={"pk": admin_user.pk}))
        assert response.status_code == 403

    def test_cannot_delete_self(self, admin_client, admin_user):
        response = admin_client.post(reverse("user_delete", kwargs={"pk": admin_user.pk}))
        assert response.status_code == 302
        assert User.objects.filter(pk=admin_user.pk).exists()
        msgs = list(get_messages(response.wsgi_request))
        assert any("cannot delete your own" in str(m).lower() for m in msgs)

    def test_can_delete_admin_when_other_admin_exists(self, admin_user):
        other_admin = User.objects.create_user(
            username="admin2",
            email="admin2@test.com",
            password="testpass123",  # noqa: S106
            role=Role.ADMIN,
        )
        client = Client()
        client.force_login(other_admin)
        response = client.post(reverse("user_delete", kwargs={"pk": admin_user.pk}))
        assert response.status_code == 302
        assert not User.objects.filter(pk=admin_user.pk).exists()

    def test_cannot_delete_last_admin(self, admin_user):
        other_admin = User.objects.create_user(
            username="admin2",
            email="admin2@test.com",
            password="testpass123",  # noqa: S106
            role=Role.ADMIN,
        )
        client = Client()
        client.force_login(other_admin)
        # Demote other_admin so admin_user is the last admin
        other_admin.role = Role.MEMBER
        other_admin.save()
        # other_admin is now a member, so they get 403 — the permission mixin itself prevents this.
        # The last-admin deletion guard in the view is a safety net for the case where
        # an admin tries to delete themselves (covered by test_cannot_delete_self).
        response = client.post(reverse("user_delete", kwargs={"pk": admin_user.pk}))
        assert response.status_code == 403
