from __future__ import annotations

import httpx
import pytest
from git import GitCommandError

from automation.agent.verification.checks import (
    CheckResult,
    ChecksUnavailableError,
    diff_fingerprint,
    failing_before_change,
    redacted_values,
    run_checks,
    tree_fingerprint,
)
from automation.agent.workspace.git_manager import PendingMerge
from core.sandbox.schemas import RunCommandResult, RunCommandsResponse


class FakeBash:
    """``exit_codes`` are handed out one per command, in order; a command past their end exits 0."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], int | None, float | None]] = []
        self.exit_codes: list[int] = []
        self.results: list[RunCommandResult] | None = None
        self.error: Exception | None = None

    async def run_commands(self, commands, *, fail_fast, timeout=None, http_timeout=None):
        self.calls.append((list(commands), timeout, http_timeout))
        if self.error:
            raise self.error
        if self.results is not None:
            return RunCommandsResponse(results=self.results)
        return RunCommandsResponse(
            results=[
                RunCommandResult(command=c, output="", exit_code=self.exit_codes.pop(0) if self.exit_codes else 0)
                for c in commands
            ]
        )


class FakeGit:
    def __init__(self) -> None:
        self.stashed = True
        self.merge: PendingMerge | None = None
        self.diffs = ["diff"]
        self.events: list[str] = []
        self.stash_error: Exception | None = None
        self.merge_error: Exception | None = None
        self.restore_error: Exception | None = None

    async def pending_merge(self):
        if self.merge_error is not None:
            raise self.merge_error
        return self.merge

    async def stash_all(self, message: str) -> bool:
        self.events.append("stash_all")
        if self.stash_error is not None:
            raise self.stash_error
        return self.stashed

    async def restore_stash(self) -> None:
        self.events.append("restore_stash")
        if self.restore_error is not None:
            raise self.restore_error

    async def get_diff(self) -> str:
        return self.diffs.pop(0)


@pytest.fixture
def fake_bash() -> FakeBash:
    return FakeBash()


@pytest.fixture
def fake_git() -> FakeGit:
    return FakeGit()


async def test_run_checks_sends_each_command_alone_from_the_repo_dir(fake_bash):
    await run_checks(fake_bash, ["make lint", "make test"])
    assert fake_bash.calls == [
        (["cd /workspace/repo && make lint"], 600, 660),
        (["cd /workspace/repo && make test"], 600, 660),
    ]


async def test_run_checks_runs_the_next_command_after_one_times_out(fake_bash):
    fake_bash.exit_codes = [124, 0]

    slow, fast = await run_checks(fake_bash, ["make slow", "make fast"])

    assert slow.timed_out and not slow.passed
    assert fast.passed
    assert len(fake_bash.calls) == 2


async def test_run_checks_keeps_declared_command_and_output_tail(fake_bash):
    fake_bash.results = [RunCommandResult(command="cd /workspace/repo && make test", output="x" * 5_000, exit_code=1)]
    [result] = await run_checks(fake_bash, ["make test"])
    assert result.command == "make test" and len(result.output_tail) == 4_000 and not result.passed


async def test_run_checks_masks_the_values_before_taking_the_tail(fake_bash):
    output = "token=hunter2-secret-0042 " + "y" * 3_990
    fake_bash.results = [RunCommandResult(command="cd /workspace/repo && make test", output=output, exit_code=1)]

    [result] = await run_checks(fake_bash, ["make test"], redact=("hunter2-secret-0042",))

    assert result.output_tail.startswith("en=******")
    assert "0042" not in result.output_tail


def test_redacted_values_keeps_values_of_eight_characters_or_more():
    assert redacted_values({"SHORT": "abc1234", "TOKEN": "abcd1234", "EMPTY": ""}) == ("abcd1234",)


def test_check_result_timed_out_on_exit_124():
    assert CheckResult("make test", 124, "").timed_out


async def test_run_checks_raises_unavailable_on_transport_error(fake_bash):
    fake_bash.error = httpx.ConnectError("down")
    with pytest.raises(ChecksUnavailableError):
        await run_checks(fake_bash, ["make test"])


async def test_failing_before_change_returns_commands_that_fail_on_starting_code(fake_bash, fake_git):
    fake_bash.exit_codes = [1, 0]
    assert await failing_before_change(fake_bash, fake_git, ["make lint", "make test"]) == frozenset({"make lint"})
    assert fake_git.events == ["stash_all", "restore_stash"]


async def test_failing_before_change_restores_tree_even_when_command_raises(fake_bash, fake_git):
    fake_bash.error = httpx.ConnectError("down")
    with pytest.raises(ChecksUnavailableError):
        await failing_before_change(fake_bash, fake_git, ["make test"])
    assert fake_git.events == ["stash_all", "restore_stash"]


async def test_failing_before_change_is_unavailable_when_the_changes_cannot_be_set_aside(fake_bash, fake_git):
    fake_git.stash_error = GitCommandError(["git", "stash", "push"], 1, "error: could not write index")

    with pytest.raises(ChecksUnavailableError):
        await failing_before_change(fake_bash, fake_git, ["make test"])

    assert fake_git.events == ["stash_all"]
    assert fake_bash.calls == []


async def test_failing_before_change_is_unavailable_when_the_tree_cannot_be_read(fake_bash, fake_git):
    fake_git.merge_error = httpx.ReadTimeout("timed out")

    with pytest.raises(ChecksUnavailableError):
        await failing_before_change(fake_bash, fake_git, ["make test"])

    assert fake_git.events == []


async def test_failing_before_change_propagates_a_failed_restore(fake_bash, fake_git):
    fake_git.restore_error = GitCommandError(["git", "stash", "pop"], 1, "CONFLICT")

    with pytest.raises(GitCommandError):
        await failing_before_change(fake_bash, fake_git, ["make test"])

    assert fake_git.events == ["stash_all", "restore_stash"]


async def test_failing_before_change_skips_pending_merge(fake_bash, fake_git):
    fake_git.merge = PendingMerge(head="a" * 40, branch=None, unmerged_paths=(), conflicted_paths=())
    assert await failing_before_change(fake_bash, fake_git, ["make test"]) == frozenset()
    assert fake_git.events == []
    assert fake_bash.calls == []


async def test_failing_before_change_never_pops_when_nothing_was_stashed(fake_bash, fake_git):
    fake_git.stashed = False
    assert await failing_before_change(fake_bash, fake_git, ["make test"]) == frozenset({"make test"})
    assert fake_git.events == ["stash_all"]
    assert fake_bash.calls == []


async def test_tree_fingerprint_changes_with_diff(fake_git):
    fake_git.diffs = ["one", "two"]
    assert await tree_fingerprint(fake_git) != await tree_fingerprint(fake_git)


async def test_run_checks_raises_unavailable_when_the_sandbox_returns_no_result(fake_bash):
    fake_bash.results = []
    with pytest.raises(ChecksUnavailableError):
        await run_checks(fake_bash, ["make test"])


async def test_tree_fingerprint_is_the_diff_fingerprint_of_the_tree(fake_git):
    fake_git.diffs = ["diff --git a/a.py b/a.py\n"]

    assert await tree_fingerprint(fake_git) == diff_fingerprint("diff --git a/a.py b/a.py\n")
