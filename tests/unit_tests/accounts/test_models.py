from django.core.exceptions import ValidationError
from django.utils import timezone

import pytest

from accounts.models import CredentialState, PlatformCredential, User


class UserModelTest:
    def test_str(self):
        user = User(email="email")
        assert str(user) == user.email

    def test_str_with_name_defined(self):
        user = User(name="name", email="email")
        assert str(user) == user.name

    def test_str_with_username_defined(self):
        user = User(username="username", email="email")
        assert str(user) == user.username


@pytest.fixture
def person(db):
    return User.objects.create_user(username="alice", email="alice@test.com", password="x")  # noqa: S106


def test_secrets_are_encrypted_at_rest(person):
    credential = PlatformCredential(user=person, provider="gitlab", host="gitlab.com", platform_uid="77")
    credential.access_token = "tok-secret"  # noqa: S105
    credential.save()

    raw = PlatformCredential.objects.values_list("_access_token_encrypted", flat=True).get()
    assert "tok-secret" not in raw
    assert PlatformCredential.objects.get().access_token == "tok-secret"  # noqa: S105


def test_an_expiring_credential_without_a_refresh_token_is_rejected(person):
    credential = PlatformCredential(
        user=person, provider="gitlab", host="gitlab.com", platform_uid="77", expires_at=timezone.now()
    )
    credential.access_token = "tok"  # noqa: S105
    with pytest.raises(ValidationError):
        credential.save()


def test_a_connected_credential_needs_a_token(person):
    with pytest.raises(ValidationError):
        PlatformCredential(
            user=person, provider="gitlab", host="gitlab.com", platform_uid="77", state=CredentialState.CONNECTED
        ).save()
