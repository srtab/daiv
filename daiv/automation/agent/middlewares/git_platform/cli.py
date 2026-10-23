from __future__ import annotations

import asyncio
import logging
import os
import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from automation.agent.middlewares.file_system import write_tool_result_to_file
from automation.agent.utils import repo_relative_text

if TYPE_CHECKING:
    from collections.abc import Callable

    from deepagents.backends.protocol import BackendProtocol
    from langchain.tools import ToolRuntime

    from automation.agent.context import RuntimeCtx


logger = logging.getLogger("daiv.tools")


DEFAULT_CLI_TIMEOUT = 30

type AllowCommands = dict[str, set[str] | Literal["*"]]


@dataclass(frozen=True)
class PlatformCli:
    """One platform's CLI as :func:`check_subcommand` and :func:`run_cli` see it: how its errors read and what its
    policy allows."""

    display_name: str
    tool_name: str
    executable: str
    usage: str
    example: str
    allow_commands: AllowCommands
    has_disallowed_flags: Callable[[list[str]], bool]
    disallowed_flags_error: str


class CliOutcomes(Protocol):
    """Takes over what :func:`run_cli` reports, for a call whose CLI errors must not reach the model as they are."""

    async def on_timeout(self, message: str) -> str: ...

    async def on_launch_error(self) -> str: ...

    async def on_failure(self, returncode: int, stderr: str) -> str: ...

    async def on_success(self) -> str | None:
        """``None`` lets the output through; a string withholds it and is reported instead."""
        ...


@dataclass(frozen=True)
class CliInvocation:
    """How to run one checked subcommand: the arguments after the executable, the platform's environment, what
    to do to a successful output before the model reads it, and who reports its outcome instead of the defaults."""

    args: list[str]
    env: dict[str, str]
    postprocess: Callable[[str], str] | None = None
    outcomes: CliOutcomes | None = None


class PolicyRefusal(str):
    """An ``error: ...`` from :func:`check_subcommand` that the platform's policy refused, as opposed to a malformed
    subcommand."""

    __slots__ = ()


_PUBLISHED_TEXT_FLAGS = frozenset({
    "--body",
    "--title",
    "--description",
    "--notes",
    "--note",
    "--message",
    "--content",
    "-b",
    "-t",
    "-n",
})


def _repo_relative_flag_values(args: list[str]) -> list[str]:
    """``args`` with the values of the flags that carry published text made repo-relative.

    Handles ``--flag value`` and ``--flag=value``. Every other argument, file and position flags included,
    is passed through unchanged.
    """
    rewritten: list[str] = []
    value_follows = False
    for arg in args:
        if value_follows:
            rewritten.append(repo_relative_text(arg))
            value_follows = False
            continue
        flag, sep, value = arg.partition("=")
        if sep and flag in _PUBLISHED_TEXT_FLAGS:
            rewritten.append(f"{flag}={repo_relative_text(value)}")
        else:
            rewritten.append(arg)
            value_follows = arg in _PUBLISHED_TEXT_FLAGS
    return rewritten


def _is_allowed_cli_command(resource: str, action: str, allow_commands: AllowCommands) -> tuple[bool, str]:
    allowed_actions = allow_commands.get(resource)
    if allowed_actions is None:
        logger.warning("[git-platform] The subcommand '%s' is not allowed by policy.", resource)
        return False, f"error: The subcommand '{resource}' is not allowed by policy."

    if allowed_actions == "*":
        return True, ""

    if action in allowed_actions:
        return True, ""

    logger.warning("[git-platform] The action '%s' for subcommand '%s' is not allowed by policy.", action, resource)
    return False, f"error: The action '{action}' for subcommand '{resource}' is not allowed by policy."


def _cli_failure_message(platform: str, returncode: int, stdout: bytes, stderr: bytes) -> str:
    """Report a failed CLI call with its stdout too: some commands exit non-zero with the answer on stdout
    (``gh pr checks`` exits 1 when a check failed and prints the checks table)."""
    message = f"error: {platform} command failed (exit code {returncode}). Details: {stderr.decode('utf-8').strip()}"
    if output := stdout.decode("utf-8").strip():
        message += f"\nOutput:\n{output}"
    return message


def check_subcommand(cli: PlatformCli, subcommand: str) -> list[str] | str:
    """Parse ``subcommand`` and check it against ``cli``'s policy: the arguments, or an ``error: ...`` to return, a
    ``PolicyRefusal`` when the policy refused it."""
    if not subcommand or not subcommand.strip():
        return f"error: Subcommand cannot be empty. Format: '{cli.usage}'"

    try:
        args = _repo_relative_flag_values(shlex.split(subcommand.strip()))
    except ValueError as e:
        return f"error: Failed to parse subcommand: {e}. Check for unmatched quotes."

    incomplete = f"error: Incomplete subcommand. Expected format: '{cli.usage}'. Got: '{subcommand}'"
    if len(args) < 2:
        return incomplete

    resource, action = args[0:2]

    if resource == cli.executable:
        return (
            f"error: Do not include '{cli.executable}' command prefix. "
            f"Start directly with the object. Example: '{cli.example}'"
        )

    if not action:
        return incomplete

    is_allowed, policy_message = _is_allowed_cli_command(resource, action, cli.allow_commands)
    if not is_allowed:
        return PolicyRefusal(policy_message)

    if cli.has_disallowed_flags(args[2:]):
        return PolicyRefusal(cli.disallowed_flags_error)

    return args


async def run_cli(
    cli: PlatformCli,
    invocation: CliInvocation,
    runtime: ToolRuntime[RuntimeCtx],
    *,
    to_file: bool,
    backend: BackendProtocol,
) -> str:
    """Run ``invocation`` as a subprocess, without shell expansion, and return what the model reads back.

    With ``to_file`` the full result is written to the large-tool-results dir through ``backend`` and a compact
    confirmation comes back; otherwise the output comes back inline, and the filesystem middleware evicts it to that
    same dir when it is too large.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),  # noqa: S108
        **invocation.env,
    }
    outcomes = invocation.outcomes

    try:
        process = await asyncio.create_subprocess_exec(
            cli.executable, *invocation.args, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=DEFAULT_CLI_TIMEOUT)
    except TimeoutError:
        try:
            process.kill()
            await process.wait()
        except Exception as e:
            logger.warning("[%s] Failed to kill %s process: %s", cli.tool_name, cli.display_name, e)
        message = (
            f"error: {cli.display_name} command timed out after {DEFAULT_CLI_TIMEOUT} seconds. "
            "The operation may be too complex or the API is slow."
        )
        return await outcomes.on_timeout(message) if outcomes else message
    except Exception as e:
        logger.exception("[%s] Failed to execute %s command.", cli.tool_name, cli.display_name)
        if outcomes:
            return await outcomes.on_launch_error()
        return f"error: Failed to execute {cli.display_name} command. Details: {e}"

    if process.returncode != 0:
        if outcomes:
            return await outcomes.on_failure(process.returncode, stderr.decode("utf-8").strip())
        return _cli_failure_message(cli.display_name, process.returncode, stdout, stderr)

    if outcomes and (withheld := await outcomes.on_success()) is not None:
        return withheld

    output = stdout.decode("utf-8").strip()
    if not output:
        # Failures returned above, so "no file was written" means a genuinely empty result, not a failed command.
        empty = "(empty result — command succeeded with no output, e.g. an empty list or no matches)"
        if to_file:
            empty += "\n(note: no file was written — the command produced no output.)"
        return empty

    if invocation.postprocess:
        output = invocation.postprocess(output)

    if to_file:
        return await write_tool_result_to_file(output, runtime=runtime, backend=backend, tool_name=cli.tool_name)
    return output
