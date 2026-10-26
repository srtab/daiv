from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
from git import GitCommandError

from automation.agent.constants import REPO_PATH
from automation.agent.workspace.git_runners import SandboxGitProtocolError
from automation.agent.workspace.sandbox_backend import SANDBOX_TIMEOUT_EXIT

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from automation.agent.workspace.git_manager import GitManager
    from automation.agent.workspace.sandbox_backend import SandboxFileBackend

MAX_FIX_ROUNDS = 2
CHECK_TIMEOUT_SECONDS = 600
OUTPUT_TAIL_CHARS = 4_000
# Past the sandbox's own limit, so a check that runs out of time comes back as exit 124, not as a client timeout.
_RESPONSE_GRACE_SECONDS = 60
_REDACTED = "******"
# Shorter values (``true``, ``8000``, ``python``) would mask ordinary words all over the output.
_MIN_REDACTED_LENGTH = 8

_DIFF_HEADER = re.compile(r"^diff --git ", re.MULTILINE)


@dataclass(frozen=True)
class CheckResult:
    command: str
    exit_code: int
    output_tail: str

    @property
    def passed(self) -> bool:
        return self.exit_code == 0

    @property
    def timed_out(self) -> bool:
        return self.exit_code == SANDBOX_TIMEOUT_EXIT


class ChecksUnavailableError(Exception):
    """The checks could not be run; ``str(exc)`` is the reason shown to people, a fixed sentence that names no host,
    session or path, and the cause is chained for the logs."""


def redacted_values(env_vars: Mapping[str, str]) -> tuple[str, ...]:
    """The values of the sandbox's env vars, which may be secrets, that check output must not show."""
    return tuple(value for value in env_vars.values() if len(value) >= _MIN_REDACTED_LENGTH)


def _redact(text: str, values: Sequence[str]) -> str:
    for value in sorted(values, key=len, reverse=True):
        text = text.replace(value, _REDACTED)
    return text


async def run_checks(
    bash: SandboxFileBackend, commands: Sequence[str], *, redact: Sequence[str] = ()
) -> list[CheckResult]:
    """Run every command from the repo root, each in a request of its own so one that fails or times out never stops
    the others, and return one result per command, every ``redact`` value in its output masked."""
    return [await _run_check(bash, command, redact) for command in commands]


async def _run_check(bash: SandboxFileBackend, command: str, redact: Sequence[str]) -> CheckResult:
    try:
        response = await bash.run_commands(
            [f"cd {REPO_PATH} && {command}"],
            fail_fast=False,
            timeout=CHECK_TIMEOUT_SECONDS,
            http_timeout=CHECK_TIMEOUT_SECONDS + _RESPONSE_GRACE_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise ChecksUnavailableError("the sandbox could not be reached to run the checks") from exc
    if not response.results:
        raise ChecksUnavailableError("the sandbox returned no result for a check")
    result = response.results[0]
    output = _redact(result.output, redact)
    return CheckResult(command=command, exit_code=result.exit_code, output_tail=output[-OUTPUT_TAIL_CHARS:])


async def failing_before_change(bash: SandboxFileBackend, git: GitManager, commands: Sequence[str]) -> frozenset[str]:
    """The commands that already fail on the starting code, found by stashing the agent's changes and running them.

    Failing to set the changes aside leaves the tree as it was, so it is reported as the checks being unavailable;
    failing to restore them raises.
    """
    try:
        if await git.pending_merge() is not None:
            return frozenset()
        stashed = await git.stash_all("daiv: pre-publish verification")
    except (GitCommandError, httpx.HTTPError, SandboxGitProtocolError) as exc:
        raise ChecksUnavailableError("could not set the changes aside to re-check the starting code") from exc
    if not stashed:
        # Nothing was stashed, so a pop would restore an older, unrelated stash.
        return frozenset(commands)
    try:
        results = await run_checks(bash, commands)
    finally:
        await git.restore_stash()
    return frozenset(result.command for result in results if not result.passed)


def diff_fingerprint(diff: str) -> str:
    return hashlib.sha256(diff.encode()).hexdigest()


async def tree_fingerprint(git: GitManager) -> str:
    return diff_fingerprint(await git.get_diff())


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
