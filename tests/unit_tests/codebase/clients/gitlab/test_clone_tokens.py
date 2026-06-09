from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

from django.core.cache import cache

import pytest
import requests
from gitlab.exceptions import GitlabError

from codebase.clients.gitlab.clone_tokens import (
    CLONE_TOKEN_ACCESS_LEVEL,
    CLONE_TOKEN_LIFETIME_DAYS,
    CLONE_TOKEN_NAME,
    CLONE_TOKEN_SCOPES,
    get_ephemeral_clone_token,
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


class TestGetEphemeralCloneToken:
    def test_creates_project_token_with_expected_payload(self, gl_client):
        token = get_ephemeral_clone_token(gl_client, 42)

        assert token == "glpat-ephemeral"  # noqa: S105
        gl_client.projects.get.assert_called_once_with(42, lazy=True)
        payload = gl_client.projects.get.return_value.access_tokens.create.call_args.args[0]
        assert payload["name"] == CLONE_TOKEN_NAME
        assert payload["scopes"] == CLONE_TOKEN_SCOPES
        assert payload["access_level"] == CLONE_TOKEN_ACCESS_LEVEL
        expected_expiry = (datetime.now(UTC).date() + timedelta(days=CLONE_TOKEN_LIFETIME_DAYS)).isoformat()
        assert payload["expires_at"] == expected_expiry

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
