from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

from langchain.tools import ToolRuntime

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

            out1 = await github_tool.coroutine(subcommand="issue view 1", runtime=runtime)  # type: ignore[union-attr]
            out2 = await github_tool.coroutine(subcommand="issue view 2", runtime=runtime)  # type: ignore[union-attr]

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

            _ = await github_tool.coroutine(subcommand="issue view 1", runtime=runtime)  # type: ignore[union-attr]

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

            out = await github_tool.coroutine(subcommand="issue view 1", runtime=runtime)  # type: ignore[union-attr]

        assert out == "ok"
        assert "tok_1" not in out
