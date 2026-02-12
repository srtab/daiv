from unittest.mock import AsyncMock, Mock, patch

from automation.agent.middlewares.git import GitMiddleware
from codebase.base import Scope
from codebase.utils import GitPushPermissionError


def _make_runtime(*, scope: Scope = Scope.ISSUE) -> Mock:
    runtime = Mock()
    runtime.context = Mock()
    runtime.context.scope = scope
    return runtime


class TestGitMiddleware:
    async def test_aafter_agent_returns_none_when_publish_fails_with_push_permission_error(self):
        middleware = GitMiddleware()
        runtime = _make_runtime()

        with patch(
            "automation.agent.middlewares.git.GitChangePublisher.publish",
            new=AsyncMock(side_effect=GitPushPermissionError("No permission to push")),
        ):
            result = await middleware.aafter_agent(state={"merge_request": None}, runtime=runtime)

        assert result is None
