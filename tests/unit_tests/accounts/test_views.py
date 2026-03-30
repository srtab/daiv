from django.contrib.messages import get_messages
from django.test import Client
from django.urls import reverse

import pytest

from accounts.models import APIKey, User


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
