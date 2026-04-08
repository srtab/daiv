from unittest.mock import Mock, patch

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


def _make_sociallogin(email: str | None) -> Mock:
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

    def test_allows_first_signup_on_fresh_install(self, adapter, db):
        assert not User.objects.exists()
        sociallogin = _make_sociallogin("first@test.com")
        assert adapter.is_open_for_signup(Mock(), sociallogin) is True


@pytest.mark.django_db
class TestSocialAccountAdapterSaveUser:
    def _create_user_via_save(self, adapter, email, username):
        """Helper that mocks super().save_user() to create a real user, then runs our save_user."""
        user = User(username=username, email=email)
        user.set_unusable_password()
        sociallogin = Mock()

        with patch.object(SocialAccountAdapter.__bases__[0], "save_user", return_value=user) as mock_super:
            mock_super.side_effect = lambda req, sl, form=None: User.objects.create(
                username=username,
                email=email,
                password="!",  # noqa: S106
            )
            return adapter.save_user(Mock(), sociallogin)

    def test_first_user_gets_admin_role(self, adapter, db):
        user = self._create_user_via_save(adapter, "first@test.com", "first")
        user.refresh_from_db()
        assert user.role == Role.ADMIN

    def test_second_user_keeps_member_role(self, adapter, db):
        # Create first user (gets admin)
        self._create_user_via_save(adapter, "first@test.com", "first")
        # Create second user (stays member)
        user2 = self._create_user_via_save(adapter, "second@test.com", "second")
        user2.refresh_from_db()
        assert user2.role == Role.MEMBER

    def test_promotes_when_no_admin_exists(self, adapter, db):
        # Create a member user directly (simulating a user created by admin but no admin exists)
        User.objects.create_user(username="member", email="member@test.com", password="!", role=Role.MEMBER)  # noqa: S106
        # Next user via social login should get admin since no admin exists
        user = self._create_user_via_save(adapter, "new@test.com", "new")
        user.refresh_from_db()
        assert user.role == Role.ADMIN
