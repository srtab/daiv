from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from langchain.tools import ToolRuntime
from sessions.models import Session, SessionOrigin

from accounts.credentials import ResolvedCredential
from automation.agent.middlewares.git_platform.github import run_github_subcommand
from automation.agent.middlewares.git_platform.gitlab import run_gitlab_subcommand
from codebase.base import GitPlatform
from core.constants import CROSS_PROJECT_CONTENT_MARKER
from tests.unit_tests.conftest import recording_backend


def gitlab_runtime(repo_slug: str = "group/repo") -> ToolRuntime:
    return ToolRuntime(
        state={},
        context=Mock(repository=Mock(slug=repo_slug), git_platform=GitPlatform.GITLAB),
        config={},
        stream_writer=Mock(),
        tool_call_id="test_call_gitlab",
        store=None,
    )


def github_runtime(*, thread_id: str, tool_call_id: str = "c1", state: dict | None = None) -> ToolRuntime:
    """``thread_id`` keys the cached CLI token, so a test that must fetch its own token needs its own."""
    return ToolRuntime(
        state=state if state is not None else {},
        context=Mock(repository=Mock(slug="owner/repo"), git_platform=GitPlatform.GITHUB),
        config={"configurable": {"thread_id": thread_id}},
        stream_writer=Mock(),
        tool_call_id=tool_call_id,
        store=None,
    )


@contextmanager
def cli_process(stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
    """Make the CLI subprocess finish with this output; yields the ``create_subprocess_exec`` mock."""
    proc = Mock(communicate=AsyncMock(return_value=(stdout, stderr)), returncode=returncode)
    with patch(
        "automation.agent.middlewares.git_platform.cli.asyncio.create_subprocess_exec", return_value=proc
    ) as create_proc:
        yield create_proc


@pytest.fixture
def gitlab_settings():
    mock_settings = Mock()
    mock_settings.GITLAB_AUTH_TOKEN.get_secret_value.return_value = "test-token"  # noqa: S106
    mock_settings.GITLAB_URL.encoded_string.return_value = "https://gitlab.com"
    with patch("automation.agent.middlewares.git_platform.gitlab.settings", mock_settings):
        yield mock_settings


VALID_POSITION = {
    "position_type": "text",
    "base_sha": "aaa",
    "start_sha": "bbb",
    "head_sha": "ccc",
    "old_path": "src/foo.py",
    "new_path": "src/foo.py",
    "new_line": 42,
}


ATTACHED = "group/repo"
OTHER = "other-group/other-repo"
CROSS_PROJECT = "automation.agent.middlewares.git_platform.cross_project"


def cross_project_runtime(
    platform: GitPlatform,
    *,
    acting_user_id: int | None = 7,
    acting_platform_uid: str | None = None,
    acting_user_authenticated: bool = True,
    repo_slug: str = ATTACHED,
) -> ToolRuntime:
    context = Mock(
        repository=Mock(slug=repo_slug),
        git_platform=platform,
        acting_user_id=acting_user_id,
        acting_platform_uid=acting_platform_uid,
        acting_user_authenticated=acting_user_authenticated,
    )
    return ToolRuntime(
        state={},
        context=context,
        config={"configurable": {"thread_id": "t-xproj"}},
        stream_writer=Mock(),
        tool_call_id="c-xproj",
        store=None,
    )


def installation_runtime() -> ToolRuntime:
    """A GitHub runtime whose installation token is already cached in state."""
    runtime = cross_project_runtime(GitPlatform.GITHUB)
    runtime.state["github_token"] = "install-token"  # noqa: S105
    runtime.state["github_token_expires_at"] = 9999999999.0
    return runtime


def service_settings() -> Mock:
    mock_settings = Mock()
    mock_settings.GITLAB_AUTH_TOKEN.get_secret_value.return_value = "service-token"  # noqa: S106
    mock_settings.GITLAB_URL.encoded_string.return_value = "https://gitlab.com"
    return mock_settings


@contextmanager
def patched_cross_project(*, resolved=None, returncode=0, stdout=b"ok\n", stderr=b""):
    """Patch the credential service, the audit writer and the subprocess in one place."""
    with (
        cli_process(stdout=stdout, stderr=stderr, returncode=returncode) as create_proc,
        patch("automation.agent.middlewares.git_platform.gitlab.settings", service_settings()),
        patch(f"{CROSS_PROJECT}.aresolve_access_token") as resolve_mock,
        patch(f"{CROSS_PROJECT}._record_cross_project_access") as record_mock,
        patch(f"{CROSS_PROJECT}.arenew_rejected_token") as renew_mock,
        patch(f"{CROSS_PROJECT}._acting_person_label", AsyncMock(return_value="Ada")),
    ):
        resolve_mock.return_value = resolved if resolved is not None else ResolvedCredential(token="person-token")  # noqa: S106
        record_mock.return_value = True
        renew_mock.return_value = None
        yield SimpleNamespace(create_proc=create_proc, resolve=resolve_mock, record=record_mock, renew=renew_mock)


def marked(argv, text):
    return [arg for arg in argv if text in arg and arg.endswith(f"\n\n{CROSS_PROJECT_CONTENT_MARKER}")]


async def run_gl(subcommand, runtime, *, output_mode="simplified", to_file=False, backend=None, project=""):
    return await run_gitlab_subcommand(
        subcommand,
        runtime,
        output_mode,
        to_file,
        backend=backend if backend is not None else recording_backend(),
        project=project,
    )


async def run_gh(subcommand, runtime, *, to_file=False, backend=None, project="", cross_project_enabled=False):
    return await run_github_subcommand(
        subcommand,
        runtime,
        to_file,
        backend=backend if backend is not None else recording_backend(),
        project=project,
        cross_project_enabled=cross_project_enabled,
    )


@pytest.fixture
def xproj_session(transactional_db):
    """The session of ``cross_project_runtime``'s thread: an allowed fetch restricts it, else its result is withheld."""
    return Session.objects.create(thread_id="t-xproj", origin=SessionOrigin.CHAT, repo_id=ATTACHED)
