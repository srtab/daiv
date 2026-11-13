from __future__ import annotations

import hashlib
import shlex
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from automation.agent.constants import REPO_PATH
from automation.agent.middlewares.sandbox import is_bare_timeout_notice
from automation.agent.workspace.git_manager import GIT_ERRORS

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from automation.agent.workspace.git_manager import GitManager
    from automation.agent.workspace.sandbox_backend import SandboxFileBackend

MAX_FIX_ROUNDS = 2
CHECK_TIMEOUT_SECONDS = 600
VERIFICATION_BUDGET_SECONDS = 1_800
OUTPUT_TAIL_CHARS = 4_000
_KILL_AFTER_SECONDS = 10
# Past ``timeout``'s own limit, so ``timeout`` stops a check, and what it started, before the sandbox gives up on it.
_SANDBOX_TIMEOUT_SECONDS = CHECK_TIMEOUT_SECONDS + _KILL_AFTER_SECONDS + 20
_STOPPED_BY_TIMEOUT = frozenset({124, 137})
UNREADABLE = "the repository could not be read to run the checks"
OUT_OF_TIME = f"the checks used up their {VERIFICATION_BUDGET_SECONDS // 60} minutes for this run"
_STASH_MESSAGE = "daiv: pre-publish verification"


@dataclass(frozen=True)
class CheckResult:
    command: str
    exit_code: int
    output_tail: str
    timed_out: bool = False
    """Whether the sandbox cut the command off; exit 124 from the command itself (``timeout 60 make``) is not."""

    @property
    def passed(self) -> bool:
        return self.exit_code == 0


@dataclass(frozen=True)
class CheckRun:
    """One run of the checks on a tree: its results and the fingerprint of the tree it left."""

    results: list[CheckResult]
    fingerprint: str


class ChecksUnavailableError(Exception):
    """The checks could not be run; ``str(exc)`` is the reason shown to people, a fixed sentence that names no host,
    session or path, and the cause is chained for the logs."""


class CheckRunner:
    """
    Runs a repository's ``before_publish`` checks in the sandbox, each from the repo root in a request of its own so one
    that fails or times out never stops the others. Whatever they change stays in the tree.

    A check runs under ``timeout`` where the sandbox has it, which stops the check and every process it started in its
    process group after ``CHECK_TIMEOUT_SECONDS``, so nothing it left running writes to the tree after the checks.
    Every check a run starts shares ``VERIFICATION_BUDGET_SECONDS``, counted from the first: once it is spent, no
    further check starts and ``ChecksUnavailableError`` says so, so a run waits at most the budget and one check.

    Args:
        bash: The sandbox shell the checks run in.
        git: The run's git, which reads the tree and sets it aside to run the checks on the starting code.
        clock: Monotonic seconds, which the budget is measured in.
    """

    def __init__(
        self, bash: SandboxFileBackend, git: GitManager, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._bash = bash
        self._git = git
        self._clock = clock
        self._deadline: float | None = None

    async def run(self, commands: Sequence[str]) -> list[CheckResult]:
        """One result per command."""
        return [await self._run_one(command) for command in commands]

    async def run_on_tree(self, commands: Sequence[str]) -> CheckRun:
        """Run the checks on the tree as it is, leaving whatever they change in it, and fingerprint the tree they left.
        Failing to read it afterwards raises ``ChecksUnavailableError``."""
        results = await self.run(commands)
        try:
            after = await self._git.get_diff()
        except GIT_ERRORS as exc:
            raise ChecksUnavailableError(UNREADABLE) from exc
        return CheckRun(results, diff_fingerprint(after))

    async def failing_before_change(self, failing: Sequence[CheckResult], *, merging: bool) -> frozenset[str]:
        """The ``failing`` commands that already fail on the starting code, found by stashing the agent's changes and
        running them; none during a merge in progress (``merging``), and all of them when there was nothing to stash.

        Failing to set the changes aside leaves the tree as it was, so it is reported as the checks being unavailable;
        failing to restore them raises ``StashRestoreError``.
        """
        if merging:
            return frozenset()
        try:
            stashed = await self._git.stash_all(_STASH_MESSAGE)
        except GIT_ERRORS as exc:
            raise ChecksUnavailableError("could not set the changes aside to re-check the starting code") from exc
        if stashed is None:
            return frozenset(result.command for result in failing)
        try:
            results = await self.run([result.command for result in failing])
        finally:
            await self._git.restore_stash(stashed)
        return frozenset(result.command for result in results if not result.passed)

    async def _run_one(self, command: str) -> CheckResult:
        self._claim_time()
        started = self._clock()
        try:
            response = await self._bash.run_commands(
                [self.command_line(command)], fail_fast=False, timeout=_SANDBOX_TIMEOUT_SECONDS
            )
        except httpx.HTTPError as exc:
            raise ChecksUnavailableError("the sandbox could not be reached to run the checks") from exc
        if not response.results:
            raise ChecksUnavailableError("the sandbox returned no result for a check")
        result = response.results[0]
        stopped = result.exit_code in _STOPPED_BY_TIMEOUT and self._clock() - started >= CHECK_TIMEOUT_SECONDS
        return CheckResult(
            command=command,
            exit_code=result.exit_code,
            output_tail=result.output[-OUTPUT_TAIL_CHARS:],
            timed_out=stopped or result.timed_out or is_bare_timeout_notice(result, _SANDBOX_TIMEOUT_SECONDS),
        )

    @staticmethod
    def command_line(command: str) -> str:
        """The shell line that runs ``command`` from the repo root, under ``timeout`` when the sandbox has it, in the
        shell the sandbox runs it in."""
        quoted = shlex.quote(command)
        shell = '"${BASH:-sh}" -c ' + quoted
        return (
            f"cd {REPO_PATH} && if command -v timeout >/dev/null 2>&1; "
            f"then timeout -k {_KILL_AFTER_SECONDS} {CHECK_TIMEOUT_SECONDS} {shell}; else {shell}; fi"
        )

    def _claim_time(self) -> None:
        """Start the budget on the run's first check; raise ``ChecksUnavailableError`` once it is spent."""
        now = self._clock()
        if self._deadline is None:
            self._deadline = now + VERIFICATION_BUDGET_SECONDS
        elif now >= self._deadline:
            raise ChecksUnavailableError(OUT_OF_TIME)


def diff_fingerprint(diff: str) -> str:
    return hashlib.sha256(diff.encode()).hexdigest()
