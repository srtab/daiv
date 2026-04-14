from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock, patch

from django.test import Client

import pytest
from pydantic import SecretStr

from accounts.models import Role
from accounts.models import User as AccountUser
from codebase.base import GitPlatform, MergeRequest, Repository, User
from codebase.clients import RepoClient
from codebase.conf import settings as codebase_settings


@pytest.fixture
def admin_user(db):
    return AccountUser.objects.create_user(
        username="admin",
        email="admin@test.com",
        password="testpass123",  # noqa: S106
        role=Role.ADMIN,
    )


@pytest.fixture
def member_user(db):
    return AccountUser.objects.create_user(
        username="member",
        email="member@test.com",
        password="testpass123",  # noqa: S106
        role=Role.MEMBER,
    )


@pytest.fixture
def admin_client(admin_user):
    client = Client()
    client.force_login(admin_user)
    return client


@pytest.fixture
def member_client(member_user):
    client = Client()
    client.force_login(member_user)
    return client


@pytest.fixture(autouse=True)
def mock_settings(monkeypatch):
    """Fixture to mock secret tokens for testing.

    Sets environment variables so that ``site_settings`` resolves API keys
    without hitting the database.  Pydantic-only settings (codebase) are
    patched directly.
    """
    monkeypatch.setenv("DAIV_SANDBOX_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    with (
        patch.object(codebase_settings, "GITLAB_WEBHOOK_SECRET", SecretStr("test_secret")),
        patch.object(codebase_settings, "GITHUB_WEBHOOK_SECRET", SecretStr("test_secret")),
        patch.object(codebase_settings, "CLIENT", GitPlatform.GITLAB),
    ):
        yield codebase_settings


@pytest.fixture(autouse=True)
def mock_repo_client():
    """
    Global fixture that automatically mocks RepoClient.create_instance for all tests.

    This fixture returns a comprehensive mock that implements all the abstract methods
    of RepoClient to prevent AttributeError during tests.
    """
    with patch.object(RepoClient, "create_instance") as mock_create_instance:
        # Create a mock that implements the RepoClient interface
        mock_client = Mock(spec=RepoClient)

        # Set up commonly used properties and methods with reasonable defaults
        mock_client.current_user = User(id=1, username="test-user", name="Test User")
        mock_client.codebase_url = "https://test-repo.com"
        mock_client.git_platform = GitPlatform.GITLAB

        # Mock basic repository operations
        mock_client.get_repository.return_value = Repository(
            pk=1,
            slug="test/test-repo",
            name="test-repo",
            default_branch="main",
            git_platform=GitPlatform.GITLAB,
            clone_url="https://test-repo.com",
            html_url="https://test-repo.com",
        )
        mock_client.list_repositories.return_value = []
        mock_client.get_repository_file.return_value = None
        mock_client.get_project_uploaded_file = AsyncMock(return_value=b"image content")

        # Mock repository modification operations
        mock_client.set_repository_webhooks.return_value = True

        # Mock issue operations
        mock_client.get_issue.return_value = Mock()
        mock_client.create_issue_comment.return_value = None
        mock_client.create_issue_emoji.return_value = None
        mock_client.get_issue_comment.return_value = Mock()

        # Mock merge request operations
        merge_request = MergeRequest(
            repo_id="test/test-repo",
            merge_request_id=1,
            source_branch="feature/test",
            target_branch="main",
            title="Test merge request",
            description="Test merge request description",
            labels=["daiv"],
            web_url="https://test-repo.com/merge_requests/1",
            sha="testsha",
            author=mock_client.current_user,
        )
        mock_client.update_or_create_merge_request.return_value = merge_request
        mock_client.update_merge_request.return_value = merge_request
        mock_client.get_merge_request.return_value = merge_request
        mock_client.get_merge_request_comment.return_value = Mock()
        mock_client.create_merge_request_comment.return_value = None
        mock_client.create_merge_request_note_emoji.return_value = None
        mock_client.mark_merge_request_comment_as_resolved.return_value = None
        mock_client.get_merge_request_commits.return_value = []
        mock_client.get_bot_commit_email.return_value = "daiv@users.noreply.gitlab.com"

        # Mock load_repo to return a temporary directory context manager
        @contextmanager
        def mock_load_repo(repo_id: str, sha: str):
            with TemporaryDirectory() as temp_dir:
                yield Path(temp_dir)

        mock_client.load_repo = mock_load_repo

        # Set up the create_instance mock to return our comprehensive mock
        mock_create_instance.return_value = mock_client

        yield mock_client
