import io
import tarfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal
from unittest.mock import AsyncMock, Mock, patch

from django.core.cache import cache
from django.test import Client

import httpx
import pytest
from langchain_core.messages import AIMessage, ToolMessage
from pydantic import SecretStr

from accounts.models import Role
from accounts.models import User as AccountUser
from codebase.base import GitPlatform, MergeRequest, Repository, User
from codebase.clients import RepoClient
from codebase.conf import settings as codebase_settings
from codebase.context import SandboxRuntime
from core.models import PROVIDERS_CACHE_KEY, SITE_CONFIGURATION_CACHE_KEY, WEB_FETCH_AUTH_HEADERS_CACHE_KEY
from core.sandbox.client import reset_run_sandbox_client, set_run_sandbox_client
from core.sandbox.command_policy import SandboxCommandPolicy
from core.sandbox.schemas import (
    EgressConfigRequest,
    RunCommandResult,
    RunCommandsRequest,
    RunCommandsResponse,
    StartSessionRequest,
)


def sandbox_runtime(
    *, base_image: str | None = "python:3.12", egress: EgressConfigRequest | None = None
) -> SandboxRuntime:
    return SandboxRuntime(
        base_image=base_image,
        memory_bytes=None,
        cpus=None,
        env_vars={},
        command_policy=SandboxCommandPolicy(),
        egress=egress,
    )


@contextmanager
def bound_run_sandbox_client(client):
    """Bind ``client`` as the run-scoped sandbox client, as ``set_runtime_ctx`` does for a sandbox run."""
    token = set_run_sandbox_client(client)
    try:
        yield client
    finally:
        reset_run_sandbox_client(token)


@dataclass
class FakeSandboxSession:
    request: StartSessionRequest
    state: Literal["running", "stopped"] = "running"
    egress: EgressConfigRequest | None = None
    repo_files: frozenset[str] | None = None
    skills_files: frozenset[str] | None = None


class FakeSandboxClient:
    """In-memory ``DAIVSandboxClient``: sessions, seeded files, a call log and failure switches.

    Like the real client, it starts closed (``opened()`` builds one as ``set_runtime_ctx`` hands it
    to the run), a call on a closed client raises ``AttributeError`` without reaching the log, and
    ``session_exists`` returns ``False`` on a 404. Like the real sandbox, commands 404 on a missing
    session and restart a stopped one, ``fail_fast`` stops at the first non-zero exit,
    ``close_session`` is idempotent, and ``update_egress`` 409s on a session started without egress.

    ``responses`` maps a command substring to ``(exit_code, output)``; the first match wins and
    unmatched commands succeed with empty output.
    """

    def __init__(self, responses: dict[str, tuple[int, str]] | None = None) -> None:
        self.responses = responses or {}
        self.sessions: dict[str, FakeSandboxSession] = {}
        self.calls: list[tuple[str, tuple]] = []
        self.commands: list[str] = []
        self.is_open = False
        self._failures: dict[str, Exception] = {}
        self._next_id = 0

    @classmethod
    def opened(cls, responses: dict[str, tuple[int, str]] | None = None) -> FakeSandboxClient:
        client = cls(responses)
        client.is_open = True
        return client

    def add_running_session(self, session_id: str) -> str:
        """Register a running session without a logged ``start_session``, for tests that only need a live id."""
        self.sessions[session_id] = FakeSandboxSession(request=StartSessionRequest(base_image="python:3.12"))
        return session_id

    def fail(self, method: str, *, status: int | None = None, detail: str = "") -> None:
        """Make every later call to ``method`` fail with HTTP ``status``, or a transport error when ``status`` is None.

        The failed call is still logged; ``session_exists`` maps a 404 to ``False``.
        """
        if status is None:
            request = httpx.Request("POST", f"http://sandbox.test/{method}")
            self._failures[method] = httpx.ConnectError("connection refused", request=request)
        else:
            self._failures[method] = self._status_error(method, status, detail)

    def calls_to(self, method: str) -> list[tuple]:
        return [args for name, args in self.calls if name == method]

    def method_names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def ran(self, needle: str) -> bool:
        return any(needle in command for command in self.commands)

    def _record(self, method: str, *args) -> None:
        if not self.is_open:
            raise AttributeError(f"{method} called on a closed sandbox client")
        self.calls.append((method, args))
        if (failure := self._failures.get(method)) is not None:
            raise failure

    @staticmethod
    def _status_error(method: str, status: int, detail: str) -> httpx.HTTPStatusError:
        request = httpx.Request("POST", f"http://sandbox.test/{method}")
        response = httpx.Response(status, json={"detail": detail}, request=request)
        return httpx.HTTPStatusError(str(status), request=request, response=response)

    def _require(self, method: str, session_id: str) -> FakeSandboxSession:
        if session_id not in self.sessions:
            raise self._status_error(method, 404, "Session not found")
        return self.sessions[session_id]

    async def open(self) -> FakeSandboxClient:
        if self.is_open:
            raise RuntimeError("FakeSandboxClient is already open")
        self.calls.append(("open", ()))
        self.is_open = True
        return self

    async def close(self) -> None:
        self.calls.append(("close", ()))
        self.is_open = False

    async def start_session(self, request: StartSessionRequest) -> str:
        self._record("start_session", request)
        self._next_id += 1
        session_id = f"sess-{self._next_id}"
        self.sessions[session_id] = FakeSandboxSession(request=request, egress=request.egress)
        return session_id

    async def seed_session(
        self, session_id: str, repo_archive: bytes | None = None, skills_archive: bytes | None = None
    ) -> None:
        if repo_archive is None and skills_archive is None:
            raise ValueError("seed_session requires at least one of repo_archive or skills_archive")
        self._record("seed_session", session_id)
        session = self._require("seed_session", session_id)
        if session.repo_files is not None or session.skills_files is not None:
            return
        session.repo_files = _archive_members(repo_archive)
        session.skills_files = _archive_members(skills_archive)

    async def session_exists(self, session_id: str) -> bool:
        try:
            self._record("session_exists", session_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return False
            raise
        session = self.sessions.get(session_id)
        if session is None:
            return False
        session.state = "running"
        return True

    async def close_session(self, session_id: str, *, force: bool = False) -> None:
        self._record("close_session", session_id, force)
        if session_id not in self.sessions:
            return
        if force:
            del self.sessions[session_id]
        else:
            self.sessions[session_id].state = "stopped"

    async def update_egress(self, session_id: str, egress: EgressConfigRequest) -> None:
        self._record("update_egress", session_id, egress)
        session = self._require("update_egress", session_id)
        if session.request.egress is None:
            raise self._status_error("update_egress", 409, "Session has no egress proxy")
        session.egress = egress

    async def run_commands(self, session_id: str, request: RunCommandsRequest) -> RunCommandsResponse:
        self._record("run_commands", session_id, tuple(request.commands))
        self._require("run_commands", session_id).state = "running"
        results: list[RunCommandResult] = []
        for command in request.commands:
            self.commands.append(command)
            exit_code, output = 0, ""
            for needle, (code, out) in self.responses.items():
                if needle in command:
                    exit_code, output = code, out
                    break
            results.append(RunCommandResult(command=command, output=output, exit_code=exit_code))
            if request.fail_fast and exit_code != 0:
                break
        return RunCommandsResponse(results=results)


def _archive_members(archive: bytes | None) -> frozenset[str] | None:
    if archive is None:
        return None
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tf:
        return frozenset(member.name for member in tf.getmembers() if member.isfile())


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


@pytest.fixture(autouse=True)
def _no_repo_access_backstop():
    """Neutralize the repository-access backstop across the unit suite.

    ``Activity.objects.visible_to`` routes through ``codebase.authorization`` on every
    read surface. Its backstop probe would enqueue ``sync_repository_access_cron_task``,
    which the ImmediateBackend runs synchronously against a mocked client. Patch the
    enqueue helper to a no-op and clear the once-a-minute probe marker for determinism.

    ``test_authorization.py`` re-patches the same target in a more specific autouse
    fixture it asserts on; nested patches of one target are safe.
    """
    cache.delete("repo-access:backstop-probe")
    with patch("codebase.authorization._enqueue_sync"):
        yield


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
    with patch("chat.api.threads.generate_title_task") as m3:
        m3.aenqueue = AsyncMock(return_value=None)
        with patch("sessions.services.generate_batch_title_task") as m1:
            m1.aenqueue = AsyncMock(return_value=None)
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

    Note: ``search_viewable_repositories`` / ``asearch_viewable_repositories`` are intentionally
    NOT patched here — they are catalog-backed DB queries. Tests that hit the search API, repo
    picker, or MCP ``list_repositories`` must seed ``RepositoryCatalog`` rows or patch the query
    themselves; otherwise they will see ``[]`` against an empty test catalog.
    """
    with (
        patch("sessions.services.aassert_can_run", new=AsyncMock(return_value=None)),
        patch("jobs.api.views.aassert_can_run", new=AsyncMock(return_value=None)),
        patch("mcp_server.server.aassert_can_run", new=AsyncMock(return_value=None)),
        patch("chat.api.views.aassert_can_run", new=AsyncMock(return_value=None)),
        patch("sessions.forms.assert_can_run", new=Mock(return_value=None)),
        patch("sessions.views.can_run", new=Mock(return_value=True)),
        patch("codebase.views.can_view", new=Mock(return_value=True)),
        patch("memory.views.can_view", new=Mock(return_value=True)),
        patch("memory.views.viewable_repo_ids", new=Mock(side_effect=lambda user, ids: set(ids))),
    ):
        yield


@pytest.fixture
def database_task_backend(settings):
    """Swap the suite's immediate backend for the database one production enqueues through."""
    settings.TASKS = {
        "default": {**settings.TASKS["default"], "BACKEND": "core.backends.deduplicating.DeduplicatingDatabaseBackend"}
    }


SAMPLE_QUESTION_PAYLOAD = {
    "questions": [
        {
            "header": "Database",
            "question": "Which database engine should the project move to?",
            "options": [
                {"label": "PostgreSQL", "description": "Keep a relational store with the richest Django support."},
                {"label": "SQLite", "description": "Single-file database, simplest to run."},
            ],
            "multi_select": False,
        }
    ]
}


def ask_user_question_messages(payload: dict | None = None) -> list:
    """The tail a turn leaves when it ends on a question: the call, its delivery, and the close message."""
    from automation.agent.questions import ASK_USER_QUESTION_TOOL_NAME, QUESTION_DELIVERED, render_questions

    payload = payload or SAMPLE_QUESTION_PAYLOAD
    return [
        AIMessage(content="", tool_calls=[{"id": "ask-1", "name": ASK_USER_QUESTION_TOOL_NAME, "args": payload}]),
        ToolMessage(content=QUESTION_DELIVERED, tool_call_id="ask-1", name=ASK_USER_QUESTION_TOOL_NAME),
        AIMessage(content=render_questions(payload)),
    ]
