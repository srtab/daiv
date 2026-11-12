from __future__ import annotations

import base64
import hashlib
import logging
import re
import shlex
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import quote

import httpx

from automation.agent.constants import REPO_PATH
from automation.agent.middlewares.sandbox import is_bare_timeout_notice
from automation.agent.workspace.git_manager import GIT_ERRORS

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from automation.agent.workspace.git_manager import GitManager
    from automation.agent.workspace.sandbox_backend import SandboxFileBackend

logger = logging.getLogger("daiv.agent")

MAX_FIX_ROUNDS = 2
CHECK_TIMEOUT_SECONDS = 600
VERIFICATION_BUDGET_SECONDS = 1_800
OUTPUT_TAIL_CHARS = 4_000
_KILL_AFTER_SECONDS = 10
# Past ``timeout``'s own limit, so ``timeout`` stops a check, and what it started, before the sandbox gives up on it.
_SANDBOX_TIMEOUT_SECONDS = CHECK_TIMEOUT_SECONDS + _KILL_AFTER_SECONDS + 20
_STOPPED_BY_TIMEOUT = frozenset({124, 137})
_OUTPUT_WINDOW_CHARS = 200_000
UNREADABLE = "the repository could not be read to run the checks"
OUT_OF_TIME = f"the checks used up their {VERIFICATION_BUDGET_SECONDS // 60} minutes for this run"
_STASH_MESSAGE = "daiv: pre-publish verification"
_REDACTED = "******"
# Shorter values (``true``, ``8000``, ``python``) would mask ordinary words all over the output.
_MIN_REDACTED_LENGTH = 8

_DIFF_HEADER = re.compile(r"^diff --git ", re.MULTILINE)


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
    """One run of the checks on a tree: its results, the files it changed, and the fingerprint of the tree it left."""

    results: list[CheckResult]
    changed_by_checks: list[str]
    fingerprint: str


class ChecksUnavailableError(Exception):
    """The checks could not be run; ``str(exc)`` is the reason shown to people, a fixed sentence that names no host,
    session or path, and the cause is chained for the logs."""


class CheckRunner:
    """
    Runs a repository's ``before_publish`` checks in the sandbox, each from the repo root in a request of its own so one
    that fails or times out never stops the others, and puts back whatever they change in the tree.

    A check runs under ``timeout`` where the sandbox has it, which stops the check and every process it started in its
    process group after ``CHECK_TIMEOUT_SECONDS``, so nothing it left running writes to the tree after the checks.
    Every check a run starts shares ``VERIFICATION_BUDGET_SECONDS``, counted from the first: once it is spent, no
    further check starts and ``ChecksUnavailableError`` says so, so a run waits at most the budget and one check.

    Only the last ``_OUTPUT_WINDOW_CHARS`` of a check's output are masked and compared, so a check that prints without
    end costs no more than that.

    Args:
        bash: The sandbox shell the checks run in.
        git: The run's git, which sets the tree aside and puts it back.
        redact: Values masked in the checks' output before anyone sees it.
        clock: Monotonic seconds, which the budget is measured in.
    """

    def __init__(
        self,
        bash: SandboxFileBackend,
        git: GitManager,
        *,
        redact: Sequence[str] = (),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bash = bash
        self._git = git
        self._redact = sorted(redact, key=len, reverse=True)
        self._clock = clock
        self._deadline: float | None = None

    @staticmethod
    def redacted_values(env_vars: Mapping[str, str]) -> tuple[str, ...]:
        """The values of the sandbox's env vars, which may be secrets, and their URL- and base64-encoded forms, that
        check output must not show."""
        values = [value for value in env_vars.values() if len(value) >= _MIN_REDACTED_LENGTH]
        encoded = [
            form
            for value in values
            for form in (quote(value, safe=""), base64.b64encode(value.encode()).decode().rstrip("="))
        ]
        return tuple(dict.fromkeys([*values, *encoded]))

    async def run(self, commands: Sequence[str]) -> list[CheckResult]:
        """One result per command, every ``redact`` value in its output masked."""
        return [await self._run_one(command) for command in commands]

    async def run_in_place(self, commands: Sequence[str], *, before: str, merging: bool) -> CheckRun:
        """Run the checks on the tree whose diff is ``before``, then put back the files they changed so none of it is
        published.

        The tree is saved in the stash first, which a merge in progress (``merging``) would not survive, so during a
        merge what the checks changed stays. Failing to save the tree or to read it afterwards raises
        ``ChecksUnavailableError``; failing to put it back raises ``StashRestoreError``.
        """
        try:
            saved = None if merging else await self._git.save_tree(_STASH_MESSAGE)
        except GIT_ERRORS as exc:
            raise ChecksUnavailableError("could not set the changes aside to run the checks") from exc
        try:
            results = await self.run(commands)
            try:
                after = await self._git.get_diff()
            except GIT_ERRORS as exc:
                raise ChecksUnavailableError(UNREADABLE) from exc
        except Exception:
            if saved is not None:
                await self._git.restore_stash(saved)
            raise
        changed = changed_between(before, after)
        if saved is not None and changed:
            await self._git.restore_stash(saved)
            after = before
        elif saved is not None:
            try:
                await self._git.drop_stash(saved)
            except GIT_ERRORS:
                logger.warning("Could not drop the stash entry saved before the checks", exc_info=True)
        return CheckRun(results, changed, diff_fingerprint(after))

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
        output = self._masked_window(result.output)
        return CheckResult(
            command=command,
            exit_code=result.exit_code,
            output_tail=output[-OUTPUT_TAIL_CHARS:],
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

    def _masked_window(self, output: str) -> str:
        """The masked end of ``output``. The window is cut a longest value wider and trimmed by as much after masking,
        so a value the cut splits is dropped rather than shown in part."""
        margin = len(self._redact[0]) if self._redact else 0
        window = output[-(_OUTPUT_WINDOW_CHARS + margin) :]
        masked = self._mask(window)
        if len(window) < len(output):
            masked = masked[margin:]
        return masked[-_OUTPUT_WINDOW_CHARS:]

    def _mask(self, text: str) -> str:
        for value in self._redact:
            text = text.replace(value, _REDACTED)
        return text


def diff_fingerprint(diff: str) -> str:
    return hashlib.sha256(diff.encode()).hexdigest()


def _section_path(header: str) -> str:
    """The path in a ``a/<path> b/<path>`` header; a rename's header, which names two paths, is kept whole."""
    path = header[2 : 2 + (len(header) - 5) // 2]
    return path if header == f"a/{path} b/{path}" else header


def _diff_sections(diff: str) -> dict[str, str]:
    sections = _DIFF_HEADER.split(diff)[1:]
    return {_section_path(section.partition("\n")[0]): section for section in sections}


def changed_between(before: str, after: str) -> list[str]:
    old, new = _diff_sections(before), _diff_sections(after)
    return [path for path in dict.fromkeys([*old, *new]) if old.get(path) != new.get(path)]
