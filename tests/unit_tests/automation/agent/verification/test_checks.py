from __future__ import annotations

import httpx
import pytest
from git import GitCommandError

from automation.agent.verification.checks import (
    VERIFICATION_BUDGET_SECONDS,
    CheckResult,
    CheckRunner,
    ChecksUnavailableError,
    diff_fingerprint,
)
from automation.agent.workspace.git_manager import StashedTree, StashRestoreError
from core.sandbox.schemas import RunCommandResult, RunCommandsResponse


class FakeBash:
    """``exit_codes`` are handed out one per command, in order, 124 as a sandbox timeout; a command past their end exits
    0."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], int | None]] = []
        self.exit_codes: list[int] = []
        self.results: list[RunCommandResult] | None = None
        self.error: Exception | None = None

    async def run_commands(self, commands, *, fail_fast, timeout=None):
        self.calls.append((list(commands), timeout))
        if self.error:
            raise self.error
        if self.results is not None:
            return RunCommandsResponse(results=self.results)
        codes = [self.exit_codes.pop(0) if self.exit_codes else 0 for _ in commands]
        return RunCommandsResponse(
            results=[
                RunCommandResult(command=c, output="", exit_code=code, timed_out=code == 124)
                for c, code in zip(commands, codes, strict=True)
            ]
        )


SAVED = StashedTree(stash="5" * 40, head="h" * 40, branch="main")


class FakeGit:
    def __init__(self) -> None:
        self.stashed = True
        self.diffs = ["diff"]
        self.events: list[str] = []
        self.stash_error: Exception | None = None
        self.restore_error: Exception | None = None
        self.drop_error: Exception | None = None
        self.diff_error: Exception | None = None

    async def stash_all(self, message: str) -> StashedTree | None:
        self.events.append("stash_all")
        if self.stash_error is not None:
            raise self.stash_error
        return SAVED if self.stashed else None

    async def restore_stash(self, saved: StashedTree) -> None:
        assert saved is SAVED
        self.events.append("restore_stash")
        if self.restore_error is not None:
            raise self.restore_error

    async def drop_stash(self, saved: StashedTree) -> None:
        assert saved is SAVED
        self.events.append("drop_stash")
        if self.drop_error is not None:
            raise self.drop_error

    async def get_diff(self) -> str:
        if self.diff_error is not None:
            raise self.diff_error
        return self.diffs.pop(0)


@pytest.fixture
def fake_bash() -> FakeBash:
    return FakeBash()


@pytest.fixture
def fake_git() -> FakeGit:
    return FakeGit()


async def test_run_checks_sends_each_command_alone_from_the_repo_dir(fake_bash):
    await CheckRunner(fake_bash, FakeGit()).run(["make lint", "make test"])
    assert fake_bash.calls == [
        ([CheckRunner.command_line("make lint")], 630),
        ([CheckRunner.command_line("make test")], 630),
    ]


def test_command_line_runs_the_check_under_timeout_from_the_repo_dir():
    line = CheckRunner.command_line("pytest -k 'not slow'")

    assert line == (
        "cd /workspace/repo && if command -v timeout >/dev/null 2>&1; "
        "then timeout -k 10 600 \"${BASH:-sh}\" -c 'pytest -k '\"'\"'not slow'\"'\"''; "
        "else \"${BASH:-sh}\" -c 'pytest -k '\"'\"'not slow'\"'\"''; fi"
    )


async def test_run_checks_runs_the_next_command_after_one_times_out(fake_bash):
    fake_bash.exit_codes = [124, 0]

    slow, fast = await CheckRunner(fake_bash, FakeGit()).run(["make slow", "make fast"])

    assert slow.timed_out and not slow.passed
    assert fast.passed
    assert len(fake_bash.calls) == 2


async def test_run_checks_keeps_declared_command_and_output_tail(fake_bash):
    fake_bash.results = [RunCommandResult(command="cd /workspace/repo && make test", output="x" * 5_000, exit_code=1)]
    [result] = await CheckRunner(fake_bash, FakeGit()).run(["make test"])
    assert result.command == "make test" and len(result.output_tail) == 4_000 and not result.passed


@pytest.mark.parametrize(
    ("result", "timed_out"),
    [
        (RunCommandResult(command="make test", output="", exit_code=124, timed_out=True), True),
        (RunCommandResult(command="make test", output="Command timed out after 630s", exit_code=124), True),
        (RunCommandResult(command="timeout 60 make test", output="", exit_code=124), False),
    ],
    ids=["sandbox-flag", "sandbox-notice", "the-commands-own-timeout"],
)
async def test_run_checks_tells_the_sandbox_timeout_from_the_commands_own(fake_bash, result, timed_out):
    fake_bash.results = [result]

    [check] = await CheckRunner(fake_bash, FakeGit()).run(["make test"])

    assert check.timed_out is timed_out


@pytest.mark.parametrize("exit_code", [124, 137])
async def test_run_checks_counts_a_check_timeout_stopped_at_the_limit_as_timed_out(fake_bash, exit_code):
    clock = FakeClock()

    async def slow(commands, *, fail_fast, timeout=None):
        clock.now += 600
        return RunCommandsResponse(results=[RunCommandResult(command=commands[0], output="", exit_code=exit_code)])

    fake_bash.run_commands = slow

    [check] = await CheckRunner(fake_bash, FakeGit(), clock=clock).run(["make test"])

    assert check.timed_out is True


async def test_run_checks_raises_unavailable_on_transport_error(fake_bash):
    fake_bash.error = httpx.ConnectError("All connection attempts failed: http://sandbox:8000/session/s-1/")
    with pytest.raises(ChecksUnavailableError) as raised:
        await CheckRunner(fake_bash, FakeGit()).run(["make test"])

    assert str(raised.value) == "the sandbox could not be reached to run the checks"


def _failing(*commands: str) -> list[CheckResult]:
    return [CheckResult(command, 1, "") for command in commands]


async def test_failing_before_change_returns_the_commands_failing_on_starting_code(fake_bash, fake_git):
    fake_bash.exit_codes = [0, 1]

    found = await CheckRunner(fake_bash, fake_git).failing_before_change(
        _failing("make lint", "make test"), merging=False
    )

    assert found == frozenset({"make test"})
    assert fake_git.events == ["stash_all", "restore_stash"]


async def test_failing_before_change_restores_tree_even_when_command_raises(fake_bash, fake_git):
    fake_bash.error = httpx.ConnectError("down")
    with pytest.raises(ChecksUnavailableError):
        await CheckRunner(fake_bash, fake_git).failing_before_change(_failing("make test"), merging=False)
    assert fake_git.events == ["stash_all", "restore_stash"]


async def test_failing_before_change_is_unavailable_when_the_changes_cannot_be_set_aside(fake_bash, fake_git):
    fake_git.stash_error = GitCommandError(["git", "stash", "push"], 1, "error: could not write index")

    with pytest.raises(ChecksUnavailableError):
        await CheckRunner(fake_bash, fake_git).failing_before_change(_failing("make test"), merging=False)

    assert fake_git.events == ["stash_all"]
    assert fake_bash.calls == []


async def test_failing_before_change_propagates_a_failed_restore(fake_bash, fake_git):
    fake_git.restore_error = StashRestoreError()

    with pytest.raises(StashRestoreError):
        await CheckRunner(fake_bash, fake_git).failing_before_change(_failing("make test"), merging=False)

    assert fake_git.events == ["stash_all", "restore_stash"]


async def test_failing_before_change_skips_pending_merge(fake_bash, fake_git):
    found = await CheckRunner(fake_bash, fake_git).failing_before_change(_failing("make test"), merging=True)

    assert found == frozenset()
    assert fake_git.events == []
    assert fake_bash.calls == []


async def test_failing_before_change_returns_every_failing_command_when_nothing_was_stashed(fake_bash, fake_git):
    fake_git.stashed = False

    found = await CheckRunner(fake_bash, fake_git).failing_before_change(
        _failing("make lint", "make test"), merging=False
    )

    assert found == frozenset({"make lint", "make test"})
    assert fake_git.events == ["stash_all"]
    assert fake_bash.calls == []


async def test_run_checks_raises_unavailable_when_the_sandbox_returns_no_result(fake_bash):
    fake_bash.results = []
    with pytest.raises(ChecksUnavailableError):
        await CheckRunner(fake_bash, FakeGit()).run(["make test"])


AGENT_DIFF = "diff --git a/a.py b/a.py\n+new\n"
LOCK_DIFF = "diff --git a/uv.lock b/uv.lock\n+v2\n"


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def test_no_check_starts_once_the_run_spent_its_budget(fake_bash):
    clock = FakeClock()
    runner = CheckRunner(fake_bash, FakeGit(), clock=clock)
    await runner.run(["make lint"])
    clock.now = VERIFICATION_BUDGET_SECONDS

    with pytest.raises(ChecksUnavailableError) as raised:
        await runner.run(["make test"])

    assert str(raised.value) == "the checks used up their 30 minutes for this run"
    assert fake_bash.calls == [([CheckRunner.command_line("make lint")], 630)]


async def test_checks_started_within_the_budget_run(fake_bash):
    clock = FakeClock()
    runner = CheckRunner(fake_bash, FakeGit(), clock=clock)
    await runner.run(["make lint"])
    clock.now = VERIFICATION_BUDGET_SECONDS - 1

    await runner.run(["make test"])

    assert len(fake_bash.calls) == 2


async def test_run_on_tree_fingerprints_the_tree_the_checks_left(fake_bash, fake_git):
    fake_git.diffs = ["after the checks"]

    run = await CheckRunner(fake_bash, fake_git).run_on_tree(["ruff format ."])

    assert run.fingerprint == diff_fingerprint("after the checks")
    assert fake_git.events == []


async def test_run_on_tree_is_unavailable_when_the_tree_cannot_be_read_after(fake_bash, fake_git):
    fake_git.diff_error = GitCommandError("diff", 128)

    with pytest.raises(ChecksUnavailableError, match="could not be read"):
        await CheckRunner(fake_bash, fake_git).run_on_tree(["make test"])
