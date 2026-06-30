import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

from django.core.cache import cache

import pytest
import requests
from gitlab.exceptions import GitlabAuthenticationError, GitlabError

from codebase.clients.gitlab.clone_tokens import (
    CLONE_TOKEN_ACCESS_LEVEL,
    CLONE_TOKEN_CACHE_TIMEOUT,
    CLONE_TOKEN_LIFETIME_DAYS,
    CLONE_TOKEN_NAME,
    CLONE_TOKEN_SCOPES,
    CLONE_TOKEN_TRANSIENT_UNAVAILABLE_TIMEOUT,
    CLONE_TOKEN_UNAVAILABLE_TIMEOUT,
    get_ephemeral_clone_token,
    invalidate_clone_token,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def gl_client():
    """python-gitlab client mock whose project exposes an access_tokens manager."""
    client = Mock()
    project = Mock()
    client.projects.get.return_value = project
    project.access_tokens.create.return_value = Mock(token="glpat-ephemeral")  # noqa: S106
    return client


def test_cached_token_always_outlives_the_cache_window():
    """
    The one failure mode with no PAT fallback is serving an expired token from cache: the clone
    just fails. Expiry is date-granular (00:00 UTC), so the worst-case validity of a fresh token
    is (lifetime - 1) days — that floor must cover the whole cache window plus the >=24h of
    remaining validity the module comment (and the push-failure guidance about sessions resumed
    "a day or more later") promises for a token served at the very end of the window.
    """
    worst_case_validity = timedelta(days=CLONE_TOKEN_LIFETIME_DAYS - 1)
    assert worst_case_validity >= timedelta(seconds=CLONE_TOKEN_CACHE_TIMEOUT) + timedelta(days=1)
    # Failures must be retried sooner than tokens are refreshed, transient ones soonest.
    assert CLONE_TOKEN_TRANSIENT_UNAVAILABLE_TIMEOUT <= CLONE_TOKEN_UNAVAILABLE_TIMEOUT <= CLONE_TOKEN_CACHE_TIMEOUT


class TestGetEphemeralCloneToken:
    def test_creates_project_token_with_expected_payload(self, gl_client):
        before = datetime.now(UTC).date()
        token = get_ephemeral_clone_token(gl_client, 42)
        after = datetime.now(UTC).date()

        assert token == "glpat-ephemeral"  # noqa: S105
        gl_client.projects.get.assert_called_once_with(42, lazy=True)
        payload = gl_client.projects.get.return_value.access_tokens.create.call_args.args[0]
        assert payload["name"] == CLONE_TOKEN_NAME
        assert payload["scopes"] == CLONE_TOKEN_SCOPES
        assert payload["access_level"] == CLONE_TOKEN_ACCESS_LEVEL
        # Two accepted dates so a call straddling midnight UTC cannot flake.
        expected_expiry = {(day + timedelta(days=CLONE_TOKEN_LIFETIME_DAYS)).isoformat() for day in (before, after)}
        assert payload["expires_at"] in expected_expiry

    def test_second_call_is_served_from_cache(self, gl_client):
        assert get_ephemeral_clone_token(gl_client, 42) == "glpat-ephemeral"
        assert get_ephemeral_clone_token(gl_client, 42) == "glpat-ephemeral"
        assert gl_client.projects.get.return_value.access_tokens.create.call_count == 1

    def test_returns_none_and_negative_caches_on_api_error(self, gl_client):
        gl_client.projects.get.return_value.access_tokens.create.side_effect = GitlabError("403 Forbidden")

        assert get_ephemeral_clone_token(gl_client, 42) is None
        # Second call must not retry the API: the failure is negative-cached.
        assert get_ephemeral_clone_token(gl_client, 42) is None
        assert gl_client.projects.get.return_value.access_tokens.create.call_count == 1

    def test_returns_none_and_negative_caches_on_transport_error(self, gl_client):
        gl_client.projects.get.return_value.access_tokens.create.side_effect = requests.ConnectionError(
            "connection refused"
        )

        assert get_ephemeral_clone_token(gl_client, 42) is None
        assert get_ephemeral_clone_token(gl_client, 42) is None
        assert gl_client.projects.get.return_value.access_tokens.create.call_count == 1

    @pytest.mark.parametrize("secretless_token", [Mock(spec=[]), Mock(token=""), Mock(token=None)])
    def test_missing_token_secret_warns_and_falls_back(self, gl_client, caplog, secretless_token):
        """A successful create with no usable secret must not crash, must warn, and must negative-cache."""
        gl_client.projects.get.return_value.access_tokens.create.return_value = secretless_token

        with caplog.at_level(logging.WARNING, logger="daiv.clients"):
            assert get_ephemeral_clone_token(gl_client, 42) is None

        assert "without a token secret" in caplog.text
        assert get_ephemeral_clone_token(gl_client, 42) is None
        assert gl_client.projects.get.return_value.access_tokens.create.call_count == 1

    def test_rejected_pat_is_named_in_the_warning(self, gl_client, caplog):
        """A 401 means the PAT itself is dead — the warning must say so, not claim a benign fallback."""
        gl_client.projects.get.return_value.access_tokens.create.side_effect = GitlabAuthenticationError(
            "401 Unauthorized"
        )

        with caplog.at_level(logging.WARNING, logger="daiv.clients"):
            assert get_ephemeral_clone_token(gl_client, 42) is None

        assert "rejected the configured PAT" in caplog.text
        assert get_ephemeral_clone_token(gl_client, 42) is None
        assert gl_client.projects.get.return_value.access_tokens.create.call_count == 1

    @pytest.mark.parametrize(
        ("error", "expected_timeout"),
        [
            (requests.ConnectionError("connection refused"), CLONE_TOKEN_TRANSIENT_UNAVAILABLE_TIMEOUT),
            (GitlabError("502 Bad Gateway", response_code=502), CLONE_TOKEN_TRANSIENT_UNAVAILABLE_TIMEOUT),
            (GitlabError("429 Too Many Requests", response_code=429), CLONE_TOKEN_TRANSIENT_UNAVAILABLE_TIMEOUT),
            (GitlabError("403 Forbidden", response_code=403), CLONE_TOKEN_UNAVAILABLE_TIMEOUT),
        ],
    )
    def test_negative_cache_ttl_matches_failure_persistence(self, gl_client, error, expected_timeout):
        """Transient failures (network, 429, 5xx) must retry sooner than persistent ones (role/tier)."""
        gl_client.projects.get.return_value.access_tokens.create.side_effect = error

        with patch("codebase.clients.gitlab.clone_tokens.cache") as cache_mock:
            cache_mock.get.return_value = None
            assert get_ephemeral_clone_token(gl_client, 42) is None

        assert cache_mock.set.call_args.args[2] == expected_timeout

    def test_minted_token_is_cached_for_the_full_window(self, gl_client):
        """A wrong TTL here would serve expired tokens from cache — the no-fallback failure mode."""
        with patch("codebase.clients.gitlab.clone_tokens.cache") as cache_mock:
            cache_mock.get.return_value = None
            assert get_ephemeral_clone_token(gl_client, 42) == "glpat-ephemeral"

        assert cache_mock.set.call_args.args[2] == CLONE_TOKEN_CACHE_TIMEOUT

    def test_invalidate_drops_the_cache_so_the_next_call_remints(self, gl_client):
        """A token GitLab now rejects must be evictable so the next clone mints a fresh one
        instead of being served the dead token for the rest of the cache window."""
        assert get_ephemeral_clone_token(gl_client, 42) == "glpat-ephemeral"

        invalidate_clone_token(42)

        assert get_ephemeral_clone_token(gl_client, 42) == "glpat-ephemeral"
        assert gl_client.projects.get.return_value.access_tokens.create.call_count == 2

    def test_invalidate_is_a_noop_when_nothing_is_cached(self):
        """Invalidating an absent entry must not raise (a clone can fail before anything was cached)."""
        invalidate_clone_token(999)

    def test_tokens_are_cached_per_project(self, gl_client):
        project_a, project_b = Mock(), Mock()
        project_a.access_tokens.create.return_value = Mock(token="glpat-a")  # noqa: S106
        project_b.access_tokens.create.return_value = Mock(token="glpat-b")  # noqa: S106
        gl_client.projects.get.side_effect = lambda pk, lazy=True: {1: project_a, 2: project_b}[pk]

        assert get_ephemeral_clone_token(gl_client, 1) == "glpat-a"
        assert get_ephemeral_clone_token(gl_client, 2) == "glpat-b"
        assert get_ephemeral_clone_token(gl_client, 1) == "glpat-a"
        assert project_a.access_tokens.create.call_count == 1
        assert project_b.access_tokens.create.call_count == 1
