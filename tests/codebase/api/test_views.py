from unittest.mock import patch

import pytest
from ninja.testing import TestAsyncClient

from codebase.api.callbacks_gitlab import PushCallback
from codebase.api.models import Project
from daiv.api import api


@pytest.fixture
def client():
    return TestAsyncClient(api)


@pytest.fixture(autouse=True)
def mock_settings():
    """Fixture to mock the settings for testing."""
    with patch("codebase.api.security.settings") as mock:
        mock.WEBHOOK_SECRET_GITLAB = "test"  # noqa: S105
        yield mock


@pytest.fixture
def mock_push_callback():
    return PushCallback(
        object_kind="push", project=Project(id=123, path_with_namespace="test/test"), checkout_sha="123", ref="main"
    ).model_dump()


@pytest.mark.asyncio
async def test_gitlab_callback_valid_token(client: TestAsyncClient, mock_push_callback):
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


@pytest.mark.asyncio
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
            "/codebase/callbacks/gitlab/", json=mock_push_callback, headers={"X-Gitlab-Token": "invalid"}
        )

    # Assert
    assert response.status_code == 401
    accept_callback.assert_not_called()
    process_callback.assert_not_called()


@pytest.mark.asyncio
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
            "/codebase/callbacks/gitlab/", json=mock_push_callback, headers={"X-Gitlab-Token": "test"}
        )

    # Assert
    assert response.status_code == 204
    accept_callback.assert_called_once()
    process_callback.assert_not_called()
