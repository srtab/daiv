from __future__ import annotations

import httpx
import pytest
from git import GitCommandError

from automation.agent.verification.checks import (
    CheckResult,
    ChecksUnavailableError,
    failing_before_change,
    redacted_values,
    run_checks,
    run_checks_in_place,
)
from automation.agent.workspace.git_manager import PendingMerge, StashRestoreError
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
        self.drop_error: Exception | None = None
        self.diff_error: Exception | None = None

    async def pending_merge(self):
        if self.merge_error is not None:
            raise self.merge_error
        return self.merge

    async def stash_all(self, message: str) -> bool:
        self.events.append("stash_all")
        if self.stash_error is not None:
            raise self.stash_error
        return self.stashed

    async def save_tree(self, message: str) -> bool:
        self.events.append("save_tree")
        if self.stash_error is not None:
            raise self.stash_error
        return self.stashed

    async def restore_stash(self) -> None:
        self.events.append("restore_stash")
        if self.restore_error is not None:
            raise self.restore_error

    async def drop_stash(self) -> None:
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
    fake_bash.error = httpx.ConnectError("All connection attempts failed: http://sandbox:8000/session/s-1/")
    with pytest.raises(ChecksUnavailableError) as raised:
        await run_checks(fake_bash, ["make test"])

    assert str(raised.value) == "the sandbox could not be reached to run the checks"


def _failing(*commands: str) -> list[CheckResult]:
    return [CheckResult(command, 1, "", signature=frozenset({f"{command} now"})) for command in commands]


async def test_run_checks_signature_ignores_counts_timings_and_addresses(fake_bash):
    fake_bash.results = [RunCommandResult(command="t", output="1 failed in 0.52s\n<obj at 0x7f3a2b>\n\n", exit_code=1)]
    [first] = await run_checks(fake_bash, ["t"])
    fake_bash.results = [RunCommandResult(command="t", output="3 failed in 12.1s\n<obj at 0x9c>", exit_code=1)]
    [second] = await run_checks(fake_bash, ["t"])

    assert first.signature == second.signature == frozenset({"# failed in #.#s", "<obj at #>"})


async def test_failing_before_change_maps_commands_failing_on_starting_code_to_their_signature(fake_bash, fake_git):
    fake_bash.results = [RunCommandResult(command="x", output="E1 broken", exit_code=1)]

    found = await failing_before_change(fake_bash, fake_git, _failing("make lint"))

    assert found == {"make lint": frozenset({"E# broken"})}
    assert fake_git.events == ["stash_all", "restore_stash"]


async def test_failing_before_change_leaves_out_commands_that_pass_on_starting_code(fake_bash, fake_git):
    fake_bash.exit_codes = [1, 0]

    found = await failing_before_change(fake_bash, fake_git, _failing("make lint", "make test"))

    assert set(found) == {"make lint"}


async def test_failing_before_change_masks_the_values_in_the_starting_output(fake_bash, fake_git):
    fake_bash.results = [RunCommandResult(command="x", output="token s3cr3t-value", exit_code=1)]

    found = await failing_before_change(fake_bash, fake_git, _failing("make test"), redact=("s3cr3t-value",))

    assert found == {"make test": frozenset({"token ******"})}


async def test_failing_before_change_restores_tree_even_when_command_raises(fake_bash, fake_git):
    fake_bash.error = httpx.ConnectError("down")
    with pytest.raises(ChecksUnavailableError):
        await failing_before_change(fake_bash, fake_git, _failing("make test"))
    assert fake_git.events == ["stash_all", "restore_stash"]


async def test_failing_before_change_is_unavailable_when_the_changes_cannot_be_set_aside(fake_bash, fake_git):
    fake_git.stash_error = GitCommandError(["git", "stash", "push"], 1, "error: could not write index")

    with pytest.raises(ChecksUnavailableError):
        await failing_before_change(fake_bash, fake_git, _failing("make test"))

    assert fake_git.events == ["stash_all"]
    assert fake_bash.calls == []


async def test_failing_before_change_is_unavailable_when_the_tree_cannot_be_read(fake_bash, fake_git):
    fake_git.merge_error = httpx.ReadTimeout("timed out")

    with pytest.raises(ChecksUnavailableError):
        await failing_before_change(fake_bash, fake_git, _failing("make test"))

    assert fake_git.events == []


async def test_failing_before_change_propagates_a_failed_restore(fake_bash, fake_git):
    fake_git.restore_error = StashRestoreError()

    with pytest.raises(StashRestoreError):
        await failing_before_change(fake_bash, fake_git, _failing("make test"))

    assert fake_git.events == ["stash_all", "restore_stash"]


async def test_failing_before_change_skips_pending_merge(fake_bash, fake_git):
    fake_git.merge = PendingMerge(head="a" * 40, branch=None, unmerged_paths=(), conflicted_paths=())
    assert await failing_before_change(fake_bash, fake_git, _failing("make test")) == {}
    assert fake_git.events == []
    assert fake_bash.calls == []


async def test_failing_before_change_uses_the_current_output_when_nothing_was_stashed(fake_bash, fake_git):
    fake_git.stashed = False
    assert await failing_before_change(fake_bash, fake_git, _failing("make test")) == {
        "make test": frozenset({"make test now"})
    }
    assert fake_git.events == ["stash_all"]
    assert fake_bash.calls == []


async def test_run_checks_raises_unavailable_when_the_sandbox_returns_no_result(fake_bash):
    fake_bash.results = []
    with pytest.raises(ChecksUnavailableError):
        await run_checks(fake_bash, ["make test"])


AGENT_DIFF = "diff --git a/a.py b/a.py\n+new\n"
LOCK_DIFF = "diff --git a/uv.lock b/uv.lock\n+v2\n"


async def test_run_checks_in_place_drops_the_saved_tree_when_the_checks_change_nothing(fake_bash, fake_git):
    fake_git.diffs = [AGENT_DIFF]

    results, changed, after = await run_checks_in_place(fake_bash, fake_git, ["make test"], before=AGENT_DIFF)

    assert [result.command for result in results] == ["make test"]
    assert (changed, after) == ([], AGENT_DIFF)
    assert fake_git.events == ["save_tree", "drop_stash"]


async def test_run_checks_in_place_puts_back_what_the_checks_changed(fake_bash, fake_git):
    fake_git.diffs = [AGENT_DIFF + LOCK_DIFF]

    _, changed, after = await run_checks_in_place(fake_bash, fake_git, ["make test"], before=AGENT_DIFF)

    assert (changed, after) == (["uv.lock"], AGENT_DIFF)
    assert fake_git.events == ["save_tree", "restore_stash"]


async def test_run_checks_in_place_keeps_what_the_checks_changed_during_a_merge(fake_bash, fake_git):
    fake_git.merge = PendingMerge(head="a" * 40, branch=None, unmerged_paths=(), conflicted_paths=())
    fake_git.diffs = [AGENT_DIFF + LOCK_DIFF]

    _, changed, after = await run_checks_in_place(fake_bash, fake_git, ["make test"], before=AGENT_DIFF)

    assert (changed, after) == (["uv.lock"], AGENT_DIFF + LOCK_DIFF)
    assert fake_git.events == []


async def test_run_checks_in_place_tolerates_a_failed_drop(fake_bash, fake_git):
    fake_git.diffs = [AGENT_DIFF]
    fake_git.drop_error = GitCommandError(["git", "stash", "drop"], 1, "error")

    _, changed, _ = await run_checks_in_place(fake_bash, fake_git, ["make test"], before=AGENT_DIFF)

    assert changed == []


async def test_run_checks_in_place_is_unavailable_when_the_tree_cannot_be_saved(fake_bash, fake_git):
    fake_git.stash_error = GitCommandError(["git", "stash", "push"], 1, "error: could not write index")

    with pytest.raises(ChecksUnavailableError):
        await run_checks_in_place(fake_bash, fake_git, ["make test"], before=AGENT_DIFF)

    assert fake_bash.calls == []


async def test_run_checks_in_place_puts_the_tree_back_when_the_checks_cannot_run(fake_bash, fake_git):
    fake_bash.error = httpx.ConnectError("down")

    with pytest.raises(ChecksUnavailableError):
        await run_checks_in_place(fake_bash, fake_git, ["make test"], before=AGENT_DIFF)

    assert fake_git.events == ["save_tree", "restore_stash"]


async def test_run_checks_in_place_is_unavailable_when_the_tree_cannot_be_read_after(fake_bash, fake_git):
    fake_git.diff_error = httpx.ReadTimeout("timed out")

    with pytest.raises(ChecksUnavailableError) as raised:
        await run_checks_in_place(fake_bash, fake_git, ["make test"], before=AGENT_DIFF)

    assert str(raised.value) == "the repository could not be read to run the checks"
    assert fake_git.events == ["save_tree", "restore_stash"]
