from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import pytest

from codebase.base import ClientType, Repository, User
from codebase.clients import RepoClient


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

        # Mock basic repository operations
        mock_client.get_repository.return_value = Repository(
            pk=1, slug="test/test-repo", name="test-repo", default_branch="main", client=ClientType.GITLAB
        )
        mock_client.list_repositories.return_value = []
        mock_client.get_repository_file.return_value = None
        mock_client.repository_file_exists.return_value = False
        mock_client.repository_branch_exists.return_value = True

        # Mock repository modification operations
        mock_client.set_repository_webhooks.return_value = True
        mock_client.get_merge_request_diff.return_value = iter([])
        mock_client.update_or_create_merge_request.return_value = 1
        mock_client.comment_merge_request.return_value = None
        mock_client.commit_changes.return_value = None

        # Mock repository inspection operations
        mock_client.get_repo_head_sha.return_value = "abc123"
        mock_client.get_commit_changed_files.return_value = ([], [], [])

        # Mock issue operations
        mock_client.get_issue.return_value = Mock()
        mock_client.comment_issue.return_value = None
        mock_client.create_issue_note_emoji.return_value = None
        mock_client.get_issue_notes.return_value = []
        mock_client.get_issue_discussions.return_value = []
        mock_client.get_issue_discussion.return_value = Mock()
        mock_client.get_issue_related_merge_requests.return_value = []
        mock_client.create_issue_discussion_note.return_value = None

        # Mock merge request operations
        mock_client.get_merge_request.return_value = Mock()
        mock_client.get_merge_request_latest_pipeline.return_value = None
        mock_client.get_merge_request_discussions.return_value = []
        mock_client.get_merge_request_discussion.return_value = Mock()
        mock_client.resolve_merge_request_discussion.return_value = None
        mock_client.update_merge_request_discussion_note.return_value = None

        # Mock load_repo to return a temporary directory context manager
        @contextmanager
        def mock_load_repo(repo_id: str, sha: str):
            with TemporaryDirectory() as temp_dir:
                yield Path(temp_dir)

        mock_client.load_repo = mock_load_repo

        # Set up the create_instance mock to return our comprehensive mock
        mock_create_instance.return_value = mock_client

        yield mock_client
