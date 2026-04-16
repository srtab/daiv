from unittest.mock import Mock, patch

import pytest
from pydantic import SecretStr

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


class TestSocialAccountAdapterListApps:
    @pytest.fixture(autouse=True)
    def _clear_site_config_cache(self, db):
        from django.core.cache import cache

        cache.clear()

    def test_returns_empty_when_codebase_client_is_swe(self, adapter):
        with (
            patch("accounts.adapter.codebase_settings") as mock_codebase,
            patch("accounts.adapter.site_settings") as mock_site,
        ):
            from codebase.base import GitPlatform

            mock_codebase.CLIENT = GitPlatform.SWE
            mock_site.auth_client_id = "some-id"
            mock_site.auth_client_secret = SecretStr("some-secret")
            result = adapter.list_apps(Mock())
            assert result == []

    def test_returns_empty_when_client_id_is_none(self, adapter):
        with (
            patch("accounts.adapter.codebase_settings") as mock_codebase,
            patch("accounts.adapter.site_settings") as mock_site,
        ):
            from codebase.base import GitPlatform

            mock_codebase.CLIENT = GitPlatform.GITLAB
            mock_site.auth_client_id = None
            mock_site.auth_client_secret = SecretStr("some-secret")
            result = adapter.list_apps(Mock())
            assert result == []

    def test_returns_empty_when_secret_is_none(self, adapter):
        with (
            patch("accounts.adapter.codebase_settings") as mock_codebase,
            patch("accounts.adapter.site_settings") as mock_site,
        ):
            from codebase.base import GitPlatform

            mock_codebase.CLIENT = GitPlatform.GITLAB
            mock_site.auth_client_id = "some-id"
            mock_site.auth_client_secret = None
            result = adapter.list_apps(Mock())
            assert result == []

    def test_returns_empty_for_non_matching_provider(self, adapter):
        with (
            patch("accounts.adapter.codebase_settings") as mock_codebase,
            patch("accounts.adapter.site_settings") as mock_site,
        ):
            from codebase.base import GitPlatform

            mock_codebase.CLIENT = GitPlatform.GITLAB
            mock_site.auth_client_id = "some-id"
            mock_site.auth_client_secret = SecretStr("some-secret")
            result = adapter.list_apps(Mock(), provider="github")
            assert result == []

    def test_returns_app_for_matching_gitlab(self, adapter):
        with (
            patch("accounts.adapter.codebase_settings") as mock_codebase,
            patch("accounts.adapter.site_settings") as mock_site,
        ):
            from codebase.base import GitPlatform

            mock_codebase.CLIENT = GitPlatform.GITLAB
            mock_site.auth_client_id = "gl-client-id"
            mock_site.auth_client_secret = SecretStr("gl-secret")
            mock_site.auth_gitlab_url = "https://gitlab.example.com"
            mock_site.auth_gitlab_server_url = "http://gitlab:8080"
            result = adapter.list_apps(Mock())
            assert len(result) == 1
            app = result[0]
            assert app.provider == "gitlab"
            assert app.client_id == "gl-client-id"
            assert app.secret == "gl-secret"  # noqa: S105
            assert app.settings["gitlab_url"] == "https://gitlab.example.com"
            assert app.settings["gitlab_server_url"] == "http://gitlab:8080"

    def test_returns_app_for_matching_github(self, adapter):
        with (
            patch("accounts.adapter.codebase_settings") as mock_codebase,
            patch("accounts.adapter.site_settings") as mock_site,
        ):
            from codebase.base import GitPlatform

            mock_codebase.CLIENT = GitPlatform.GITHUB
            mock_site.auth_client_id = "gh-client-id"
            mock_site.auth_client_secret = SecretStr("gh-secret")
            result = adapter.list_apps(Mock())
            assert len(result) == 1
            app = result[0]
            assert app.provider == "github"
            assert app.client_id == "gh-client-id"
            assert app.secret == "gh-secret"  # noqa: S105
            assert app.settings == {}

    def test_handles_plain_string_secret(self, adapter):
        with (
            patch("accounts.adapter.codebase_settings") as mock_codebase,
            patch("accounts.adapter.site_settings") as mock_site,
        ):
            from codebase.base import GitPlatform

            mock_codebase.CLIENT = GitPlatform.GITHUB
            mock_site.auth_client_id = "gh-client-id"
            mock_site.auth_client_secret = "plain-secret"  # noqa: S105
            result = adapter.list_apps(Mock())
            assert len(result) == 1
            assert result[0].secret == "plain-secret"  # noqa: S105

    def test_gitlab_defaults_url_when_none(self, adapter):
        with (
            patch("accounts.adapter.codebase_settings") as mock_codebase,
            patch("accounts.adapter.site_settings") as mock_site,
        ):
            from codebase.base import GitPlatform

            mock_codebase.CLIENT = GitPlatform.GITLAB
            mock_site.auth_client_id = "gl-client-id"
            mock_site.auth_client_secret = SecretStr("gl-secret")
            mock_site.auth_gitlab_url = None
            mock_site.auth_gitlab_server_url = None
            result = adapter.list_apps(Mock())
            assert result[0].settings["gitlab_url"] == "https://gitlab.com"
            assert result[0].settings["gitlab_server_url"] == ""
