from __future__ import annotations

from unittest.mock import MagicMock, Mock, patch

import pytest
from langgraph.types import Command

from automation.agent.middlewares.git_platform.cli import _is_allowed_cli_command
from automation.agent.middlewares.git_platform.github import GITHUB_CLI_ALLOW_COMMANDS, run_github_subcommand
from tests.unit_tests.automation.agent.middlewares.git_platform.conftest import cli_process, github_runtime
from tests.unit_tests.conftest import recording_backend

CACHED_TOKEN = {"github_token": "tok", "github_token_expires_at": 9999999999.0}


async def _run_gh(subcommand, runtime, *, to_file=False, backend=None):
    return await run_github_subcommand(
        subcommand, runtime, to_file, backend=backend if backend is not None else recording_backend()
    )


def _fresh_token(get_integration, token: str) -> None:
    get_integration.return_value.get_access_token.return_value = Mock(
        token=token, expires_at=Mock(timestamp=Mock(return_value=9999999999.0))
    )


def _output_applying_state(result, runtime) -> str:
    """The tool's output, with any state update it returned applied to ``runtime.state``, as the graph would."""
    if not isinstance(result, Command):
        return result
    messages = result.update["messages"]
    assert len(messages) == 1
    runtime.state.update({k: v for k, v in result.update.items() if k != "messages"})
    return messages[0].content


@patch("automation.agent.middlewares.git_platform.github.cache.lock", new=MagicMock())
class TestGitHubToolTokenCaching:
    async def test_github_tool_caches_token_in_state_and_reuses_it(self):
        runtime = github_runtime(thread_id="test-thread-1", tool_call_id="test_call_1")

        with (
            patch("automation.agent.middlewares.git_platform.github.get_github_integration") as get_integration,
            cli_process(stdout=b"ok\n"),
        ):
            _fresh_token(get_integration, "tok_1")

            out1 = _output_applying_state(await _run_gh("issue view 1", runtime), runtime)
            out2 = _output_applying_state(await _run_gh("issue view 2", runtime), runtime)

        assert out1 == "ok"
        assert out2 == "ok"

        # Cached token should avoid extra token generation.
        assert get_integration.return_value.get_access_token.call_count == 1
        assert runtime.state["github_token"] == "tok_1"  # noqa: S105
        assert runtime.state["github_token_expires_at"] is not None

    async def test_github_tool_refreshes_token_after_cache_ttl(self):
        runtime = github_runtime(
            thread_id="test-thread-2",
            tool_call_id="test_call_2",
            state={"github_token": "tok_old", "github_token_expires_at": 0.0},
        )

        with (
            patch("automation.agent.middlewares.git_platform.github.get_github_integration") as get_integration,
            cli_process(stdout=b"ok\n"),
        ):
            _fresh_token(get_integration, "tok_new")

            _output_applying_state(await _run_gh("issue view 1", runtime), runtime)

        assert get_integration.return_value.get_access_token.call_count == 1
        assert runtime.state["github_token"] == "tok_new"  # noqa: S105

    async def test_token_not_in_tool_output(self):
        runtime = github_runtime(thread_id="test-thread-3", tool_call_id="test_call_3")

        with (
            patch("automation.agent.middlewares.git_platform.github.get_github_integration") as get_integration,
            cli_process(stdout=b"ok\n"),
        ):
            _fresh_token(get_integration, "tok_1")

            out = _output_applying_state(await _run_gh("issue view 1", runtime), runtime)

        assert out == "ok"
        assert "tok_1" not in out


@patch("automation.agent.middlewares.git_platform.github.cache.lock", new=MagicMock())
class TestGitHubToolOutputToFile:
    async def test_github_output_to_file_writes_verbatim_and_wraps_in_command(self):
        runtime = github_runtime(thread_id="t-gh-of")
        backend = recording_backend()

        with (
            patch("automation.agent.middlewares.git_platform.github.get_github_integration") as get_integration,
            cli_process(stdout=b'{"number": 7}\n') as create_proc,
        ):
            _fresh_token(get_integration, "tok")

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
        runtime = github_runtime(thread_id="t-gh-empty", tool_call_id="c3", state=dict(CACHED_TOKEN))
        backend = recording_backend()

        with cli_process(stdout=b""):
            result = await _run_gh("issue list --state open", runtime, to_file=True, backend=backend)

        # Token was already cached → plain string (no Command)
        assert isinstance(result, str)
        assert "empty result" in result
        assert "no file was written" in result
        backend.awrite.assert_not_called()

    async def test_github_run_view_log_to_file(self):
        """gh run view --log to_file: clean_job_logs runs; file written, confirmation returned."""
        runtime = github_runtime(thread_id="t-gh-log", tool_call_id="c-log", state=dict(CACHED_TOKEN))
        backend = recording_backend()

        with (
            cli_process(stdout=b"2024-01-01T00:00:00.000Z job1\tsome log line\n"),
            patch(
                "automation.agent.middlewares.git_platform.github.clean_job_logs", return_value="some log line"
            ) as mock_clean,
        ):
            result = await _run_gh("run view 123 --job 456 --log", runtime, to_file=True, backend=backend)

        # Token was already cached → plain string (no Command)
        assert isinstance(result, str)
        assert result.startswith("Wrote ")
        assert backend.awrite.call_args.args[0] == "/workspace/large_tool_results/c-log"
        mock_clean.assert_called_once()

    async def test_github_output_to_file_cached_token_plain_string(self):
        """gh output_to_file with a cached valid token returns a plain str (no Command)."""
        runtime = github_runtime(thread_id="t-gh-cached", tool_call_id="c-cached", state=dict(CACHED_TOKEN))
        backend = recording_backend()

        with cli_process(stdout=b'{"number": 7}\n'):
            result = await _run_gh("pr view 7 --json number", runtime, to_file=True, backend=backend)

        assert isinstance(result, str)
        assert result.startswith("Wrote ")
        assert backend.awrite.call_args.args[0] == "/workspace/large_tool_results/c-cached"


class TestGitHubPolicy:
    @pytest.mark.parametrize(("action", "expected"), [("create", True), ("delete", False)])
    def test_release_actions_follow_policy(self, action, expected):
        allowed, _ = _is_allowed_cli_command("release", action, GITHUB_CLI_ALLOW_COMMANDS)
        assert allowed is expected

    def test_api_is_not_allowed_because_it_escapes_repo_scoping(self):
        allowed, _ = _is_allowed_cli_command("api", "repos/other/repo", GITHUB_CLI_ALLOW_COMMANDS)
        assert allowed is False

    async def test_release_create_reaches_the_cli(self):
        runtime = github_runtime(thread_id="t-gh-release", tool_call_id="c-release", state=dict(CACHED_TOKEN))

        with cli_process(stdout=b"ok\n") as create_proc:
            await _run_gh('release create v1.0.0 --title "v1.0.0" --notes "notes"', runtime)

        assert create_proc.call_args.args[:3] == ("gh", "release", "create")


class TestPublishedTextIsRepoRelative:
    async def test_a_github_comment_body_is_repo_relative(self):
        runtime = github_runtime(thread_id="t", state=dict(CACHED_TOKEN))

        with cli_process(stdout=b"ok\n") as create_proc:
            await _run_gh('issue comment 1 -b "See /workspace/repo/daiv/x.py"', runtime)

        argv = list(create_proc.call_args.args)
        assert argv[argv.index("-b") + 1] == "See daiv/x.py"


async def test_github_failure_keeps_the_command_output():
    """``gh pr checks`` exits 1 when a check failed and prints the checks table on stdout, not stderr."""
    runtime = github_runtime(thread_id="t-gh-fail", tool_call_id="c-fail", state=dict(CACHED_TOKEN))
    table = b"tests\tfail\t3m2s\thttps://github.com/owner/repo/actions/runs/1/job/2\nlint\tpass\t40s\t\n"

    with cli_process(stdout=table, returncode=1):
        result = await _run_gh("pr checks 1650", runtime)

    assert result.startswith("error: GitHub command failed (exit code 1).")
    assert "tests\tfail\t3m2s\thttps://github.com/owner/repo/actions/runs/1/job/2" in result
