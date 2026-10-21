from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from deepagents.backends.composite import CompositeBackend
from langchain_core.utils.function_calling import convert_to_openai_tool

from automation.agent.middlewares.git_platform import GitPlatformMiddleware
from codebase.base import GitPlatform
from tests.unit_tests.automation.agent.middlewares.git_platform.conftest import gitlab_runtime
from tests.unit_tests.conftest import unacquired_backend

MODEL_VIEW = Path(__file__).parent / "data" / "model_view.json"


async def _model_view() -> dict:
    """Per platform, the tools as sent to the model and the system prompt the middleware appends."""
    view = {}
    for platform in (GitPlatform.GITLAB, GitPlatform.GITHUB):
        backend = CompositeBackend(default=unacquired_backend(), routes={}, artifacts_root="/workspace")
        middleware = GitPlatformMiddleware(git_platform=platform, backend=backend)
        request = Mock(system_prompt="")
        request.runtime.context.git_platform = platform
        request.override = lambda **overrides: overrides
        overrides = await middleware.awrap_model_call(request, AsyncMock(side_effect=lambda r: r))
        view[platform.value] = {
            "tools": [convert_to_openai_tool(tool) for tool in middleware.tools],
            "system_prompt": overrides["system_prompt"],
        }
    return view


async def test_the_tools_and_prompt_the_model_sees_match_the_golden_file():
    """An intended change to a tool or the prompt rewrites ``data/model_view.json`` from ``_model_view()``."""
    assert await _model_view() == json.loads(MODEL_VIEW.read_text())


class TestGitPlatformMiddlewareWiring:
    def test_builds_gitlab_tool_and_prefix_from_backend(self):
        backend = CompositeBackend(default=unacquired_backend(), routes={}, artifacts_root="/workspace")
        mw = GitPlatformMiddleware(git_platform=GitPlatform.GITLAB, backend=backend)
        assert mw._large_tool_results_prefix == "/workspace/large_tool_results"
        assert [t.name for t in mw.tools] == ["gitlab"]

    def test_builds_github_tool(self):
        backend = CompositeBackend(default=unacquired_backend(), routes={}, artifacts_root="/workspace")
        mw = GitPlatformMiddleware(git_platform=GitPlatform.GITHUB, backend=backend)
        assert [t.name for t in mw.tools] == ["gh"]

    async def test_gitlab_closure_forwards_backend_and_prefix_end_to_end(self):
        """Invoking the closure-built gitlab tool (not the underscore helper) must write through
        the middleware's own backend, at the prefix derived from that backend's artifacts_root —
        proving the closure captured and forwarded both ``backend`` and ``large_tool_results_prefix``."""
        backend = CompositeBackend(default=unacquired_backend(), routes={}, artifacts_root="/workspace")
        backend.awrite = AsyncMock(return_value=Mock(error=None))
        mw = GitPlatformMiddleware(git_platform=GitPlatform.GITLAB, backend=backend)

        runtime = gitlab_runtime()  # tool_call_id="test_call_gitlab"
        mock_settings = Mock()
        mock_settings.GITLAB_AUTH_TOKEN.get_secret_value.return_value = "test-token"  # noqa: S106
        mock_settings.GITLAB_URL.encoded_string.return_value = "https://gitlab.com"

        with (
            patch("automation.agent.middlewares.git_platform.cli.asyncio.create_subprocess_exec") as create_proc,
            patch("automation.agent.middlewares.git_platform.gitlab.settings", mock_settings),
        ):
            proc = Mock()
            proc.communicate = AsyncMock(return_value=(b'[{"iid": 1}]\n', b""))
            proc.returncode = 0
            create_proc.return_value = proc

            result = await mw.tools[0].coroutine(
                subcommand="project-merge-request list --state opened", runtime=runtime, output_to_file=True
            )

        path, content = backend.awrite.call_args.args
        assert path == "/workspace/large_tool_results/test_call_gitlab"
        assert content == '[{"iid": 1}]'
        assert result.startswith("Wrote ")
