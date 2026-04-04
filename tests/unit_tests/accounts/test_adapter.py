from unittest.mock import Mock

import pytest

from accounts.adapter import SocialAccountAdapter
from accounts.models import Role, User


@pytest.fixture
def adapter():
    return SocialAccountAdapter()


@pytest.fixture
def user_in_db(db):
    return User.objects.create_user(
        username="existing",
        email="existing@test.com",
        password="testpass123",  # noqa: S106
        role=Role.MEMBER,
    )


def _make_sociallogin(email: str) -> Mock:
    sociallogin = Mock()
    sociallogin.user.email = email
    return sociallogin


@pytest.mark.django_db
class TestSocialAccountAdapterSignup:
    def test_allows_signup_when_email_exists(self, adapter, user_in_db):
        sociallogin = _make_sociallogin("existing@test.com")
        assert adapter.is_open_for_signup(Mock(), sociallogin) is True

    def test_allows_signup_case_insensitive(self, adapter, user_in_db):
        sociallogin = _make_sociallogin("EXISTING@TEST.COM")
        assert adapter.is_open_for_signup(Mock(), sociallogin) is True

    def test_denies_signup_when_email_not_in_db(self, adapter, user_in_db):
        sociallogin = _make_sociallogin("unknown@test.com")
        assert adapter.is_open_for_signup(Mock(), sociallogin) is False

    def test_denies_signup_when_email_is_empty(self, adapter, user_in_db):
        sociallogin = _make_sociallogin("")
        assert adapter.is_open_for_signup(Mock(), sociallogin) is False

    def test_denies_signup_when_email_is_none(self, adapter, user_in_db):
        sociallogin = _make_sociallogin(None)
        assert adapter.is_open_for_signup(Mock(), sociallogin) is False
