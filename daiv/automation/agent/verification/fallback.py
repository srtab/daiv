from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from automation.agent.verification.outcome import code_span, escape_tags
from automation.agent.workspace.bash_policy.command_parser import CommandParseError, parse_command

if TYPE_CHECKING:
    from collections.abc import Sequence

    from langchain_core.messages import ToolMessage

_RUNNER_PREFIXES = (
    ("uv", "run"),
    ("poetry", "run"),
    ("pipenv", "run"),
    ("npx",),
    ("bunx",),
    ("pnpm", "exec"),
    ("python", "-m"),
    ("python3", "-m"),
)

_CHECK_TOOLS = frozenset({
    "pytest",
    "py.test",
    "tox",
    "nox",
    "jest",
    "vitest",
    "mocha",
    "rspec",
    "phpunit",
    "ruff",
    "mypy",
    "pyright",
    "ty",
    "tsc",
    "eslint",
    "flake8",
    "pylint",
    "rubocop",
    "golangci-lint",
})

_CHECK_SUBCOMMANDS = {
    "go": frozenset({"test", "vet", "build"}),
    "cargo": frozenset({"test", "clippy", "build", "check"}),
    "make": frozenset({"test", "check", "lint", "build", "ci", "typecheck"}),
    "gradle": frozenset({"test", "check", "build", "verify"}),
    "./gradlew": frozenset({"test", "check", "build", "verify"}),
    "mvn": frozenset({"test", "check", "build", "verify"}),
    "dotnet": frozenset({"test", "build"}),
}

_JS_PACKAGE_MANAGERS = frozenset({"npm", "pnpm", "yarn", "bun"})
_JS_RUN_SCRIPTS = frozenset({"test", "lint", "build", "typecheck", "check", "ci"})


@dataclass(frozen=True)
class Check:
    """A test, linter or build the agent ran through bash, and how it exited."""

    command: str
    exit_code: int


def _strip_runner_prefixes(argv: tuple[str, ...]) -> tuple[str, ...]:
    while prefix := next((prefix for prefix in _RUNNER_PREFIXES if argv[: len(prefix)] == prefix), None):
        argv = argv[len(prefix) :]
    return argv


def _is_check_argv(argv: tuple[str, ...]) -> bool:
    argv = _strip_runner_prefixes(argv)
    if not argv:
        return False
    first, rest = argv[0], argv[1:]
    if first in _CHECK_TOOLS:
        return True
    if first in _CHECK_SUBCOMMANDS:
        return bool(rest) and rest[0] in _CHECK_SUBCOMMANDS[first]
    if first in _JS_PACKAGE_MANAGERS and rest:
        if rest[0] == "test":
            return True
        return rest[0] == "run" and len(rest) > 1 and rest[1] in _JS_RUN_SCRIPTS
    return False


def is_check_command(command: str) -> bool:
    try:
        segments = parse_command(command)
    except CommandParseError:
        return False
    return any(_is_check_argv(segment.argv) for segment in segments)


def bash_results(message: ToolMessage) -> list[tuple[str, int]]:
    """The ``(command, exit_code)`` of each command in a bash tool result; none for a result that is not the tool's
    JSON."""
    if not isinstance(message.content, str):
        return []
    try:
        payload = json.loads(message.content)
    except ValueError:
        return []
    entries = payload.get("commands") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return []
    results = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        command, exit_code = entry.get("command"), entry.get("exit_code")
        if isinstance(command, str) and isinstance(exit_code, int) and not isinstance(exit_code, bool):
            results.append((command, exit_code))
    return results


def last_check(message: ToolMessage) -> Check | None:
    """The last test, linter or build run in a bash tool result; ``None`` when it ran none."""
    checks = [Check(command, exit_code) for command, exit_code in bash_results(message) if is_check_command(command)]
    return checks[-1] if checks else None


def fallback_reminder(failed: Check | None, edited_paths: Sequence[str]) -> str:
    """The reminder for the last check run on the tree, which ``failed``, or for no check run on it (``None``)."""
    if failed is None:
        files = ", ".join(escape_tags(path) for path in edited_paths)
        return (
            "<system-reminder>"
            f"You edited files ({files}) but did not run any test, linter or build afterwards. "
            "Before finishing, run the tests, linters or build relevant to your change and fix what they "
            "report. If they cannot be run here, say so in your final answer and explain why."
            "</system-reminder>"
        )
    return (
        "<system-reminder>"
        f"The last check you ran after your final edit, {code_span(escape_tags(failed.command))}, failed with "
        f"exit code {failed.exit_code}. Fix the failure and re-run it before finishing, or say in your "
        "final answer why it cannot pass."
        "</system-reminder>"
    )
