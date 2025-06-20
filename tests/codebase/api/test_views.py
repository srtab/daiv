from unittest.mock import patch

import pytest
from daiv.api import api
from ninja.testing import TestAsyncClient
from pydantic import SecretStr

from codebase.api.callbacks_gitlab import PushCallback
from codebase.api.models import Project


@pytest.fixture
def client():
    return TestAsyncClient(api)


@pytest.fixture()
def mock_secret_token():
    """Fixture to mock the secret token for testing."""
    with patch("codebase.api.security.settings") as mock:
        mock.GITLAB_WEBHOOK_SECRET = SecretStr("test")  # noqa: S105
        yield mock


@pytest.fixture
def mock_push_callback():
    return PushCallback(
        object_kind="push", project=Project(id=123, path_with_namespace="test/test"), checkout_sha="123", ref="main"
    ).model_dump()


async def test_gitlab_callback_valid_token(client: TestAsyncClient, mock_push_callback, mock_secret_token):
    """Test GitLab callback with valid token."""
    # Execute
    with (
        patch.object(PushCallback, "accept_callback", return_value=True) as accept_callback,
        patch.object(PushCallback, "process_callback", return_value=True) as process_callback,
    ):
        response = await client.post(
            "/codebase/callbacks/gitlab/", json=mock_push_callback, headers={"X-Gitlab-Token": "test"}
        )

    # Assert
    assert response.status_code == 204
    accept_callback.assert_called_once()
    process_callback.assert_called_once()


async def test_gitlab_callback_invalid_token(client: TestAsyncClient, mock_push_callback, mock_secret_token):
    """
    Test GitLab callback with invalid token.
    """
    # Execute
    with (
        patch.object(PushCallback, "accept_callback", return_value=False) as accept_callback,
        patch.object(PushCallback, "process_callback", return_value=False) as process_callback,
    ):
        response = await client.post(
            "/codebase/callbacks/gitlab/", json=mock_push_callback, headers={"X-Gitlab-Token": "invalid"}
        )

    # Assert
    assert response.status_code == 401
    accept_callback.assert_not_called()
    process_callback.assert_not_called()


async def test_gitlab_callback_not_accepted(client: TestAsyncClient, mock_push_callback, mock_secret_token):
    """
    Test GitLab callback with not accepted webhook.
    """
    mock_secret_token.GITLAB_WEBHOOK_SECRET = None
    # Execute
    with (
        patch.object(PushCallback, "accept_callback", return_value=False) as accept_callback,
        patch.object(PushCallback, "process_callback", return_value=False) as process_callback,
    ):
        response = await client.post("/codebase/callbacks/gitlab/", json=mock_push_callback)

    # Assert
    assert response.status_code == 204
    accept_callback.assert_called_once()
    process_callback.assert_not_called()
