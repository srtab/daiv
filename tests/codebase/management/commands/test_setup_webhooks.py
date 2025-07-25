from unittest.mock import Mock, call

from django.core.management import call_command

from codebase.conf import settings


def test_setup_webhooks_success(mock_repo_client):
    """Test successful webhook setup for repositories."""
    # Mock repositories
    mock_repo1 = Mock(slug="repo1")
    mock_repo2 = Mock(slug="repo2")
    mock_repo_client.list_repositories.return_value = [mock_repo1, mock_repo2]
    mock_repo_client.set_repository_webhooks.return_value = True

    # Call the command
    call_command("setup_webhooks", base_url="https://test.com")

    # Verify list_repositories was called with load_all=True
    mock_repo_client.list_repositories.assert_called_once_with(load_all=True)

    # Verify set_repository_webhooks was called for each repo
    expected_calls = [
        call(
            "repo1",
            "https://test.com/api/codebase/callbacks/gitlab/",
            ["push_events", "issues_events", "note_events"],
            enable_ssl_verification=True,
            secret_token=settings.GITLAB_WEBHOOK_SECRET.get_secret_value(),
        ),
        call(
            "repo2",
            "https://test.com/api/codebase/callbacks/gitlab/",
            ["push_events", "issues_events", "note_events"],
            enable_ssl_verification=True,
            secret_token=settings.GITLAB_WEBHOOK_SECRET.get_secret_value(),
        ),
    ]
    assert mock_repo_client.set_repository_webhooks.call_count == 2
    mock_repo_client.set_repository_webhooks.assert_has_calls(expected_calls)


def test_setup_webhooks_with_ssl_disabled(mock_repo_client):
    """Test webhook setup with SSL verification disabled."""
    # Mock single repository
    mock_repo = Mock(slug="repo1")
    mock_repo_client.list_repositories.return_value = [mock_repo]
    mock_repo_client.set_repository_webhooks.return_value = True

    # Call the command with SSL verification disabled
    call_command("setup_webhooks", base_url="https://test.com", disable_ssl_verification=True)

    # Verify set_repository_webhooks was called with SSL verification disabled
    mock_repo_client.set_repository_webhooks.assert_called_once_with(
        "repo1",
        "https://test.com/api/codebase/callbacks/gitlab/",
        ["push_events", "issues_events", "note_events"],
        enable_ssl_verification=False,
        secret_token=settings.GITLAB_WEBHOOK_SECRET.get_secret_value(),
    )


def test_setup_webhooks_update_existing(mock_repo_client):
    """Test updating existing webhooks."""
    # Mock repository
    mock_repo = Mock(slug="repo1")
    mock_repo_client.list_repositories.return_value = [mock_repo]
    # Return False to simulate updating existing webhook
    mock_repo_client.set_repository_webhooks.return_value = False

    # Call the command
    call_command("setup_webhooks", base_url="https://test.com")

    # Verify webhook was updated
    mock_repo_client.set_repository_webhooks.assert_called_once_with(
        "repo1",
        "https://test.com/api/codebase/callbacks/gitlab/",
        ["push_events", "issues_events", "note_events"],
        enable_ssl_verification=True,
        secret_token=settings.GITLAB_WEBHOOK_SECRET.get_secret_value(),
    )


def test_setup_webhooks_no_repositories(mock_repo_client):
    """Test behavior when no repositories are found."""
    # Mock empty repository list
    mock_repo_client.list_repositories.return_value = []

    # Call the command
    call_command("setup_webhooks", base_url="https://test.com")

    # Verify list_repositories was called but set_repository_webhooks was not
    mock_repo_client.list_repositories.assert_called_once_with(load_all=True)
    mock_repo_client.set_repository_webhooks.assert_not_called()


def test_setup_webhooks_with_secret_token(mock_repo_client):
    """Test webhook setup with secret token provided via command line."""
    # Mock repository
    mock_repo = Mock(slug="repo1")
    mock_repo_client.list_repositories.return_value = [mock_repo]
    mock_repo_client.set_repository_webhooks.return_value = True

    # Call the command with secret token
    call_command("setup_webhooks", base_url="https://test.com", secret_token="test_secret")  # noqa: S106

    # Verify set_repository_webhooks was called with the secret token
    mock_repo_client.set_repository_webhooks.assert_called_once_with(
        "repo1",
        "https://test.com/api/codebase/callbacks/gitlab/",
        ["push_events", "issues_events", "note_events"],
        enable_ssl_verification=True,
        secret_token="test_secret",  # noqa: S106
    )
