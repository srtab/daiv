from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from langchain.tools import ToolRuntime
from langgraph.types import Command

from automation.agent.middlewares.git_platform.cli import _is_allowed_cli_command
from automation.agent.middlewares.git_platform.github import GITHUB_CLI_ALLOW_COMMANDS, _run_github_subcommand
from codebase.base import GitPlatform
from tests.unit_tests.automation.agent.middlewares.git_platform.conftest import LARGE_TOOL_RESULTS_PREFIX, mock_backend


async def _run_gh(subcommand, runtime, *, to_file=False, backend=None):
    """Invoke the gh tool implementation with a default mock backend + results prefix."""
    return await _run_github_subcommand(
        subcommand,
        runtime,
        to_file,
        backend=backend if backend is not None else mock_backend(),
        large_tool_results_prefix=LARGE_TOOL_RESULTS_PREFIX,
    )


@patch("automation.agent.middlewares.git_platform.github.cache.lock", new=MagicMock())
class TestGitHubToolTokenCaching:
    async def test_github_tool_caches_token_in_state_and_reuses_it(self):
        runtime = ToolRuntime(
            state={},
            context=Mock(repo_id="owner/repo", git_platform=GitPlatform.GITHUB),
            config={"configurable": {"thread_id": "test-thread-1"}},
            stream_writer=Mock(),
            tool_call_id="test_call_1",
            store=None,
        )

        with (
            patch("automation.agent.middlewares.git_platform.github.get_github_integration") as get_integration_mock,
            patch("automation.agent.middlewares.git_platform.cli.asyncio.create_subprocess_exec") as create_proc_mock,
        ):
            access_token = Mock(token="tok_1", expires_at=Mock(timestamp=Mock(return_value=9999999999.0)))  # noqa: S106
            get_integration_mock.return_value.get_access_token.return_value = access_token

            proc = Mock()
            proc.communicate = AsyncMock(return_value=(b"ok\n", b""))
            proc.returncode = 0
            create_proc_mock.return_value = proc

            result1 = await _run_gh("issue view 1", runtime)
            # Handle Command return - extract output and apply state update
            if isinstance(result1, Command):
                assert result1.update is not None
                # Extract output from ToolMessage in messages
                messages = result1.update.get("messages", [])
                assert len(messages) == 1
                out1 = messages[0].content
                # Apply state updates (excluding messages)
                state_updates = {k: v for k, v in result1.update.items() if k != "messages"}
                runtime.state.update(state_updates)
            else:
                out1 = result1

            result2 = await _run_gh("issue view 2", runtime)
            # Handle Command return - extract output and apply state update
            if isinstance(result2, Command):
                assert result2.update is not None
                # Extract output from ToolMessage in messages
                messages = result2.update.get("messages", [])
                assert len(messages) == 1
                out2 = messages[0].content
                # Apply state updates (excluding messages)
                state_updates = {k: v for k, v in result2.update.items() if k != "messages"}
                runtime.state.update(state_updates)
            else:
                out2 = result2

        assert out1 == "ok"
        assert out2 == "ok"

        # Cached token should avoid extra token generation.
        assert get_integration_mock.return_value.get_access_token.call_count == 1
        assert runtime.state["github_token"] == "tok_1"  # noqa: S105
        assert runtime.state["github_token_expires_at"] is not None

    async def test_github_tool_refreshes_token_after_cache_ttl(self):
        runtime = ToolRuntime(
            state={"github_token": "tok_old", "github_token_expires_at": 0.0},
            context=Mock(repo_id="owner/repo", git_platform=GitPlatform.GITHUB),
            config={"configurable": {"thread_id": "test-thread-2"}},
            stream_writer=Mock(),
            tool_call_id="test_call_2",
            store=None,
        )

        with (
            patch("automation.agent.middlewares.git_platform.github.get_github_integration") as get_integration_mock,
            patch("automation.agent.middlewares.git_platform.cli.asyncio.create_subprocess_exec") as create_proc_mock,
        ):
            access_token = Mock(token="tok_new", expires_at=Mock(timestamp=Mock(return_value=9999999999.0)))  # noqa: S106
            get_integration_mock.return_value.get_access_token.return_value = access_token

            proc = Mock()
            proc.communicate = AsyncMock(return_value=(b"ok\n", b""))
            proc.returncode = 0
            create_proc_mock.return_value = proc

            result = await _run_gh("issue view 1", runtime)
            # Handle Command return - apply state update
            if isinstance(result, Command) and result.update is not None:
                # Apply state updates (excluding messages)
                state_updates = {k: v for k, v in result.update.items() if k != "messages"}
                runtime.state.update(state_updates)

        assert get_integration_mock.return_value.get_access_token.call_count == 1
        assert runtime.state["github_token"] == "tok_new"  # noqa: S105

    async def test_token_not_in_tool_output(self):
        runtime = ToolRuntime(
            state={},
            context=Mock(repo_id="owner/repo", git_platform=GitPlatform.GITHUB),
            config={"configurable": {"thread_id": "test-thread-3"}},
            stream_writer=Mock(),
            tool_call_id="test_call_3",
            store=None,
        )

        with (
            patch("automation.agent.middlewares.git_platform.github.get_github_integration") as get_integration_mock,
            patch("automation.agent.middlewares.git_platform.cli.asyncio.create_subprocess_exec") as create_proc_mock,
        ):
            access_token = Mock(token="tok_1", expires_at=Mock(timestamp=Mock(return_value=9999999999.0)))  # noqa: S106
            get_integration_mock.return_value.get_access_token.return_value = access_token

            proc = Mock()
            proc.communicate = AsyncMock(return_value=(b"ok\n", b""))
            proc.returncode = 0
            create_proc_mock.return_value = proc

            result = await _run_gh("issue view 1", runtime)
            # Handle Command return - extract output
            if isinstance(result, Command):
                assert result.update is not None
                # Extract output from ToolMessage in messages
                messages = result.update.get("messages", [])
                assert len(messages) == 1
                out = messages[0].content
            else:
                out = result

        assert out == "ok"
        assert "tok_1" not in out


@patch("automation.agent.middlewares.git_platform.github.cache.lock", new=MagicMock())
class TestGitHubToolOutputToFile:
    async def test_github_output_to_file_writes_verbatim_and_wraps_in_command(self):
        runtime = ToolRuntime(
            state={"session_id": "sess-1"},
            context=Mock(repo_id="owner/repo", git_platform=GitPlatform.GITHUB),
            config={"configurable": {"thread_id": "t-gh-of"}},
            stream_writer=Mock(),
            tool_call_id="c1",
            store=None,
        )
        backend = mock_backend()
        payload = b'{"number": 7}\n'

        with (
            patch("automation.agent.middlewares.git_platform.github.get_github_integration") as gi,
            patch("automation.agent.middlewares.git_platform.cli.asyncio.create_subprocess_exec") as create_proc,
        ):
            gi.return_value.get_access_token.return_value = Mock(
                token="tok",  # noqa: S106
                expires_at=Mock(timestamp=Mock(return_value=9999999999.0)),
            )
            proc = Mock()
            proc.communicate = AsyncMock(return_value=(payload, b""))
            proc.returncode = 0
            create_proc.return_value = proc

            result = await _run_gh("pr view 7 --json number", runtime, to_file=True, backend=backend)

        # gh is written verbatim — the tool never injects a global --output flag
        argv = list(create_proc.call_args.args)
        assert "--output" not in argv

        path, content = backend.awrite.call_args.args
        assert path == "/workspace/large_tool_results/c1"
        assert content == '{"number": 7}'

        # token was refreshed → Command, and its ToolMessage carries the confirmation
        assert isinstance(result, Command)
        msg = result.update["messages"][0].content
        assert msg.startswith("Wrote ")
        assert "/workspace/large_tool_results/c1" in msg

    async def test_github_empty_output_to_file_notes_no_file_written(self):
        """When gh returns empty stdout and output_to_file is true, the result must contain
        both the 'empty result' sentinel and a note that no file was written."""
        runtime = ToolRuntime(
            state={"session_id": "sess-1", "github_token": "tok", "github_token_expires_at": 9999999999.0},
            context=Mock(repo_id="owner/repo", git_platform=GitPlatform.GITHUB),
            config={"configurable": {"thread_id": "t-gh-empty"}},
            stream_writer=Mock(),
            tool_call_id="c3",
            store=None,
        )
        backend = mock_backend()
        with patch("automation.agent.middlewares.git_platform.cli.asyncio.create_subprocess_exec") as create_proc:
            proc = Mock()
            proc.communicate = AsyncMock(return_value=(b"", b""))
            proc.returncode = 0
            create_proc.return_value = proc

            result = await _run_gh("issue list --state open", runtime, to_file=True, backend=backend)

        # Token was already cached → plain string (no Command)
        assert isinstance(result, str)
        assert "empty result" in result
        assert "no file was written" in result
        backend.awrite.assert_not_called()


@patch("automation.agent.middlewares.git_platform.github.cache.lock", new=MagicMock())
class TestGitHubToolOutputToFileExtra:
    async def test_github_run_view_log_to_file(self):
        """gh run view --log to_file: clean_job_logs runs; file written, confirmation returned."""
        runtime = ToolRuntime(
            state={"session_id": "sess-2", "github_token": "tok", "github_token_expires_at": 9999999999.0},
            context=Mock(repo_id="owner/repo", git_platform=GitPlatform.GITHUB),
            config={"configurable": {"thread_id": "t-gh-log"}},
            stream_writer=Mock(),
            tool_call_id="c-log",
            store=None,
        )
        backend = mock_backend()
        log_output = b"2024-01-01T00:00:00.000Z job1\tsome log line\n"

        with (
            patch("automation.agent.middlewares.git_platform.cli.asyncio.create_subprocess_exec") as create_proc,
            patch(
                "automation.agent.middlewares.git_platform.cli.clean_job_logs", return_value="some log line"
            ) as mock_clean,
        ):
            proc = Mock()
            proc.communicate = AsyncMock(return_value=(log_output, b""))
            proc.returncode = 0
            create_proc.return_value = proc

            result = await _run_gh("run view 123 --job 456 --log", runtime, to_file=True, backend=backend)

        # Token was already cached → plain string (no Command)
        assert isinstance(result, str)
        assert result.startswith("Wrote ")
        assert backend.awrite.call_args.args[0] == "/workspace/large_tool_results/c-log"
        mock_clean.assert_called_once()

    async def test_github_output_to_file_cached_token_plain_string(self):
        """gh output_to_file with a cached valid token returns a plain str (no Command)."""
        runtime = ToolRuntime(
            state={"session_id": "sess-3", "github_token": "tok", "github_token_expires_at": 9999999999.0},
            context=Mock(repo_id="owner/repo", git_platform=GitPlatform.GITHUB),
            config={"configurable": {"thread_id": "t-gh-cached"}},
            stream_writer=Mock(),
            tool_call_id="c-cached",
            store=None,
        )
        backend = mock_backend()
        payload = b'{"number": 7}\n'

        with patch("automation.agent.middlewares.git_platform.cli.asyncio.create_subprocess_exec") as create_proc:
            proc = Mock()
            proc.communicate = AsyncMock(return_value=(payload, b""))
            proc.returncode = 0
            create_proc.return_value = proc

            result = await _run_gh("pr view 7 --json number", runtime, to_file=True, backend=backend)

        assert isinstance(result, str)
        assert result.startswith("Wrote ")
        assert backend.awrite.call_args.args[0] == "/workspace/large_tool_results/c-cached"


class TestGitHubReleasePolicy:
    @pytest.mark.parametrize(("action", "expected"), [("create", True), ("delete", False)])
    def test_release_actions_follow_policy(self, action, expected):
        allowed, _ = _is_allowed_cli_command("release", action, GITHUB_CLI_ALLOW_COMMANDS)
        assert allowed is expected

    async def test_release_create_reaches_the_cli(self):
        context = Mock(git_platform=GitPlatform.GITHUB)
        context.repository.slug = "owner/repo"
        runtime = ToolRuntime(
            state={"github_token": "tok", "github_token_expires_at": 9999999999.0},
            context=context,
            config={"configurable": {"thread_id": "t-gh-release"}},
            stream_writer=Mock(),
            tool_call_id="c-release",
            store=None,
        )

        with patch("automation.agent.middlewares.git_platform.cli.asyncio.create_subprocess_exec") as create_proc:
            proc = Mock()
            proc.communicate = AsyncMock(return_value=(b"ok\n", b""))
            proc.returncode = 0
            create_proc.return_value = proc

            await _run_gh('release create v1.0.0 --title "v1.0.0" --notes "notes"', runtime)

        assert create_proc.call_args.args[:3] == ("gh", "release", "create")


class TestPublishedTextIsRepoRelative:
    async def test_a_github_comment_body_is_repo_relative(self):
        runtime = ToolRuntime(
            state={"github_token": "tok", "github_token_expires_at": 9999999999.0},
            context=Mock(repo_id="owner/repo", git_platform=GitPlatform.GITHUB),
            config={"configurable": {"thread_id": "t"}},
            stream_writer=Mock(),
            tool_call_id="c1",
            store=None,
        )
        with patch("automation.agent.middlewares.git_platform.cli.asyncio.create_subprocess_exec") as create_proc:
            proc = Mock()
            proc.communicate = AsyncMock(return_value=(b"ok\n", b""))
            proc.returncode = 0
            create_proc.return_value = proc
            await _run_gh('issue comment 1 -b "See /workspace/repo/daiv/x.py"', runtime)

        argv = list(create_proc.call_args.args)
        assert argv[argv.index("-b") + 1] == "See daiv/x.py"


async def test_github_failure_keeps_the_command_output():
    """``gh pr checks`` exits 1 when a check failed and prints the checks table on stdout, not stderr."""
    runtime = ToolRuntime(
        state={"session_id": "sess-4", "github_token": "tok", "github_token_expires_at": 4102444800.0},
        context=Mock(repo_id="owner/repo", git_platform=GitPlatform.GITHUB),
        config={"configurable": {"thread_id": "t-gh-fail"}},
        stream_writer=Mock(),
        tool_call_id="c-fail",
        store=None,
    )
    table = b"tests\tfail\t3m2s\thttps://github.com/owner/repo/actions/runs/1/job/2\nlint\tpass\t40s\t\n"

    with patch("automation.agent.middlewares.git_platform.cli.asyncio.create_subprocess_exec") as create_proc:
        proc = Mock()
        proc.communicate = AsyncMock(return_value=(table, b""))
        proc.returncode = 1
        create_proc.return_value = proc

        result = await _run_gh("pr checks 1650", runtime)

    assert result.startswith("error: GitHub command failed (exit code 1).")
    assert "tests\tfail\t3m2s\thttps://github.com/owner/repo/actions/runs/1/job/2" in result
