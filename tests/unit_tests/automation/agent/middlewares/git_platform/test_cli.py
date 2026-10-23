from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest

from automation.agent.middlewares.git_platform.cli import (
    CliInvocation,
    PolicyRefusal,
    _repo_relative_flag_values,
    check_subcommand,
    run_cli,
)
from automation.agent.middlewares.git_platform.github import GITHUB_CLI
from automation.agent.middlewares.git_platform.gitlab import GITLAB_CLI
from tests.unit_tests.automation.agent.middlewares.git_platform.conftest import cli_process, gitlab_runtime
from tests.unit_tests.conftest import recording_backend

PLATFORMS = [pytest.param(GITLAB_CLI, "project-issue", id="gitlab"), pytest.param(GITHUB_CLI, "issue", id="github")]


async def _run(cli, invocation, *, to_file=False):
    return await run_cli(cli, invocation, gitlab_runtime(), to_file=to_file, backend=recording_backend())


@pytest.mark.parametrize(("cli", "resource"), PLATFORMS)
def test_an_empty_action_is_an_incomplete_subcommand(cli, resource):
    result = check_subcommand(cli, f'{resource} ""')

    assert result == f"error: Incomplete subcommand. Expected format: '{cli.usage}'. Got: '{resource} \"\"'"


@pytest.mark.parametrize(("cli", "resource"), PLATFORMS)
async def test_a_timeout_kills_the_process_and_names_the_limit(cli, resource):
    proc = Mock(kill=Mock(), wait=AsyncMock(), communicate=AsyncMock(side_effect=TimeoutError()))

    with (
        patch("automation.agent.middlewares.git_platform.cli.DEFAULT_CLI_TIMEOUT", 7),
        patch("automation.agent.middlewares.git_platform.cli.asyncio.create_subprocess_exec", return_value=proc),
    ):
        result = await _run(cli, CliInvocation(args=[resource, "list"], env={}))

    assert result.startswith(f"error: {cli.display_name} command timed out after 7 seconds.")
    proc.kill.assert_called_once()


async def test_postprocess_shapes_the_output_before_it_is_written():
    backend = recording_backend()
    invocation = CliInvocation(args=["project-job", "trace"], env={}, postprocess=str.upper)

    with cli_process(stdout=b"raw log\n"):
        await run_cli(GITLAB_CLI, invocation, gitlab_runtime(), to_file=True, backend=backend)

    assert backend.awrite.call_args.args[1] == "RAW LOG"


class TestPublishedTextIsRepoRelative:
    def test_flag_values_are_rewritten_in_every_form(self):
        args = [
            "pr",
            "create",
            "--title",
            "Fix /workspace/repo/a.py",
            "--description=See /workspace/repo/b.py",
            "-b",
            "Touches /workspace/repo/c.py",
            "--notes",
            "Released /workspace/repo/f.py",
            "--position",
            '{"new_path": "/workspace/repo/d.py"}',
            "--body-file",
            "/workspace/repo/e.md",
        ]

        assert _repo_relative_flag_values(args) == [
            "pr",
            "create",
            "--title",
            "Fix a.py",
            "--description=See b.py",
            "-b",
            "Touches c.py",
            "--notes",
            "Released f.py",
            "--position",
            '{"new_path": "/workspace/repo/d.py"}',
            "--body-file",
            "/workspace/repo/e.md",
        ]

    @pytest.mark.parametrize("flag", ["-n", "--note", "--message", "--content"])
    def test_other_text_flags_are_rewritten(self, flag):
        assert _repo_relative_flag_values(["x", flag, "See /workspace/repo/a.py"]) == ["x", flag, "See a.py"]


@pytest.mark.parametrize(("cli", "resource"), PLATFORMS)
def test_a_policy_refusal_is_told_apart_from_a_malformed_subcommand(cli, resource):
    assert isinstance(check_subcommand(cli, "not-a-resource list"), PolicyRefusal)
    assert not isinstance(check_subcommand(cli, resource), PolicyRefusal)


def _outcomes(*, on_success=None) -> Mock:
    return Mock(
        on_timeout=AsyncMock(return_value="timed out"),
        on_launch_error=AsyncMock(return_value="launch failed"),
        on_failure=AsyncMock(return_value="failed"),
        on_success=AsyncMock(return_value=on_success),
    )


async def test_outcomes_report_a_failure_instead_of_its_stderr():
    outcomes = _outcomes()

    with cli_process(stdout=b"out", stderr=b"secret\n", returncode=1):
        result = await _run(GITLAB_CLI, CliInvocation(args=["project-issue", "list"], env={}, outcomes=outcomes))

    assert result == "failed"
    outcomes.on_failure.assert_awaited_once_with(1, "secret")


@pytest.mark.parametrize(("withheld", "expected"), [(None, "out"), ("withheld", "withheld")])
async def test_outcomes_may_withhold_a_successful_output(withheld, expected):
    outcomes = _outcomes(on_success=withheld)

    with cli_process(stdout=b"out\n"):
        result = await _run(GITLAB_CLI, CliInvocation(args=["project-issue", "list"], env={}, outcomes=outcomes))

    assert result == expected


async def test_outcomes_report_a_launch_error_without_its_details():
    outcomes = _outcomes()

    with patch(
        "automation.agent.middlewares.git_platform.cli.asyncio.create_subprocess_exec", side_effect=OSError("x")
    ):
        result = await _run(GITLAB_CLI, CliInvocation(args=["project-issue", "list"], env={}, outcomes=outcomes))

    assert result == "launch failed"
