from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import AsyncMock, Mock, patch

import pytest
from langchain.tools import ToolRuntime

from codebase.base import GitPlatform


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
