"""Real OAuth callback and signup POST flow with a mocked GitLab backchannel."""

import json
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.http import HttpResponse
from django.urls import include, path

import pytest
import requests
from allauth.account.models import EmailAddress
from allauth.core.exceptions import SignupClosedException
from allauth.socialaccount.models import SocialAccount, SocialApp
from allauth.socialaccount.providers.gitlab.provider import GitLabProvider
from pydantic import SecretStr

from accounts.adapter import SocialAccountAdapter
from accounts.forms import UserCreateForm
from accounts.models import Role, User
from accounts.socialaccount import GitHubAppOAuth2Adapter, GitLabServerAwareAdapter
from codebase.base import GitPlatform

urlpatterns = [
    path("accounts/", include("accounts.urls.allauth")),
    path("dashboard/", lambda request: HttpResponse("dashboard"), name="dashboard"),
    path("inactive/", lambda request: HttpResponse("inactive", status=403), name="account_inactive"),
]


@pytest.fixture
def gitlab_oauth(settings):
    settings.ROOT_URLCONF = __name__
    settings.ALLOWED_HOSTS = ["testserver"]
    settings.SESSION_COOKIE_SAMESITE = "Lax"
    settings.SOCIALACCOUNT_STORE_TOKENS = False
    settings.TEMPLATES = [
        {
            "BACKEND": "django.template.backends.django.DjangoTemplates",
            "OPTIONS": {
                "loaders": [
                    (
                        "django.template.loaders.locmem.Loader",
                        {
                            "account/signup_closed.html": "signup closed",
                            "socialaccount/signup.html": "signup form {{ form.errors }}",
                            "socialaccount/authentication_error.html": "authentication failed",
                            "account/account_inactive.html": "inactive",
                        },
                    )
                ],
                "context_processors": ["django.template.context_processors.request"],
            },
        }
    ]
    config = SimpleNamespace(
        auth_login_enabled=True,
        auth_signup_open=False,
        auth_client_id="fixture-client",
        auth_client_secret=SecretStr("fixture-secret"),
        auth_gitlab_url="https://gitlab.example.test",
        auth_gitlab_server_url="",
        cross_project_access_enabled=False,
    )
    profile = {
        "id": 42,
        "username": "gitlab-member",
        "name": "GitLab Member",
        "email": "existing@example.com",
        "confirmed_at": "2026-11-06T12:00:00Z",
    }

    def backchannel(session, method, url, **kwargs):
        response = requests.Response()
        response.status_code = 200
        response.headers["Content-Type"] = "application/json"
        if url.endswith("/oauth/token"):
            payload = {"access_token": "fixture-token", "token_type": "Bearer", "scope": "read_user"}
        elif url == "https://github.com/login/oauth/access_token":
            payload = {"access_token": "fixture-token", "token_type": "Bearer", "scope": "user:email"}
        elif url == "https://api.github.com/user":
            payload = {**profile, "login": profile["username"]}
        elif url == "https://api.github.com/user/emails":
            payload = [{"email": profile["email"], "primary": True, "verified": True}]
        elif url.endswith("/api/v4/user"):
            payload = profile
        else:
            raise AssertionError(f"Unexpected OAuth endpoint: {urlsplit(url).path}")
        response._content = json.dumps(payload).encode()
        return response

    with patch("accounts.adapter.site_settings", config), patch.object(requests.Session, "request", backchannel):
        yield profile, config


@pytest.fixture
def precreated_user(db):
    User.objects.create_user(username="admin", email="admin@example.com", role=Role.ADMIN)
    form = UserCreateForm(data={"name": "Existing", "email": "existing@example.com", "role": Role.MEMBER})
    assert form.is_valid(), form.errors
    user = form.save()
    assert not EmailAddress.objects.filter(user=user).exists()
    return user


def oauth_callback(client, provider="gitlab"):
    response = client.get(f"/accounts/{provider}/login/")
    assert response.status_code == 302
    state = parse_qs(urlsplit(response["Location"]).query)["state"][0]
    return client.get(f"/accounts/{provider}/login/callback/", {"code": "fixture-code", "state": state})


@pytest.mark.django_db
@pytest.mark.parametrize("role", [Role.MEMBER, Role.ADMIN])
def test_confirmed_gitlab_email_logs_into_precreated_user_without_duplicate(
    client, gitlab_oauth, precreated_user, role
):
    precreated_user.role = role
    precreated_user.save(update_fields=["role"])
    count = User.objects.count()
    response = oauth_callback(client)
    assert response.status_code == 302
    assert response["Location"] == "/dashboard/"
    assert client.session["_auth_user_id"] == str(precreated_user.pk)
    assert User.objects.count() == count
    assert SocialAccount.objects.get(provider="gitlab", uid="42").user_id == precreated_user.pk
    precreated_user.refresh_from_db()
    assert precreated_user.email == "existing@example.com"
    assert precreated_user.role == role


@pytest.mark.django_db
def test_existing_email_cannot_unlock_signup_with_an_edited_form_email(client, gitlab_oauth, precreated_user):
    profile, _ = gitlab_oauth
    profile["confirmed_at"] = None
    count = User.objects.count()
    oauth_callback(client)
    # Exercise the stale signup form route too, even if the fixed callback
    # correctly rejects signup before presenting a form.
    provider = GitLabProvider(request=None, app=SocialApp(provider="gitlab", client_id="fixture-client"))
    login = provider.sociallogin_from_response(None, profile)
    session = client.session
    session["socialaccount_sociallogin"] = login.serialize()
    session.save()
    client.post("/accounts/3rdparty/signup/", {"email": "edited@example.com"})
    assert User.objects.count() == count
    assert not User.objects.filter(email="edited@example.com").exists()
    assert not SocialAccount.objects.filter(provider="gitlab", uid="42").exists()
    assert "_auth_user_id" not in client.session


@pytest.mark.django_db
def test_unknown_confirmed_email_is_rejected_when_signup_is_closed(client, gitlab_oauth, precreated_user):
    profile, _ = gitlab_oauth
    profile["email"] = "unregistered@example.com"
    count = User.objects.count()
    oauth_callback(client)
    assert User.objects.count() == count
    assert "_auth_user_id" not in client.session
    assert not SocialAccount.objects.filter(uid="42", provider="gitlab").exists()


@pytest.mark.django_db
def test_existing_gitlab_uid_keeps_its_account_after_email_changes(client, gitlab_oauth, precreated_user):
    SocialAccount.objects.create(user=precreated_user, provider="gitlab", uid="42")
    profile, _ = gitlab_oauth
    profile["email"] = "changed-in-gitlab@example.com"
    oauth_callback(client)
    assert client.session["_auth_user_id"] == str(precreated_user.pk)
    assert User.objects.count() == 2


@pytest.mark.django_db
def test_inactive_precreated_user_cannot_sign_in(client, gitlab_oauth, precreated_user):
    precreated_user.is_active = False
    precreated_user.save(update_fields=["is_active"])
    oauth_callback(client)
    assert "_auth_user_id" not in client.session
    assert User.objects.count() == 2


@pytest.mark.django_db
def test_save_user_rechecks_closed_signup_before_persisting(gitlab_oauth, precreated_user):
    adapter = SocialAccountAdapter()
    sociallogin = SimpleNamespace(
        user=SimpleNamespace(email="existing@example.com"), account=SimpleNamespace(provider="gitlab")
    )
    with (
        patch("allauth.socialaccount.adapter.DefaultSocialAccountAdapter.save_user") as save,
        pytest.raises(SignupClosedException),
    ):
        adapter.save_user(None, sociallogin, form=SimpleNamespace(cleaned_data={"email": "edited@example.com"}))
    save.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize("confirmation", [None, "", "invalid", "2026-11-06T12:00:00", False, 42])
def test_missing_or_invalid_confirmation_never_links_an_email(confirmation, client, gitlab_oauth, precreated_user):
    profile, _ = gitlab_oauth
    profile["confirmed_at"] = confirmation
    oauth_callback(client)
    assert "_auth_user_id" not in client.session
    assert User.objects.count() == 2
    assert not SocialAccount.objects.filter(provider="gitlab", uid="42").exists()


@pytest.mark.django_db
def test_confirmed_primary_email_matches_case_insensitively(client, gitlab_oauth, precreated_user):
    profile, _ = gitlab_oauth
    profile["email"] = "EXISTING@EXAMPLE.COM"
    profile["unconfirmed_email"] = "pending@example.com"
    oauth_callback(client)
    assert client.session["_auth_user_id"] == str(precreated_user.pk)
    assert not User.objects.filter(email="pending@example.com").exists()
    assert User.objects.count() == 2


@pytest.mark.django_db
def test_explicitly_open_signup_still_creates_a_member(client, gitlab_oauth, precreated_user):
    profile, config = gitlab_oauth
    profile["email"] = "new-member@example.com"
    config.auth_signup_open = True
    oauth_callback(client)
    user = User.objects.get(email="new-member@example.com")
    assert user.role == Role.MEMBER
    assert client.session["_auth_user_id"] == str(user.pk)


@pytest.mark.django_db
def test_first_user_bootstrap_still_creates_an_admin(client, gitlab_oauth):
    oauth_callback(client)
    user = User.objects.get(email="existing@example.com")
    assert user.role == Role.ADMIN
    assert client.session["_auth_user_id"] == str(user.pk)


@pytest.mark.django_db
def test_github_verified_email_still_links_when_signup_is_closed(client, gitlab_oauth, precreated_user):
    with patch("accounts.adapter.codebase_settings.CLIENT", GitPlatform.GITHUB):
        oauth_callback(client, provider="github")
    assert client.session["_auth_user_id"] == str(precreated_user.pk)
    assert SocialAccount.objects.get(provider="github", uid="42").user_id == precreated_user.pk
    assert User.objects.count() == 2


def test_gitlab_confirmation_applies_only_to_its_matching_primary_address():
    primary = EmailAddress(email="existing@example.com", primary=True, verified=False)
    other = EmailAddress(email="another@example.com", primary=True, verified=False)
    secondary = EmailAddress(email="existing@example.com", primary=False, verified=False)
    login = SimpleNamespace(
        account=SimpleNamespace(extra_data={"email": "existing@example.com", "confirmed_at": "2026-11-06T12:00:00Z"}),
        email_addresses=[primary, other, secondary],
    )
    with patch("allauth.socialaccount.providers.gitlab.views.GitLabOAuth2Adapter.complete_login", return_value=login):
        assert GitLabServerAwareAdapter(None).complete_login(None, None, None) is login
    assert primary.verified
    assert not other.verified
    assert not secondary.verified


def test_github_enterprise_never_posts_the_secret_to_github_com(settings, monkeypatch):
    monkeypatch.setattr("accounts.socialaccount.codebase_settings.GITHUB_URL", "https://ghe.example.com")
    assert GitHubAppOAuth2Adapter.access_token_endpoint() == "https://ghe.example.com/login/oauth/access_token"
