import shlex
import subprocess  # noqa: S404
import traceback
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from git import GitCommandError, Repo

from automation.agent.git_runners import (
    GitResult,
    LocalGitRunner,
    SandboxGitProtocolError,
    SandboxGitRunner,
    _shell_quote,
)
from codebase.clients.base import GitAuthEnv
from core.sandbox.schemas import RunCommandResult, RunCommandsResponse
from tests.unit_tests.conftest import FakeSandboxClient, sandbox_backend_on, unacquired_backend


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


def _capture_subprocess(monkeypatch) -> dict[str, dict[str, str]]:
    captured: dict[str, dict[str, str]] = {}

    def fake_run(cmd, **kwargs):
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("automation.agent.git_runners.subprocess.run", fake_run)
    return captured


def test_shell_quote_passes_safe_args_through() -> None:
    assert _shell_quote("status") == "status"
    assert _shell_quote("--porcelain") == "--porcelain"
    assert _shell_quote("origin/main..HEAD") == "origin/main..HEAD"


@pytest.mark.parametrize(
    "arg",
    [
        "fix: thing",
        "don't",
        "line1\nline2",
        "$(id)",
        "`id`",
        "a;b",
        "a|b",
        "a&&b",
        "a*",
        'say "hi"',
        "back\\slash",
        "!bang",
        "#c",
        "",
    ],
)
def test_shell_quote_makes_a_shell_read_the_argument_verbatim(arg: str) -> None:
    assert shlex.split(_shell_quote(arg)) == [arg]


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


async def test_sandbox_runner_lets_the_unbound_session_guard_propagate_unwrapped() -> None:
    runner = SandboxGitRunner(unacquired_backend())

    with pytest.raises(RuntimeError, match="not bound") as exc_info:
        await runner.run(("status",))

    assert type(exc_info.value) is RuntimeError


async def test_sandbox_runner_batch_raises_on_a_short_result_list() -> None:
    runner = SandboxGitRunner(
        sandbox_backend_on(_replying(RunCommandResult(command="git", exit_code=0, output="")), "sid")
    )

    with pytest.raises(SandboxGitProtocolError, match="1 results for 2 git commands"):
        await runner.run_batch([("status",), ("ls-files",)])


async def test_local_runner_batch_returns_results_in_input_order(tmp_path: Path) -> None:
    runner = LocalGitRunner(_init_repo(tmp_path))

    results = await runner.run_batch([("rev-parse", "--is-inside-work-tree"), ("rev-parse", "--verify", "missing")])

    assert results[0] == GitResult(exit_code=0, output="true\n")
    assert results[1].exit_code != 0


async def test_local_runner_folds_stderr_into_the_output(tmp_path: Path) -> None:
    result = await LocalGitRunner(_init_repo(tmp_path)).run(("rev-parse", "--verify", "missing"))

    assert result.exit_code != 0
    assert "fatal" in result.output


async def test_local_runner_reads_non_utf8_output_lossily(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    latin1_file = Path(repo.working_dir) / "latin1.txt"
    latin1_file.write_bytes(b"caf\xe9\n")

    result = await LocalGitRunner(repo).run(("diff", "--no-index", "/dev/null", str(latin1_file)))

    assert result.exit_code == 1
    assert "caf" in result.output


async def test_local_runner_spawn_failure_raises_without_the_credential_in_any_frame(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("automation.agent.git_runners.subprocess.run", MagicMock(side_effect=OSError("boom")))
    auth_env = GitAuthEnv.for_token("https://gitlab.com/group/repo.git", "sekret-token")
    secret = auth_env.header.get_secret_value()

    with pytest.raises(GitCommandError) as exc_info:
        await LocalGitRunner(_init_repo(tmp_path), auth_env=auth_env).run(("status",))

    assert exc_info.value.__context__ is None
    runner_frames = [
        frame
        for frame, _ in traceback.walk_tb(exc_info.value.__traceback__)
        if frame.f_globals["__name__"] == "automation.agent.git_runners"
    ]
    assert runner_frames
    assert all(secret not in repr(frame.f_locals) for frame in runner_frames)
    assert secret not in str(exc_info.value)


async def test_local_auth_env_reaches_git_but_never_persists_to_config(tmp_path: Path) -> None:
    """The credential reaches git through ``GIT_CONFIG_*`` env and is never written to ``.git/config``."""
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
    """Prompting is disabled with or without a credential, so a rejected one fails fast as an auth error."""
    repo = _init_repo(tmp_path)
    captured = _capture_subprocess(monkeypatch)

    await LocalGitRunner(repo).run(("status",))
    assert captured["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert captured["env"]["GIT_ASKPASS"] == ""

    auth_env = GitAuthEnv.for_token("https://gitlab.com/group/repo.git", "tok")
    await LocalGitRunner(repo, auth_env=auth_env).run(("status",))
    assert "Authorization: Basic" in captured["env"]["GIT_CONFIG_VALUE_0"]
    assert captured["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert captured["env"]["GIT_ASKPASS"] == ""


async def test_local_env_overlay_keeps_process_environment(tmp_path: Path, monkeypatch) -> None:
    """The overlay extends the inherited environment: replacing it would drop PATH/HOME and break git."""
    monkeypatch.setenv("DAIV_TEST_SENTINEL", "kept")
    captured = _capture_subprocess(monkeypatch)

    await LocalGitRunner(_init_repo(tmp_path)).run(("status",))

    assert captured["env"]["DAIV_TEST_SENTINEL"] == "kept"
