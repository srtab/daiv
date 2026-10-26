from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from automation.agent.constants import REPO_PATH

if TYPE_CHECKING:
    from collections.abc import Sequence

    from automation.agent.workspace.git_manager import GitManager
    from automation.agent.workspace.sandbox_backend import SandboxFileBackend

MAX_FIX_ROUNDS = 2
CHECK_TIMEOUT_SECONDS = 600
OUTPUT_TAIL_CHARS = 4_000
TIMEOUT_EXIT_CODE = 124

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
        return self.exit_code == TIMEOUT_EXIT_CODE


class ChecksUnavailableError(Exception):
    """The checks could not be run; ``str(exc)`` is the reason shown to people."""


async def run_checks(bash: SandboxFileBackend, commands: Sequence[str]) -> list[CheckResult]:
    """Run every command from the repo root, none stopping the others, and return one result per command."""
    try:
        response = await bash.run_commands(
            [f"cd {REPO_PATH} && {command}" for command in commands], fail_fast=False, timeout=CHECK_TIMEOUT_SECONDS
        )
    except httpx.HTTPError as exc:
        raise ChecksUnavailableError(f"the sandbox could not be reached to run the checks ({exc})") from exc
    if len(response.results) != len(commands):
        raise ChecksUnavailableError("the sandbox returned an unexpected number of results")
    return [
        CheckResult(command=command, exit_code=result.exit_code, output_tail=result.output[-OUTPUT_TAIL_CHARS:])
        for command, result in zip(commands, response.results, strict=True)
    ]


async def failing_before_change(bash: SandboxFileBackend, git: GitManager, commands: Sequence[str]) -> frozenset[str]:
    """The commands that already fail on the starting code, found by stashing the agent's changes and running them."""
    if await git.pending_merge() is not None:
        return frozenset()
    if not await git.stash_all("daiv: pre-publish verification"):
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
