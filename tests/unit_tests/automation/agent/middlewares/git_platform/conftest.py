from __future__ import annotations

from unittest.mock import AsyncMock, Mock

from langchain.tools import ToolRuntime

from codebase.base import GitPlatform

LARGE_TOOL_RESULTS_PREFIX = "/workspace/large_tool_results"


def mock_backend(*, error: str | None = None):
    """Filesystem backend stub whose ``awrite`` records calls and returns a WriteResult-like obj."""
    backend = Mock()
    backend.awrite = AsyncMock(return_value=Mock(error=error))
    return backend


def gitlab_runtime(repo_slug: str = "group/repo") -> ToolRuntime:
    return ToolRuntime(
        state={},
        context=Mock(repository=Mock(slug=repo_slug), git_platform=GitPlatform.GITLAB),
        config={},
        stream_writer=Mock(),
        tool_call_id="test_call_gitlab",
        store=None,
    )
