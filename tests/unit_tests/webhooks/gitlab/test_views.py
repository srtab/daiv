import json
from unittest.mock import patch

import pytest
from ninja.testing import TestAsyncClient
from webhooks.conf import settings as webhooks_settings
from webhooks.gitlab.callbacks import NoteCallback, PushCallback
from webhooks.gitlab.models import Project

from daiv.api import api

ISSUE_NOTE = json.dumps({
    "object_kind": "note",
    "project": {"id": 123, "path_with_namespace": "test/test"},
    "user": {"id": 10, "name": "Alice", "username": "alice", "email": "alice@example.com"},
    "object_attributes": {
        "id": 1,
        "action": "create",
        "noteable_type": "Issue",
        "noteable_id": 7,
        "discussion_id": "abc",
        "note": "@daiv please",
        "system": False,
    },
}).encode()


@pytest.fixture
def client():
    return TestAsyncClient(api)


@pytest.fixture
def platform_calls():
    with (
        patch("webhooks.gitlab.callbacks.RepositoryConfig.get_config") as get_config,
        patch("webhooks.gitlab.callbacks.RepoClient.create_instance") as create_instance,
    ):
        yield get_config, create_instance


@pytest.fixture
def mock_push_callback():
    return PushCallback(
        object_kind="push", project=Project(id=123, path_with_namespace="test/test"), checkout_sha="123", ref="main"
    ).model_dump()


async def test_gitlab_callback_valid_token(client: TestAsyncClient, mock_push_callback):
    """Test GitLab callback with valid token."""
    # Execute
    with (
        patch.object(PushCallback, "accept_callback", return_value=True) as accept_callback,
        patch.object(PushCallback, "process_callback", return_value=True) as process_callback,
    ):
        response = await client.post(
            "/codebase/callbacks/gitlab/", json=mock_push_callback, headers={"X-Gitlab-Token": "test_secret"}
        )

    # Assert
    assert response.status_code == 204
    accept_callback.assert_called_once()
    process_callback.assert_called_once()


async def test_gitlab_callback_invalid_token(client: TestAsyncClient, mock_push_callback):
    """
    Test GitLab callback with invalid token.
    """
    # Execute
    with (
        patch.object(PushCallback, "accept_callback", return_value=False) as accept_callback,
        patch.object(PushCallback, "process_callback", return_value=False) as process_callback,
    ):
        response = await client.post(
            "/codebase/callbacks/gitlab/", json=mock_push_callback, headers={"X-Gitlab-Token": "invalid_secret"}
        )

    # Assert
    assert response.status_code == 401
    accept_callback.assert_not_called()
    process_callback.assert_not_called()


async def test_gitlab_callback_non_ascii_token_is_401_not_500(client: TestAsyncClient, mock_push_callback):
    """An attacker-supplied non-ASCII token byte must answer 401, never crash the endpoint."""
    # Execute
    with (
        patch.object(PushCallback, "accept_callback", return_value=False) as accept_callback,
        patch.object(PushCallback, "process_callback", return_value=False) as process_callback,
    ):
        response = await client.post(
            "/codebase/callbacks/gitlab/",
            json=mock_push_callback,
            headers={"X-Gitlab-Token": b"\xe9".decode("latin-1")},
        )

    # Assert
    assert response.status_code == 401
    accept_callback.assert_not_called()
    process_callback.assert_not_called()


async def test_gitlab_callback_not_accepted(client: TestAsyncClient, mock_push_callback):
    """
    Test GitLab callback with not accepted webhook.
    """
    # Execute
    with (
        patch.object(PushCallback, "accept_callback", return_value=False) as accept_callback,
        patch.object(PushCallback, "process_callback", return_value=False) as process_callback,
    ):
        response = await client.post(
            "/codebase/callbacks/gitlab/", json=mock_push_callback, headers={"X-Gitlab-Token": "test_secret"}
        )

    # Assert
    assert response.status_code == 204
    accept_callback.assert_called_once()
    process_callback.assert_not_called()


async def test_a_valid_token_parses_the_note(client: TestAsyncClient):
    with (
        patch.object(NoteCallback, "accept_callback", return_value=True) as accept_callback,
        patch.object(NoteCallback, "process_callback") as process_callback,
    ):
        response = await client.post(
            "/codebase/callbacks/gitlab/", data=ISSUE_NOTE, headers={"X-Gitlab-Token": "test_secret"}
        )

    assert response.status_code == 204
    accept_callback.assert_called_once()
    process_callback.assert_awaited_once()


@pytest.mark.parametrize(
    "headers", [pytest.param({}, id="no-token"), pytest.param({"X-Gitlab-Token": "invalid_secret"}, id="wrong-token")]
)
async def test_an_unverified_request_is_rejected_before_the_payload_is_parsed(
    client: TestAsyncClient, platform_calls, headers
):
    response = await client.post("/codebase/callbacks/gitlab/", data=ISSUE_NOTE, headers=headers)

    assert response.status_code == 401
    for platform_call in platform_calls:
        platform_call.assert_not_called()


async def test_an_unauthenticated_malformed_payload_is_401_not_422(client: TestAsyncClient):
    """A 422 would mean the body was parsed, and its error detail leaked, before the token was checked."""
    response = await client.post("/codebase/callbacks/gitlab/", data=b'{"object_kind": "note"}')

    assert response.status_code == 401


async def test_without_a_configured_secret_every_request_is_rejected(
    client: TestAsyncClient, platform_calls, monkeypatch
):
    monkeypatch.setattr(webhooks_settings, "GITLAB_WEBHOOK_SECRET", None)

    response = await client.post(
        "/codebase/callbacks/gitlab/", data=ISSUE_NOTE, headers={"X-Gitlab-Token": "test_secret"}
    )

    assert response.status_code == 401
    for platform_call in platform_calls:
        platform_call.assert_not_called()
