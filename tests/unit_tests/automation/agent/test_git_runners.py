import subprocess  # noqa: S404
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from git import Repo

from automation.agent.git_runners import (
    GitResult,
    LocalGitRunner,
    SandboxGitProtocolError,
    SandboxGitRunner,
    _shell_quote,
)
from codebase.clients.base import GitAuthEnv
from core.sandbox.schemas import RunCommandResult, RunCommandsResponse
from tests.unit_tests.conftest import FakeSandboxClient, sandbox_backend_on


def _init_repo(tmp_path: Path) -> Repo:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    return Repo.init(repo_dir)


def _sandbox_runner(responses: dict[str, tuple[int, str]] | None = None) -> tuple[SandboxGitRunner, FakeSandboxClient]:
    client = FakeSandboxClient.opened(responses)
    return SandboxGitRunner(sandbox_backend_on(client, client.add_running_session("sid"))), client


def _replying(*results: RunCommandResult) -> MagicMock:
    client = MagicMock()
    client.run_commands = AsyncMock(return_value=RunCommandsResponse(results=list(results)))
    return client


# ---------------------------------------------------------------------------
# _shell_quote (sandbox command construction)
# ---------------------------------------------------------------------------


def test_shell_quote_passes_safe_args_through() -> None:
    assert _shell_quote("status") == "status"
    assert _shell_quote("--porcelain") == "--porcelain"
    assert _shell_quote("origin/main..HEAD") == "origin/main..HEAD"


def test_shell_quote_single_quotes_args_with_spaces() -> None:
    assert _shell_quote("fix: thing") == "'fix: thing'"


def test_shell_quote_escapes_embedded_single_quote() -> None:
    # The POSIX idiom: close the quote, emit an escaped quote, reopen — `'\''`.
    assert _shell_quote("don't") == "'don'\\''t'"


def test_shell_quote_preserves_newlines_inside_quotes() -> None:
    quoted = _shell_quote("line1\nline2")
    assert quoted.startswith("'") and quoted.endswith("'")
    assert "\n" in quoted


# ---------------------------------------------------------------------------
# SandboxGitRunner
# ---------------------------------------------------------------------------


async def test_sandbox_runner_runs_git_in_the_workspace_repo() -> None:
    runner, client = _sandbox_runner({"status": (0, " M a.py\n")})

    result = await runner.run(("status", "--porcelain"))

    assert result == GitResult(exit_code=0, output=" M a.py\n")
    assert client.commands == ["git -C /workspace/repo status --porcelain"]


async def test_sandbox_runner_quotes_each_argument() -> None:
    runner, client = _sandbox_runner()

    await runner.run(("commit", "-m", "fix: don't break"))

    assert client.commands == ["git -C /workspace/repo commit -m 'fix: don'\\''t break'"]


async def test_sandbox_runner_reports_a_failure_without_raising() -> None:
    runner, _ = _sandbox_runner({"add -A": (1, "fatal: boom")})

    assert await runner.run(("add", "-A")) == GitResult(exit_code=1, output="fatal: boom")


async def test_sandbox_runner_batch_is_one_round_trip_that_runs_past_a_failure() -> None:
    """``diff --no-index`` exits 1 when it finds differences, so a batch must not stop there."""
    runner, client = _sandbox_runner({"diff --no-index": (1, "diff --git a/x b/x\n")})

    results = await runner.run_batch([("diff", "--no-index", "/dev/null", "x"), ("ls-files", "--others")])

    assert [r.exit_code for r in results] == [1, 0]
    assert len(client.calls_to("run_commands")) == 1


async def test_sandbox_runner_raises_on_a_missing_result() -> None:
    runner = SandboxGitRunner(sandbox_backend_on(_replying(), "sid"))

    with pytest.raises(SandboxGitProtocolError, match="no result"):
        await runner.run(("status",))


async def test_sandbox_runner_batch_raises_on_a_short_result_list() -> None:
    runner = SandboxGitRunner(
        sandbox_backend_on(_replying(RunCommandResult(command="git", exit_code=0, output="")), "sid")
    )

    with pytest.raises(SandboxGitProtocolError, match="1 results for 2 git commands"):
        await runner.run_batch([("status",), ("ls-files",)])


# ---------------------------------------------------------------------------
# LocalGitRunner
# ---------------------------------------------------------------------------


async def test_local_runner_batch_returns_results_in_input_order(tmp_path: Path) -> None:
    runner = LocalGitRunner(_init_repo(tmp_path))

    results = await runner.run_batch([("rev-parse", "--is-inside-work-tree"), ("rev-parse", "--verify", "missing")])

    assert results[0] == GitResult(exit_code=0, output="true\n")
    assert results[1].exit_code != 0


async def test_local_auth_env_reaches_git_but_never_persists_to_config(tmp_path: Path) -> None:
    """``LocalGitRunner(auth_env=...)`` must overlay the credential on every local git invocation via
    git's own ``GIT_CONFIG_*`` env mechanism — this is how the ephemeral credential reaches git now
    that the clone's ``.git/config`` carries none — AND that credential must stay ephemeral: it must
    never be written into ``.git/config``. Reading it back in-process proves the overlay reaches git;
    reading the on-disk config proves it did not persist (guarding the core security contract against
    a future switch to a persisted ``git config`` write, which would re-seed the token into the
    sandbox)."""
    repo = _init_repo(tmp_path)
    auth_env = GitAuthEnv.for_token("https://gitlab.com/group/repo.git", "sekret-token")
    runner = LocalGitRunner(repo, auth_env=auth_env)

    result = await runner.run(("config", "--get", "http.https://gitlab.com/.extraheader"))
    assert "Authorization: Basic" in result.output

    config_on_disk = (Path(repo.working_dir) / ".git" / "config").read_text()
    assert "sekret-token" not in config_on_disk
    assert "extraheader" not in config_on_disk
    assert "GIT_CONFIG" not in config_on_disk


async def test_local_git_never_prompts_for_credentials(tmp_path: Path, monkeypatch) -> None:
    """Every local git invocation must run with prompting fully disabled. With no credential in
    ``.git/config``, an auth-required remote otherwise makes git prompt — on the tty, or via an
    inherited ``SSH_ASKPASS`` GUI helper — hanging an unattended publish forever. Both
    ``GIT_TERMINAL_PROMPT=0`` (no tty prompt) and ``GIT_ASKPASS=''`` (no askpass fallback) are
    required; together they make a rejected credential fail fast with ``could not read Username``,
    a marker ``is_git_auth_error_text`` classifies."""
    repo = _init_repo(tmp_path)
    captured: dict[str, dict[str, str] | None] = {}

    def fake_run(cmd, **kwargs):
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("automation.agent.git_runners.subprocess.run", fake_run)

    await LocalGitRunner(repo).run(("status",))
    assert captured["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert captured["env"]["GIT_ASKPASS"] == ""

    auth_env = GitAuthEnv.for_token("https://gitlab.com/group/repo.git", "tok")
    await LocalGitRunner(repo, auth_env=auth_env).run(("status",))
    assert "Authorization: Basic" in captured["env"]["GIT_CONFIG_VALUE_0"]
    assert captured["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert captured["env"]["GIT_ASKPASS"] == ""


async def test_local_env_overlay_keeps_process_environment(tmp_path: Path) -> None:
    """The overlay must extend the inherited environment, not replace it — wiping PATH/HOME
    would break git itself (and drop e.g. commit identity from the environment)."""
    result = await LocalGitRunner(_init_repo(tmp_path)).run(("status", "--porcelain"))

    assert result.exit_code == 0
