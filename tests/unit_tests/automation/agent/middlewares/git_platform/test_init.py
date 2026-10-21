from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from deepagents.backends.composite import CompositeBackend
from langchain_core.utils.function_calling import convert_to_openai_tool

from automation.agent.middlewares.git_platform import GitPlatformMiddleware
from codebase.base import GitPlatform
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
