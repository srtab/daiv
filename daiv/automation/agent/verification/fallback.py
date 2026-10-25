from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from langchain_core.messages import AIMessage, ToolMessage

from automation.agent.middlewares.file_system import WRITE_TOOL_NAMES
from automation.agent.middlewares.sandbox import BASH_TOOL_NAME
from automation.agent.workspace.bash_policy.command_parser import CommandParseError, parse_command

if TYPE_CHECKING:
    from collections.abc import Sequence

    from langchain_core.messages import AnyMessage

type FallbackVerdict = Literal["checked", "not_run", "failed"]

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
class FallbackFinding:
    verdict: FallbackVerdict
    command: str | None = None
    exit_code: int | None = None


def _strip_runner_prefixes(argv: tuple[str, ...]) -> tuple[str, ...]:
    stripped = True
    while stripped:
        stripped = False
        for prefix in _RUNNER_PREFIXES:
            if argv[: len(prefix)] == prefix:
                argv = argv[len(prefix) :]
                stripped = True
                break
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


def _bash_results(message: ToolMessage) -> list[tuple[str, int]]:
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


def _last_edit_index(messages: Sequence[AnyMessage]) -> int:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, AIMessage) and any(call["name"] in WRITE_TOOL_NAMES for call in message.tool_calls):
            return index
    return -1


def checks_after_last_edit(messages: Sequence[AnyMessage]) -> FallbackFinding:
    last_check: tuple[str, int] | None = None
    for message in messages[_last_edit_index(messages) + 1 :]:
        if not isinstance(message, ToolMessage) or message.name != BASH_TOOL_NAME:
            continue
        for command, exit_code in _bash_results(message):
            if is_check_command(command):
                last_check = (command, exit_code)
    if last_check is None:
        return FallbackFinding("not_run")
    command, exit_code = last_check
    if exit_code == 0:
        return FallbackFinding("checked", command, exit_code)
    return FallbackFinding("failed", command, exit_code)


def _escape(text: str) -> str:
    return text.replace("<", "&lt;").replace(">", "&gt;")


def fallback_reminder(finding: FallbackFinding, edited_paths: Sequence[str]) -> str:
    match finding.verdict:
        case "not_run":
            files = ", ".join(_escape(path) for path in edited_paths)
            return (
                "<system-reminder>"
                f"You edited files ({files}) but did not run any test, linter or build afterwards. "
                "Before finishing, run the tests, linters or build relevant to your change and fix what they "
                "report. If they cannot be run here, say so in your final answer and explain why."
                "</system-reminder>"
            )
        case "failed":
            return (
                "<system-reminder>"
                f"The last check you ran after your final edit, `{_escape(finding.command or '')}`, failed with "
                f"exit code {finding.exit_code}. Fix the failure and re-run it before finishing, or say in your "
                "final answer why it cannot pass."
                "</system-reminder>"
            )
        case _:
            raise ValueError(f"No reminder for verdict {finding.verdict!r}")
