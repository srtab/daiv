from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock, patch

from django.core.cache import cache
from django.test import Client

import pytest
from pydantic import SecretStr

from accounts.models import Role
from accounts.models import User as AccountUser
from codebase.base import GitPlatform, MergeRequest, Repository, User
from codebase.clients import RepoClient
from codebase.conf import settings as codebase_settings
from core.models import PROVIDERS_CACHE_KEY, SITE_CONFIGURATION_CACHE_KEY, WEB_FETCH_AUTH_HEADERS_CACHE_KEY


@pytest.fixture(autouse=True)
def _clear_model_caches():
    # Provider/WebFetchAuthHeader/SiteConfiguration invalidate via
    # transaction.on_commit; @pytest.mark.django_db tests roll back without
    # committing, so the LocMem cache would otherwise leak state between tests.
    keys = (PROVIDERS_CACHE_KEY, SITE_CONFIGURATION_CACHE_KEY, WEB_FETCH_AUTH_HEADERS_CACHE_KEY)
    for key in keys:
        cache.delete(key)
    yield
    for key in keys:
        cache.delete(key)


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
def mock_generate_title_task():
    """Stub the titling tasks so the ImmediateBackend doesn't fire real LLM calls.

    Without this, every test that hits ``submit_batch_runs`` / chat thread creation
    pays for two failed LLM retries plus the fallback model — adding tens of seconds
    to the suite. Tests that exercise titling itself import the ``.func`` attribute
    directly, so they bypass this patch.

    Patching the module-level binding at each import site (rather than the frozen
    ``Task`` instance) avoids ``patch.object`` teardown issues on slotted dataclasses.
    """
    with (
        patch("activity.services.generate_batch_title_task") as m1,
        patch("chat.models.generate_title_task") as m2,
        patch("chat.api.threads.generate_title_task") as m3,
    ):
        for m in (m1, m2, m3):
            m.aenqueue = AsyncMock(return_value=None)
        yield m1


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
        mock_client.list_repository_members.return_value = []
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


@pytest.fixture(autouse=True)
def mock_repo_authorization():
    """Grant repository access by default.

    The authorization layer has its own tests (tests/unit_tests/codebase/test_authorization.py);
    every other test gets an allow-all so pre-authorization behavior is preserved. Tests that
    exercise denial re-patch the same name inside their own ``with`` block (the inner patch wins).
    """
    with (
        patch("activity.services.aassert_can_run", new=AsyncMock(return_value=None)),
        patch("jobs.api.views.aassert_can_run", new=AsyncMock(return_value=None)),
        patch("mcp_server.server.aassert_can_run", new=AsyncMock(return_value=None)),
        patch("chat.api.views.aassert_can_run", new=AsyncMock(return_value=None)),
        patch("activity.forms.assert_can_run", new=Mock(return_value=None)),
        patch("codebase.views.can_view", new=Mock(return_value=True)),
        patch("memory.views.can_view", new=Mock(return_value=True)),
        patch("memory.views.viewable_repo_ids", new=Mock(side_effect=lambda user, ids: set(ids))),
    ):
        yield
