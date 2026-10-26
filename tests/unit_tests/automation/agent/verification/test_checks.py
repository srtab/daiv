from __future__ import annotations

import httpx
import pytest

from automation.agent.verification.checks import (
    CheckResult,
    ChecksUnavailableError,
    diff_fingerprint,
    failing_before_change,
    run_checks,
    tree_fingerprint,
)
from automation.agent.workspace.git_manager import PendingMerge
from core.sandbox.schemas import RunCommandResult, RunCommandsResponse


class FakeBash:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], bool, int | None]] = []
        self.exit_codes: list[int] | None = None
        self.results: list[RunCommandResult] | None = None
        self.error: Exception | None = None

    async def run_commands(self, commands, *, fail_fast, timeout=None):
        self.calls.append((list(commands), fail_fast, timeout))
        if self.error:
            raise self.error
        if self.results is not None:
            return RunCommandsResponse(results=self.results)
        codes = self.exit_codes or [0] * len(commands)
        return RunCommandsResponse(
            results=[
                RunCommandResult(command=c, output="", exit_code=code) for c, code in zip(commands, codes, strict=True)
            ]
        )


class FakeGit:
    def __init__(self) -> None:
        self.stashed = True
        self.merge: PendingMerge | None = None
        self.diffs = ["diff"]
        self.events: list[str] = []

    async def pending_merge(self):
        return self.merge

    async def stash_all(self, message: str) -> bool:
        self.events.append("stash_all")
        return self.stashed

    async def restore_stash(self) -> None:
        self.events.append("restore_stash")

    async def get_diff(self) -> str:
        return self.diffs.pop(0)


@pytest.fixture
def fake_bash() -> FakeBash:
    return FakeBash()


@pytest.fixture
def fake_git() -> FakeGit:
    return FakeGit()


async def test_run_checks_wraps_commands_in_repo_dir(fake_bash):
    await run_checks(fake_bash, ["make lint", "make test"])
    assert fake_bash.calls == [(["cd /workspace/repo && make lint", "cd /workspace/repo && make test"], False, 600)]


async def test_run_checks_keeps_declared_command_and_output_tail(fake_bash):
    fake_bash.results = [RunCommandResult(command="cd /workspace/repo && make test", output="x" * 5_000, exit_code=1)]
    [result] = await run_checks(fake_bash, ["make test"])
    assert result.command == "make test" and len(result.output_tail) == 4_000 and not result.passed


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


async def test_run_checks_raises_unavailable_when_result_count_differs(fake_bash):
    fake_bash.results = [RunCommandResult(command="cd /workspace/repo && make lint", output="", exit_code=0)]
    with pytest.raises(ChecksUnavailableError):
        await run_checks(fake_bash, ["make lint", "make test"])


async def test_tree_fingerprint_is_the_diff_fingerprint_of_the_tree(fake_git):
    fake_git.diffs = ["diff --git a/a.py b/a.py\n"]

    assert await tree_fingerprint(fake_git) == diff_fingerprint("diff --git a/a.py b/a.py\n")
