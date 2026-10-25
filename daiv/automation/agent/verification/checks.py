from __future__ import annotations

import hashlib
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


async def tree_fingerprint(git: GitManager) -> str:
    return hashlib.sha256((await git.get_diff()).encode()).hexdigest()
