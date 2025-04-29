from unittest.mock import AsyncMock, MagicMock, patch

from django.test import Client

import pytest

from codebase.api.callbacks_gitlab import PushCallback


@pytest.fixture
def mock_push_callback():
    callback = MagicMock(spec=PushCallback)
    callback.object_kind = "push"
    callback.project = MagicMock()
    callback.project.id = 123
    callback.accept_callback.return_value = True
    callback.process_callback = AsyncMock()
    return callback


@pytest.mark.django_db
@pytest.mark.asyncio
@patch("codebase.api.views.validate_gitlab_webhook")
async def test_gitlab_callback_valid_webhook(mock_validate, mock_push_callback, client: Client):
    """Test GitLab callback with valid webhook validation."""
    # Setup
    mock_validate.return_value = True

    # Execute
    with patch(
        "codebase.api.views.IssueCallback | NoteCallback | PushCallback | PipelineStatusCallback",
        return_value=mock_push_callback,
    ):
        response = client.post("/api/codebase/callbacks/gitlab/", {}, content_type="application/json")

    # Assert
    assert response.status_code == 204
    mock_validate.assert_called_once()
    mock_push_callback.accept_callback.assert_called_once()
    mock_push_callback.process_callback.assert_called_once()


@pytest.mark.django_db
@pytest.mark.asyncio
@patch("codebase.api.views.validate_gitlab_webhook")
async def test_gitlab_callback_invalid_webhook(mock_validate, mock_push_callback, client: Client):
    """Test GitLab callback with invalid webhook validation."""
    # Setup
    mock_validate.return_value = False

    # Execute
    with patch(
        "codebase.api.views.IssueCallback | NoteCallback | PushCallback | PipelineStatusCallback",
        return_value=mock_push_callback,
    ):
        response = client.post("/api/codebase/callbacks/gitlab/", {}, content_type="application/json")

    # Assert
    assert response.status_code == 401
    mock_validate.assert_called_once()
    mock_push_callback.accept_callback.assert_not_called()
    mock_push_callback.process_callback.assert_not_called()


@pytest.mark.django_db
@pytest.mark.asyncio
@patch("codebase.api.views.validate_gitlab_webhook")
async def test_gitlab_callback_not_accepted(mock_validate, mock_push_callback, client: Client):
    """Test GitLab callback that is not accepted."""
    # Setup
    mock_validate.return_value = True
    mock_push_callback.accept_callback.return_value = False

    # Execute
    with patch(
        "codebase.api.views.IssueCallback | NoteCallback | PushCallback | PipelineStatusCallback",
        return_value=mock_push_callback,
    ):
        response = client.post("/api/codebase/callbacks/gitlab/", {}, content_type="application/json")

    # Assert
    assert response.status_code == 204
    mock_validate.assert_called_once()
    mock_push_callback.accept_callback.assert_called_once()
    mock_push_callback.process_callback.assert_not_called()
