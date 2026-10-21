from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from langchain_core.utils.function_calling import convert_to_openai_tool

from automation.agent.middlewares.git_platform import GitPlatformMiddleware
from codebase.base import GitPlatform
from tests.unit_tests.automation.agent.middlewares.git_platform.conftest import cli_process, gitlab_runtime
from tests.unit_tests.conftest import recording_backend

MODEL_VIEW = Path(__file__).parent / "data" / "model_view.json"


async def _model_view() -> dict:
    """Per platform, the tools as sent to the model and the system prompt the middleware appends."""
    view = {}
    for platform in (GitPlatform.GITLAB, GitPlatform.GITHUB):
        middleware = GitPlatformMiddleware(git_platform=platform, backend=recording_backend())
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


@pytest.mark.usefixtures("gitlab_settings")
async def test_the_gitlab_tool_writes_through_the_middleware_backend():
    backend = recording_backend()
    mw = GitPlatformMiddleware(git_platform=GitPlatform.GITLAB, backend=backend)

    with cli_process(stdout=b'[{"iid": 1}]\n'):
        result = await mw.tools[0].coroutine(
            subcommand="project-merge-request list --state opened", runtime=gitlab_runtime(), output_to_file=True
        )

    path, content = backend.awrite.call_args.args
    assert path == "/workspace/large_tool_results/test_call_gitlab"
    assert content == '[{"iid": 1}]'
    assert result.startswith("Wrote ")
