from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, Mock, patch

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.utils import timezone

import pytest
from asgiref.sync import sync_to_async

from accounts import credentials
from accounts.credentials import CredentialReason, ResolvedCredential
from accounts.models import CredentialState, PlatformCredential, User
from codebase.base import GitPlatform

pytestmark = pytest.mark.django_db(transaction=True)

HOST = "gitlab.com"


@pytest.fixture(autouse=True)
def _capability_on():
    """The resolution order short-circuits on the toggle; every test here is past that gate."""
    with patch("accounts.credentials.site_settings") as settings_mock:
        settings_mock.cross_project_access_enabled = True
        settings_mock.cross_project_webhook_runs_enabled = True
        settings_mock.auth_client_id = "client-id"
        settings_mock.auth_client_secret = "client-secret"  # noqa: S105
        settings_mock.auth_gitlab_url = "https://gitlab.com"
        settings_mock.auth_gitlab_server_url = None
        yield settings_mock


@pytest.fixture(autouse=True)
def _clear_token_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def person(db):
    return User.objects.create_user(username="ada", email="ada@test.com", password="pw")  # noqa: S106


def _credential(user, **overrides) -> PlatformCredential:
    fields = {
        "user": user,
        "provider": GitPlatform.GITLAB.value,
        "host": HOST,
        "platform_uid": "77",
        "access_token": "tok-live",
        "refresh_token": "refresh-live",
        "expires_at": timezone.now() + timedelta(hours=2),
        "scopes": ["read_user", "api"],
        "state": CredentialState.CONNECTED,
    }
    fields.update(overrides)
    # The encrypted columns are reached through descriptors, not model fields, so they cannot be
    # passed to the constructor — mirroring how ``credentials.store`` writes them.
    secrets = {name: fields.pop(name) for name in ("access_token", "refresh_token")}
    credential = PlatformCredential(**fields)
    for name, value in secrets.items():
        setattr(credential, name, value)
    credential.save()
    return credential


class TestModelValidation:
    def test_expiring_credential_without_refresh_token_is_rejected(self, person):
        with pytest.raises(ValidationError):
            _credential(person, refresh_token=None)

    def test_non_expiring_credential_without_refresh_token_is_allowed(self, person):
        credential = _credential(person, refresh_token=None, expires_at=None)
        assert credential.pk is not None

    def test_connected_credential_without_access_token_is_rejected(self, person):
        with pytest.raises(ValidationError):
            _credential(person, access_token=None, expires_at=None, refresh_token=None)

    def test_secrets_round_trip_through_encryption(self, person):
        _credential(person)
        reloaded = PlatformCredential.objects.get(user=person)
        assert reloaded.access_token == "tok-live"  # noqa: S105
        assert reloaded._access_token_encrypted != "tok-live"  # noqa: S105


class TestResolution:
    async def test_disabled_capability_refuses_before_any_lookup(self, person, _capability_on):
        _capability_on.cross_project_access_enabled = False
        await _credential_async(person)
        result = await credentials.aresolve_access_token(
            acting_user_id=person.pk, provider=GitPlatform.GITLAB, host=HOST
        )
        assert result.reason is CredentialReason.DISABLED
        assert result.token is None

    async def test_no_acting_user(self):
        result = await credentials.aresolve_access_token(acting_user_id=None, provider=GitPlatform.GITLAB, host=HOST)
        assert result.reason is CredentialReason.NO_ACTING_USER

    async def test_no_credential_row(self, person):
        result = await credentials.aresolve_access_token(
            acting_user_id=person.pk, provider=GitPlatform.GITLAB, host=HOST
        )
        assert result.reason is CredentialReason.NO_CREDENTIAL

    async def test_connected_credential_returns_token(self, person):
        await _credential_async(person)
        result = await credentials.aresolve_access_token(
            acting_user_id=person.pk, provider=GitPlatform.GITLAB, host=HOST
        )
        assert result.token == "tok-live"  # noqa: S105
        assert result.scopes == ("read_user", "api")

    async def test_expired_state_names_its_own_cause(self, person):
        await _credential_async(
            person, state=CredentialState.EXPIRED, access_token=None, expires_at=None, refresh_token=None
        )
        result = await credentials.aresolve_access_token(
            acting_user_id=person.pk, provider=GitPlatform.GITLAB, host=HOST
        )
        assert result.reason is CredentialReason.EXPIRED

    async def test_revoked_state_names_its_own_cause(self, person):
        await _credential_async(
            person, state=CredentialState.REVOKED, access_token=None, expires_at=None, refresh_token=None
        )
        result = await credentials.aresolve_access_token(
            acting_user_id=person.pk, provider=GitPlatform.GITLAB, host=HOST
        )
        assert result.reason is CredentialReason.REVOKED

    async def test_platform_uid_lookup_resolves_the_owner(self, person):
        await _credential_async(person, platform_uid="77")
        result = await credentials.aresolve_access_token(provider=GitPlatform.GITLAB, host=HOST, platform_uid=77)
        assert result.ok
        assert result.user_id == person.pk

    async def test_platform_uid_lookup_never_falls_back_to_username(self, person):
        await _credential_async(person, platform_uid="77")
        result = await credentials.aresolve_access_token(provider=GitPlatform.GITLAB, host=HOST, platform_uid="999")
        assert result.reason == CredentialReason.NO_CREDENTIAL

    async def test_naming_both_identities_is_a_programming_error(self, person):
        with pytest.raises(ValueError, match="not both"):
            await credentials.aresolve_access_token(
                provider=GitPlatform.GITLAB, host=HOST, acting_user_id=person.pk, platform_uid="77"
            )

    async def test_a_deactivated_owner_is_refused(self, person):
        await _credential_async(person, platform_uid="77")
        person.is_active = False
        await person.asave(update_fields=["is_active"])
        result = await credentials.aresolve_access_token(provider=GitPlatform.GITLAB, host=HOST, platform_uid="77")
        assert result.reason == CredentialReason.NO_CREDENTIAL

    async def test_a_uid_held_by_two_accounts_is_refused(self, person):
        other = await User.objects.acreate(username="other", email="other@test.com")
        await _credential_async(person, platform_uid="77")
        await _credential_async(other, platform_uid="77")
        result = await credentials.aresolve_access_token(provider=GitPlatform.GITLAB, host=HOST, platform_uid="77")
        assert result.reason == CredentialReason.NO_CREDENTIAL

    async def test_platform_uid_refused_when_webhook_runs_disabled(self, person):
        await _credential_async(person, platform_uid="77")
        with patch("accounts.credentials._awebhook_runs_enabled", AsyncMock(return_value=False)):
            result = await credentials.aresolve_access_token(provider=GitPlatform.GITLAB, host=HOST, platform_uid="77")
        assert result.reason == CredentialReason.WEBHOOK_RUNS_DISABLED

    async def test_signed_in_runs_ignore_the_webhook_runs_toggle(self, person, _capability_on):
        _capability_on.cross_project_webhook_runs_enabled = False
        await _credential_async(person)
        result = await credentials.aresolve_access_token(
            provider=GitPlatform.GITLAB, host=HOST, acting_user_id=person.pk
        )
        assert result.ok

    async def test_failures_after_lookup_name_the_owner(self, person):
        await _credential_async(person, platform_uid="77", state=CredentialState.REVOKED, access_token=None)
        result = await credentials.aresolve_access_token(provider=GitPlatform.GITLAB, host=HOST, platform_uid="77")
        assert (result.reason, result.user_id) == (CredentialReason.REVOKED, person.pk)


class TestRefresh:
    async def test_refresh_rotates_both_tokens_and_expiry_together(self, person):
        credential = await _credential_async(person, expires_at=timezone.now() + timedelta(seconds=60))
        payload = {
            "access_token": "tok-new",
            "refresh_token": "refresh-new",
            "expires_in": 7200,
            "scope": "read_user api",
        }
        with patch("accounts.credentials._arequest_refresh", return_value=payload):
            result = await credentials.aresolve_access_token(
                acting_user_id=person.pk, provider=GitPlatform.GITLAB, host=HOST
            )

        assert result.token == "tok-new"  # noqa: S105
        reloaded = await PlatformCredential.objects.aget(pk=credential.pk)
        # GitLab rotates the refresh token on every use: a partial write leaves a credential that
        # can never renew again.
        assert reloaded.refresh_token == "refresh-new"  # noqa: S105
        assert reloaded.expires_at > timezone.now() + timedelta(hours=1)
        assert reloaded.state == CredentialState.CONNECTED

    async def test_failed_refresh_lands_in_expired_and_clears_the_secret(self, person):
        credential = await _credential_async(person, expires_at=timezone.now() + timedelta(seconds=60))
        with patch("accounts.credentials._arequest_refresh", return_value=credentials.RefreshFailure.TERMINAL):
            result = await credentials.aresolve_access_token(
                acting_user_id=person.pk, provider=GitPlatform.GITLAB, host=HOST
            )

        assert result.reason is CredentialReason.EXPIRED
        reloaded = await PlatformCredential.objects.aget(pk=credential.pk)
        assert reloaded.state == CredentialState.EXPIRED
        assert reloaded.access_token is None
        assert reloaded.refresh_token is None

    async def test_token_outside_the_margin_is_not_refreshed(self, person):
        await _credential_async(person, expires_at=timezone.now() + timedelta(hours=2))
        with patch("accounts.credentials._arequest_refresh") as request_mock:
            result = await credentials.aresolve_access_token(
                acting_user_id=person.pk, provider=GitPlatform.GITLAB, host=HOST
            )
        assert result.token == "tok-live"  # noqa: S105
        request_mock.assert_not_called()

    async def test_non_expiring_credential_never_refreshes(self, person):
        await _credential_async(person, expires_at=None, refresh_token=None)
        with patch("accounts.credentials._arequest_refresh") as request_mock:
            result = await credentials.aresolve_access_token(
                acting_user_id=person.pk, provider=GitPlatform.GITLAB, host=HOST
            )
        assert result.token == "tok-live"  # noqa: S105
        request_mock.assert_not_called()


class TestRevoke:
    async def test_revoke_clears_both_secrets(self, person):
        credential = await _credential_async(person)
        changed = await credentials.arevoke(user_id=person.pk, provider=GitPlatform.GITLAB, host=HOST)
        assert changed is True

        reloaded = await PlatformCredential.objects.aget(pk=credential.pk)
        assert reloaded.state == CredentialState.REVOKED
        assert reloaded.access_token is None
        assert reloaded.refresh_token is None

    async def test_revoke_drops_the_cached_token(self, person):
        await _credential_async(person)
        await credentials.aresolve_access_token(acting_user_id=person.pk, provider=GitPlatform.GITLAB, host=HOST)
        key = credentials.token_cache_key(person.pk, GitPlatform.GITLAB, HOST)
        assert await cache.aget(key) == "tok-live"

        await credentials.arevoke(user_id=person.pk, provider=GitPlatform.GITLAB, host=HOST)
        assert await cache.aget(key) is None

    async def test_revoke_on_a_missing_row_reports_no_change(self, person):
        assert await credentials.arevoke(user_id=person.pk, provider=GitPlatform.GITLAB, host=HOST) is False


class TestStatusNeverLeaksTheSecret:
    async def test_status_reports_state_expiry_and_scopes(self, person):
        credential = await _credential_async(person)
        status = await credentials.astatus(user_id=person.pk, provider=GitPlatform.GITLAB, host=HOST)
        assert status.connected is True
        assert status.state == CredentialState.CONNECTED
        assert status.expires_at == credential.expires_at
        assert status.scopes == ("read_user", "api")
        assert "tok-live" not in repr(status)

    async def test_status_without_a_credential(self, person):
        status = await credentials.astatus(user_id=person.pk, provider=GitPlatform.GITLAB, host=HOST)
        assert status.connected is False
        assert status.state is None


class TestCacheKeyIsIdentityDerived:
    """A resumed thread whose acting person differs must not read the previous person's token,
    so the cache key may never carry the thread."""

    def test_key_is_derived_from_the_identity_not_the_thread(self):
        key = credentials.token_cache_key(7, GitPlatform.GITLAB, HOST)
        assert key == f"{credentials.TOKEN_CACHE_PREFIX}:7:gitlab:{HOST}"

    def test_two_identities_get_different_keys(self):
        assert credentials.token_cache_key(1, GitPlatform.GITLAB, HOST) != credentials.token_cache_key(
            2, GitPlatform.GITLAB, HOST
        )

    async def test_two_identities_on_one_thread_never_share_a_token(self, db):
        """The tool call carries a ``thread_id`` but the cache does not: resolving for a second
        person on the same thread returns that person's own token, never the first's."""
        first = await User.objects.acreate_user(username="first", email="first@test.com", password="pw")  # noqa: S106
        second = await User.objects.acreate_user(username="second", email="second@test.com", password="pw")  # noqa: S106
        await _credential_async(first, access_token="tok-first", platform_uid="1")  # noqa: S106
        await _credential_async(second, access_token="tok-second", platform_uid="2")  # noqa: S106

        first_result = await credentials.aresolve_access_token(
            acting_user_id=first.pk, provider=GitPlatform.GITLAB, host=HOST
        )
        second_result = await credentials.aresolve_access_token(
            acting_user_id=second.pk, provider=GitPlatform.GITLAB, host=HOST
        )

        assert first_result.token == "tok-first"  # noqa: S105
        assert second_result.token == "tok-second"  # noqa: S105

    async def test_cache_ttl_never_outlives_the_token(self, person):
        await _credential_async(person, expires_at=timezone.now() + timedelta(seconds=400))
        await credentials.aresolve_access_token(acting_user_id=person.pk, provider=GitPlatform.GITLAB, host=HOST)
        # 400s of life minus the 300s refresh margin leaves 100s of cacheable life.
        assert await cache.aget(credentials.token_cache_key(person.pk, GitPlatform.GITLAB, HOST)) == "tok-live"


class TestNoSecretInRepr:
    def test_resolved_credential_repr_hides_the_token(self):
        assert "tok" not in repr(ResolvedCredential(token="tok-live"))  # noqa: S106


async def _credential_async(user, **overrides):
    from asgiref.sync import sync_to_async

    return await sync_to_async(_credential)(user, **overrides)


class TestPreExistingNarrowAuthorisation:
    """A ``read_user``-only grant predates this feature: only the cross-project call is refused,
    and the refusal names re-authorisation as the next step."""

    async def test_a_read_user_only_grant_is_insufficient(self, person):
        await _credential_async(person, scopes=["read_user"])
        result = await credentials.aresolve_access_token(
            acting_user_id=person.pk, provider=GitPlatform.GITLAB, host=HOST
        )
        assert result.reason is CredentialReason.INSUFFICIENT_SCOPE
        assert result.token is None
        assert result.scopes == ("read_user",)

    @pytest.mark.parametrize("scope", ["api", "read_api"])
    async def test_either_widened_scope_is_enough(self, person, scope):
        await _credential_async(person, scopes=["read_user", scope])
        result = await credentials.aresolve_access_token(
            acting_user_id=person.pk, provider=GitPlatform.GITLAB, host=HOST
        )
        assert result.token == "tok-live"  # noqa: S105

    async def test_an_unreported_grant_is_attempted_rather_than_pre_emptively_refused(self, person):
        """An empty list means the platform did not say what it granted; its own check is the
        real enforcement point."""
        await _credential_async(person, scopes=[])
        result = await credentials.aresolve_access_token(
            acting_user_id=person.pk, provider=GitPlatform.GITLAB, host=HOST
        )
        assert result.token == "tok-live"  # noqa: S105

    def test_a_github_app_grant_is_never_judged_on_scope(self):
        """GitHub Apps ignore the OAuth scope parameter entirely."""
        assert credentials.scopes_permit_cross_project(GitPlatform.GITHUB, []) is True
        assert credentials.scopes_permit_cross_project(GitPlatform.GITHUB, ["user:email"]) is True

    async def test_the_status_page_flags_the_narrow_grant(self, person):
        await _credential_async(person, scopes=["read_user"])
        status = await credentials.astatus(user_id=person.pk, provider=GitPlatform.GITLAB, host=HOST)
        assert status.connected is True
        assert status.permits_cross_project is False


class TestStore:
    """The login path's only write. Exercised here because the allauth flow that calls it is
    framework behaviour the suite deliberately does not re-test."""

    def test_store_persists_a_connected_grant(self, person):
        credentials.store(
            user_id=person.pk,
            provider=GitPlatform.GITLAB,
            host=HOST,
            platform_uid="77",
            access_token="tok-fresh",  # noqa: S106
            refresh_token="refresh-fresh",  # noqa: S106
            expires_at=timezone.now() + timedelta(hours=2),
            scopes=["read_user", "api"],
        )

        credential = PlatformCredential.objects.get(user=person)
        assert credential.state == CredentialState.CONNECTED
        assert credential.access_token == "tok-fresh"  # noqa: S105
        assert credential.platform_uid == "77"
        assert credential.scopes == ["read_user", "api"]

    def test_a_second_sign_in_replaces_the_grant_rather_than_duplicating_it(self, person):
        for token in ("tok-first", "tok-second"):
            credentials.store(
                user_id=person.pk,
                provider=GitPlatform.GITLAB,
                host=HOST,
                platform_uid="77",
                access_token=token,
                scopes=["read_user", "api"],
            )

        assert PlatformCredential.objects.filter(user=person).count() == 1
        assert PlatformCredential.objects.get(user=person).access_token == "tok-second"  # noqa: S105

    def test_signing_in_again_does_not_undo_a_revoke(self, person):
        """Withdrawing has to outlast the next sign-in: the platform still remembers the
        authorisation, so the OAuth round trip never asks the person again."""
        _credential(person)
        credentials.revoke(user_id=person.pk, provider=GitPlatform.GITLAB, host=HOST)

        credentials.store(
            user_id=person.pk,
            provider=GitPlatform.GITLAB,
            host=HOST,
            platform_uid="77",
            access_token="tok-again",  # noqa: S106
            scopes=["read_user", "api"],
        )

        credential = PlatformCredential.objects.get(user=person)
        assert credential.state == CredentialState.REVOKED
        assert credential.access_token is None

    def test_clear_revoked_lets_an_explicit_re_authorisation_through(self, person):
        _credential(person)
        credentials.revoke(user_id=person.pk, provider=GitPlatform.GITLAB, host=HOST)

        assert credentials.clear_revoked(user_id=person.pk, provider=GitPlatform.GITLAB, host=HOST) is True

        credentials.store(
            user_id=person.pk,
            provider=GitPlatform.GITLAB,
            host=HOST,
            platform_uid="77",
            access_token="tok-again",  # noqa: S106
            scopes=["read_user", "api"],
        )

        credential = PlatformCredential.objects.get(user=person)
        assert credential.state == CredentialState.CONNECTED
        assert credential.access_token == "tok-again"  # noqa: S105

    def test_clear_revoked_leaves_a_live_grant_alone(self, person):
        _credential(person)
        assert credentials.clear_revoked(user_id=person.pk, provider=GitPlatform.GITLAB, host=HOST) is False
        assert PlatformCredential.objects.filter(user=person).exists()

    def test_an_expiry_with_no_refresh_token_is_stored_as_expired(self, person):
        """Dropping the expiry would blind ``_needs_refresh``: every call after the platform's own
        expiry would 401 while the settings page still read Connected."""
        credentials.store(
            user_id=person.pk,
            provider=GitPlatform.GITHUB,
            host="github.com",
            platform_uid="99",
            access_token="tok-gh",  # noqa: S106
            refresh_token=None,
            expires_at=timezone.now() + timedelta(hours=8),
            scopes=[],
        )

        credential = PlatformCredential.objects.get(user=person, provider="github")
        assert credential.expires_at is None
        assert credential.state == CredentialState.EXPIRED

    def test_store_drops_a_stale_cached_token(self, person):
        key = credentials.token_cache_key(person.pk, GitPlatform.GITLAB, HOST)
        cache.set(key, "tok-stale", timeout=300)

        credentials.store(
            user_id=person.pk,
            provider=GitPlatform.GITLAB,
            host=HOST,
            platform_uid="77",
            access_token="tok-fresh",  # noqa: S106
            scopes=["api"],
        )

        assert cache.get(key) is None


class TestRefreshFailuresAreClassified:
    """A renewal that failed for a transport reason must not destroy the grant; only the
    platform refusing the grant itself is terminal."""

    async def test_a_transport_failure_leaves_the_grant_intact(self, person):
        credential = await _credential_async(person, expires_at=timezone.now() + timedelta(seconds=60))
        with patch("accounts.credentials._arequest_refresh", return_value=credentials.RefreshFailure.TRANSIENT):
            result = await credentials.aresolve_access_token(
                acting_user_id=person.pk, provider=GitPlatform.GITLAB, host=HOST
            )

        assert result.reason is CredentialReason.REFRESH_FAILED
        reloaded = await PlatformCredential.objects.aget(pk=credential.pk)
        assert reloaded.state == CredentialState.CONNECTED
        assert reloaded.refresh_token == "refresh-live"  # noqa: S105

    async def test_the_platform_refusing_the_grant_expires_it(self, person):
        credential = await _credential_async(person, expires_at=timezone.now() + timedelta(seconds=60))
        with patch("accounts.credentials._arequest_refresh", return_value=credentials.RefreshFailure.TERMINAL):
            result = await credentials.aresolve_access_token(
                acting_user_id=person.pk, provider=GitPlatform.GITLAB, host=HOST
            )

        assert result.reason is CredentialReason.EXPIRED
        reloaded = await PlatformCredential.objects.aget(pk=credential.pk)
        assert reloaded.state == CredentialState.EXPIRED
        assert reloaded.refresh_token is None

    @pytest.mark.parametrize(
        ("status_code", "body", "expected"),
        [
            (500, {"error": "server_error"}, "TRANSIENT"),
            (502, {}, "TRANSIENT"),
            (429, {"error": "rate_limited"}, "TRANSIENT"),
            (400, {"error": "invalid_grant"}, "TERMINAL"),
            (401, {"error": "invalid_grant"}, "TERMINAL"),
            (200, {"error": "invalid_grant"}, "TERMINAL"),
            (200, {"error": "temporarily_unavailable"}, "TRANSIENT"),
        ],
    )
    async def test_only_an_invalid_grant_is_terminal(self, person, status_code, body, expected):
        credential = await _credential_async(person, expires_at=timezone.now() + timedelta(seconds=60))
        response = Mock(status_code=status_code)
        response.json.return_value = body
        client = AsyncMock()
        client.post.return_value = response
        with patch("accounts.credentials.httpx.AsyncClient") as http_mock:
            http_mock.return_value.__aenter__.return_value = client
            outcome = await credentials._arequest_refresh(credential, "refresh-live")  # noqa: S106

        assert outcome is getattr(credentials.RefreshFailure, expected)

    async def test_a_non_json_body_is_transient(self, person):
        credential = await _credential_async(person, expires_at=timezone.now() + timedelta(seconds=60))
        response = Mock(status_code=200)
        response.json.side_effect = ValueError("not json")
        client = AsyncMock()
        client.post.return_value = response
        with patch("accounts.credentials.httpx.AsyncClient") as http_mock:
            http_mock.return_value.__aenter__.return_value = client
            outcome = await credentials._arequest_refresh(credential, "refresh-live")  # noqa: S106

        assert outcome is credentials.RefreshFailure.TRANSIENT

    async def test_a_missing_oauth_client_is_transient_not_terminal(self, person, _capability_on):
        """A deployment that never configured the OAuth client must not lose every credential
        it touches — that is a misconfiguration, not a withdrawn grant."""
        credential = await _credential_async(person, expires_at=timezone.now() + timedelta(seconds=60))
        _capability_on.auth_client_id = None
        outcome = await credentials._arequest_refresh(credential, "refresh-live")  # noqa: S106
        assert outcome is credentials.RefreshFailure.TRANSIENT


class TestConcurrentRefreshDoesNotClobberTheWinner:
    """The loser of two parallel refreshes is told ``invalid_grant`` for a token the winner already
    replaced; expiring on that would clear the grant the winner just renewed."""

    async def test_a_losing_refresh_returns_the_winners_token(self, person):
        credential = await _credential_async(person, expires_at=timezone.now() + timedelta(seconds=60))

        async def _winner_lands_first(cred, refresh_token):
            await sync_to_async(credentials._persist_refresh)(
                cred.pk,
                seen_modified=cred.modified,
                access_token="tok-winner",  # noqa: S106
                refresh_token="refresh-winner",  # noqa: S106
                expires_at=timezone.now() + timedelta(hours=2),
                scopes=["api"],
            )
            return credentials.RefreshFailure.TERMINAL

        with patch("accounts.credentials._arequest_refresh", side_effect=_winner_lands_first):
            result = await credentials.aresolve_access_token(
                acting_user_id=person.pk, provider=GitPlatform.GITLAB, host=HOST
            )

        assert result.token == "tok-winner"  # noqa: S105
        reloaded = await PlatformCredential.objects.aget(pk=credential.pk)
        assert reloaded.state == CredentialState.CONNECTED


class TestARefreshNeverUndoesAChangeThatLandedMidFlight:
    """The refresh request can take seconds; a Disconnect or re-authorisation that lands meanwhile
    must survive it."""

    async def test_a_revoke_during_the_request_is_not_undone(self, person):
        credential = await _credential_async(person, expires_at=timezone.now() + timedelta(seconds=60))

        async def _revoked_while_in_flight(cred, refresh_token):
            await credentials.arevoke(user_id=person.pk, provider=GitPlatform.GITLAB, host=HOST)
            return {"access_token": "tok-late", "refresh_token": "refresh-late", "expires_in": 7200}

        with patch("accounts.credentials._arequest_refresh", side_effect=_revoked_while_in_flight):
            result = await credentials.aresolve_access_token(
                acting_user_id=person.pk, provider=GitPlatform.GITLAB, host=HOST
            )

        assert result.token is None
        assert result.reason is CredentialReason.REVOKED
        reloaded = await PlatformCredential.objects.aget(pk=credential.pk)
        assert reloaded.state == CredentialState.REVOKED
        assert reloaded.access_token is None
        assert reloaded.refresh_token is None

    async def test_a_revoke_during_a_refused_refresh_stays_revoked(self, person):
        credential = await _credential_async(person, expires_at=timezone.now() + timedelta(seconds=60))

        async def _revoked_while_in_flight(cred, refresh_token):
            await credentials.arevoke(user_id=person.pk, provider=GitPlatform.GITLAB, host=HOST)
            return credentials.RefreshFailure.TERMINAL

        with patch("accounts.credentials._arequest_refresh", side_effect=_revoked_while_in_flight):
            result = await credentials.aresolve_access_token(
                acting_user_id=person.pk, provider=GitPlatform.GITLAB, host=HOST
            )

        assert result.reason is CredentialReason.REVOKED
        reloaded = await PlatformCredential.objects.aget(pk=credential.pk)
        assert reloaded.state == CredentialState.REVOKED

    async def test_a_reauthorisation_during_the_request_keeps_its_token_and_scopes(self, person):
        await _credential_async(person, scopes=["read_api"], expires_at=timezone.now() + timedelta(seconds=60))

        async def _reauthorised_while_in_flight(cred, refresh_token):
            await credentials.astore(
                user_id=person.pk,
                provider=GitPlatform.GITLAB,
                host=HOST,
                platform_uid="77",
                access_token="tok-reauth",  # noqa: S106
                refresh_token="refresh-reauth",  # noqa: S106
                expires_at=timezone.now() + timedelta(hours=2),
                scopes=["api"],
            )
            return {"access_token": "tok-late", "refresh_token": "refresh-late", "expires_in": 7200}

        with patch("accounts.credentials._arequest_refresh", side_effect=_reauthorised_while_in_flight):
            result = await credentials.aresolve_access_token(
                acting_user_id=person.pk, provider=GitPlatform.GITLAB, host=HOST
            )

        assert result.token == "tok-reauth"  # noqa: S105
        assert result.scopes == ("api",)
        reloaded = await PlatformCredential.objects.aget(user=person)
        assert reloaded.access_token == "tok-reauth"  # noqa: S105
        assert reloaded.scopes == ["api"]

    async def test_a_row_deleted_during_the_request_refuses_instead_of_raising(self, person):
        await _credential_async(person, expires_at=timezone.now() + timedelta(seconds=60))

        async def _deleted_while_in_flight(cred, refresh_token):
            await PlatformCredential.objects.filter(user=person).adelete()
            return {"access_token": "tok-late", "refresh_token": "refresh-late", "expires_in": 7200}

        with patch("accounts.credentials._arequest_refresh", side_effect=_deleted_while_in_flight):
            result = await credentials.aresolve_access_token(
                acting_user_id=person.pk, provider=GitPlatform.GITLAB, host=HOST
            )

        assert result.reason is CredentialReason.NO_CREDENTIAL


class TestAnUndecryptableCredentialRefusesInsteadOfRaising:
    """A rotated ``DAIV_ENCRYPTION_KEY`` is a documented operator action. The store's contract is
    a token or a typed reason, so the descriptor's ``DecryptionError`` must not escape."""

    async def test_resolution_returns_a_typed_reason(self, person):
        from cryptography.fernet import InvalidToken

        await _credential_async(person)
        with patch("core.encryption.decrypt_value", side_effect=InvalidToken):
            result = await credentials.aresolve_access_token(
                acting_user_id=person.pk, provider=GitPlatform.GITLAB, host=HOST
            )

        assert result.reason is CredentialReason.UNREADABLE
        assert result.token is None

    async def test_an_undecryptable_credential_stops_being_retried(self, person):
        from cryptography.fernet import InvalidToken

        credential = await _credential_async(person)
        with patch("core.encryption.decrypt_value", side_effect=InvalidToken):
            await credentials.aresolve_access_token(acting_user_id=person.pk, provider=GitPlatform.GITLAB, host=HOST)

        reloaded = await PlatformCredential.objects.aget(pk=credential.pk)
        assert reloaded.state == CredentialState.EXPIRED


class TestDisconnectFindsTheGrantAfterTheHostChanges:
    """``platform_host`` is derived live from settings; the row's host was frozen at sign-in. A
    person clicking Disconnect must not be told there was nothing to disconnect."""

    def test_revoke_clears_a_row_stored_under_another_host(self, person):
        credential = _credential(person, host="gitlab.example.com")

        with patch("accounts.credentials.platform_host", return_value="gitlab.com"):
            changed = credentials.revoke(user_id=person.pk, provider=GitPlatform.GITLAB)

        assert changed is True
        credential.refresh_from_db()
        assert credential.state == CredentialState.REVOKED
        assert credential.access_token is None

    def test_status_reports_a_row_stored_under_another_host(self, person):
        _credential(person, host="gitlab.example.com")

        with patch("accounts.credentials.platform_host", return_value="gitlab.com"):
            result = credentials.status(user_id=person.pk, provider=GitPlatform.GITLAB)

        assert result.connected is True
        assert result.host == "gitlab.example.com"

    def test_revoke_still_reports_no_change_when_there_is_no_grant(self, person):
        with patch("accounts.credentials.platform_host", return_value="gitlab.com"):
            assert credentials.revoke(user_id=person.pk, provider=GitPlatform.GITLAB) is False


class TestTheGitHubTokenEndpointFollowsTheConfiguredHost:
    """A GitHub Enterprise deployment must not POST its client secret to github.com."""

    def test_enterprise_host_is_honoured(self):
        with patch("accounts.socialaccount.codebase_settings") as cfg:
            cfg.GITHUB_URL = "https://ghe.example.com"
            endpoint = credentials._token_endpoint(GitPlatform.GITHUB)
        assert endpoint == "https://ghe.example.com/login/oauth/access_token"

    def test_github_dot_com_is_the_default(self):
        with patch("accounts.socialaccount.codebase_settings") as cfg:
            cfg.GITHUB_URL = None
            endpoint = credentials._token_endpoint(GitPlatform.GITHUB)
        assert endpoint == "https://github.com/login/oauth/access_token"


class TestUnknownStateFailsClosed:
    """``choices`` is not a database constraint and ``queryset.update()`` bypasses ``save()``, so
    a state this module does not recognise must refuse rather than fall through to the token."""

    async def test_an_unrecognised_state_refuses(self, person):
        await _credential_async(person)
        await PlatformCredential.objects.filter(user=person).aupdate(state="disconnected")

        result = await credentials.aresolve_access_token(
            acting_user_id=person.pk, provider=GitPlatform.GITLAB, host=HOST
        )

        assert result.token is None
        assert result.reason is CredentialReason.EXPIRED


class TestHostMismatch:
    """``platform_host`` derives from the codebase URL while allauth mints the token against
    ``auth_gitlab_url``; nothing else compares them."""

    def test_a_grant_minted_elsewhere_is_not_stored(self, person, settings):
        with patch("accounts.credentials.auth_host", return_value="gitlab.com"):
            result = credentials.store(
                user_id=person.pk,
                provider=GitPlatform.GITLAB,
                host="git.internal",
                platform_uid="77",
                access_token="tok",  # noqa: S106
                scopes=["api"],
            )

        assert result is None
        assert not PlatformCredential.objects.filter(user=person).exists()

    def test_matching_hosts_store_normally(self, person):
        with patch("accounts.credentials.auth_host", return_value=HOST):
            credentials.store(
                user_id=person.pk,
                provider=GitPlatform.GITLAB,
                host=HOST,
                platform_uid="77",
                access_token="tok",  # noqa: S106
                scopes=["api"],
            )

        assert PlatformCredential.objects.get(user=person).access_token == "tok"  # noqa: S105
