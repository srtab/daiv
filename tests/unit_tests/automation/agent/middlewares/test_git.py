from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from automation.agent.middlewares.git import GitMiddleware
from codebase.base import Scope
from codebase.utils import GitPushPermissionError


def _make_runtime(*, scope: Scope = Scope.ISSUE) -> Mock:
    runtime = Mock()
    runtime.context = Mock()
    runtime.context.scope = scope
    runtime.context.merge_request = None
    runtime.context.repository = Mock(slug="a/b")
    runtime.context.config = Mock(default_branch="main")
    runtime.context.gitrepo = Mock()
    return runtime


class TestGitMiddleware:
    async def test_aafter_agent_propagates_push_permission_error(self):
        middleware = GitMiddleware()
        runtime = _make_runtime()

        with (
            patch(
                "automation.agent.middlewares.git.GitChangePublisher.publish",
                new=AsyncMock(side_effect=GitPushPermissionError("No permission to push")),
            ),
            pytest.raises(GitPushPermissionError),
        ):
            await middleware.aafter_agent(state={"merge_request": None}, runtime=runtime)

    async def test_abefore_agent_seeds_open_mr_when_state_empty(self):
        """No MR in state → seed it so it streams via STATE_SNAPSHOT."""
        middleware = GitMiddleware()
        runtime = _make_runtime(scope=Scope.GLOBAL)
        existing_mr = MagicMock(source_branch="feature-x")

        with (
            patch("automation.agent.middlewares.git.get_repo_ref", return_value="feature-x"),
            patch(
                "automation.agent.middlewares.git.GitMiddleware._alookup_open_mr",
                new=AsyncMock(return_value=existing_mr),
            ) as lookup,
        ):
            result = await middleware.abefore_agent({}, runtime)

        lookup.assert_awaited_once_with(runtime.context)
        assert result == {"merge_request": existing_mr, "code_changes": False}

    async def test_abefore_agent_skips_lookup_when_state_has_mr(self):
        middleware = GitMiddleware()
        runtime = _make_runtime(scope=Scope.GLOBAL)
        state_mr = MagicMock(source_branch="feature-x")

        with (
            patch("automation.agent.middlewares.git.get_repo_ref", return_value="feature-x"),
            patch("automation.agent.middlewares.git.GitMiddleware._alookup_open_mr", new=AsyncMock()) as lookup,
        ):
            result = await middleware.abefore_agent({"merge_request": state_mr}, runtime)

        lookup.assert_not_called()
        assert result["merge_request"] is state_mr

    async def test_abefore_agent_runtime_context_overrides_state_in_mr_scope(self):
        middleware = GitMiddleware()
        runtime = _make_runtime(scope=Scope.MERGE_REQUEST)
        runtime.context.merge_request = MagicMock(source_branch="feature-y")
        stale_state_mr = MagicMock(source_branch="feature-x")

        with (
            patch("automation.agent.middlewares.git.get_repo_ref", return_value="feature-y"),
            patch("automation.agent.middlewares.git.GitMiddleware._alookup_open_mr", new=AsyncMock()) as lookup,
        ):
            result = await middleware.abefore_agent({"merge_request": stale_state_mr}, runtime)

        lookup.assert_not_called()
        assert result["merge_request"] is runtime.context.merge_request

    async def test_alookup_open_mr_returns_platform_mr(self):
        runtime = _make_runtime()
        existing_mr = MagicMock(source_branch="feature-x")
        client = MagicMock()
        client.get_merge_request_by_branches = MagicMock(return_value=existing_mr)

        with (
            patch("automation.agent.middlewares.git.get_repo_ref", return_value="feature-x"),
            patch("automation.agent.middlewares.git.RepoClient.create_instance", return_value=client),
        ):
            mr = await GitMiddleware._alookup_open_mr(runtime.context)  # noqa: SLF001

        assert mr is existing_mr
        client.get_merge_request_by_branches.assert_called_once_with("a/b", "feature-x", "main")

    async def test_alookup_open_mr_skips_default_branch(self):
        runtime = _make_runtime()

        with (
            patch("automation.agent.middlewares.git.get_repo_ref", return_value="main"),
            patch("automation.agent.middlewares.git.RepoClient.create_instance") as create,
        ):
            mr = await GitMiddleware._alookup_open_mr(runtime.context)  # noqa: SLF001

        assert mr is None
        create.assert_not_called()

    async def test_alookup_open_mr_swallows_platform_errors(self):
        runtime = _make_runtime()
        client = MagicMock()
        client.get_merge_request_by_branches = MagicMock(side_effect=RuntimeError("gitlab down"))

        with (
            patch("automation.agent.middlewares.git.get_repo_ref", return_value="feature-x"),
            patch("automation.agent.middlewares.git.RepoClient.create_instance", return_value=client),
        ):
            mr = await GitMiddleware._alookup_open_mr(runtime.context)  # noqa: SLF001

        assert mr is None
