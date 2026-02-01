from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

from langchain.tools import ToolRuntime
from langgraph.types import Command

from automation.agent.middlewares.git_platform import github_tool


class TestGitHubToolTokenCaching:
    async def test_github_tool_caches_token_in_state_and_reuses_it(self):
        runtime = ToolRuntime(
            state={},
            context=Mock(repo_id="owner/repo", git_platform=Mock()),
            config={},
            stream_writer=Mock(),
            tool_call_id=None,
            store=None,
        )

        with (
            patch(
                "automation.agent.middlewares.git_platform.get_github_cli_token", side_effect=["tok_1"]
            ) as get_token_mock,
            patch("automation.agent.middlewares.git_platform.asyncio.create_subprocess_exec") as create_proc_mock,
        ):
            proc = Mock()
            proc.communicate = AsyncMock(return_value=(b"ok\n", b""))
            proc.returncode = 0
            create_proc_mock.return_value = proc

            result1 = await github_tool.coroutine(subcommand="issue view 1", runtime=runtime)  # type: ignore[union-attr]
            # Handle Command return - extract output and apply state update
            if isinstance(result1, Command):
                assert result1.update is not None
                runtime.state.update(result1.update)
                out1 = result1.resume
            else:
                out1 = result1

            result2 = await github_tool.coroutine(subcommand="issue view 2", runtime=runtime)  # type: ignore[union-attr]
            # Handle Command return - extract output and apply state update
            if isinstance(result2, Command):
                assert result2.update is not None
                runtime.state.update(result2.update)
                out2 = result2.resume
            else:
                out2 = result2

        assert out1 == "ok"
        assert out2 == "ok"

        # Cached token should avoid extra token generation.
        assert get_token_mock.call_count == 1
        assert runtime.state["github_token"] == "tok_1"  # noqa: S105
        assert runtime.state["github_token_cached_at"] is not None

    async def test_github_tool_refreshes_token_after_cache_ttl(self):
        runtime = ToolRuntime(
            state={"github_token": "tok_old", "github_token_cached_at": 0.0},
            context=Mock(repo_id="owner/repo", git_platform=Mock()),
            config={},
            stream_writer=Mock(),
            tool_call_id=None,
            store=None,
        )

        with (
            patch(
                "automation.agent.middlewares.git_platform.get_github_cli_token", side_effect=["tok_new"]
            ) as get_token_mock,
            patch("automation.agent.middlewares.git_platform.time.time", return_value=56 * 60),
            patch("automation.agent.middlewares.git_platform.asyncio.create_subprocess_exec") as create_proc_mock,
        ):
            proc = Mock()
            proc.communicate = AsyncMock(return_value=(b"ok\n", b""))
            proc.returncode = 0
            create_proc_mock.return_value = proc

            result = await github_tool.coroutine(subcommand="issue view 1", runtime=runtime)  # type: ignore[union-attr]
            # Handle Command return - apply state update
            if isinstance(result, Command) and result.update is not None:
                runtime.state.update(result.update)

        assert get_token_mock.call_count == 1
        assert runtime.state["github_token"] == "tok_new"  # noqa: S105

    async def test_token_not_in_tool_output(self):
        runtime = ToolRuntime(
            state={},
            context=Mock(repo_id="owner/repo", git_platform=Mock()),
            config={},
            stream_writer=Mock(),
            tool_call_id=None,
            store=None,
        )

        with (
            patch("automation.agent.middlewares.git_platform.get_github_cli_token", return_value="tok_1"),
            patch("automation.agent.middlewares.git_platform.asyncio.create_subprocess_exec") as create_proc_mock,
        ):
            proc = Mock()
            proc.communicate = AsyncMock(return_value=(b"ok\n", b""))
            proc.returncode = 0
            create_proc_mock.return_value = proc

            result = await github_tool.coroutine(subcommand="issue view 1", runtime=runtime)  # type: ignore[union-attr]
            # Handle Command return - extract output
            out = result.resume if isinstance(result, Command) else result

        assert out == "ok"
        assert "tok_1" not in out
