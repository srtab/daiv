from __future__ import annotations

import asyncio
from functools import partial
from typing import TYPE_CHECKING

from django.core.cache import cache
from django.utils import timezone

from langchain_core.messages import ToolMessage
from langgraph.types import Command

from automation.agent.middlewares.git_platform.cli import (
    AllowCommands,
    CliInvocation,
    PlatformCli,
    check_subcommand,
    run_cli,
)
from codebase.base import GitPlatform
from codebase.clients.github.utils import get_github_integration
from codebase.clients.utils import clean_job_logs
from codebase.conf import settings

if TYPE_CHECKING:
    from deepagents.backends.protocol import BackendProtocol
    from langchain.tools import ToolRuntime

    from automation.agent.context import RuntimeCtx


GITHUB_TOOL_NAME = "gh"

# Acquired with blocking=True, so without an expiry a dead holder blocks token fetches forever.
GITHUB_TOKEN_LOCK_TIMEOUT = 60


GITHUB_CLI_ALLOW_COMMANDS: AllowCommands = {
    # Typical issue workflow
    "issue": {"status", "list", "view", "create", "edit", "comment", "close", "reopen", "lock", "unlock", "develop"},
    # Typical pull request workflow (without merge/destructive operations)
    "pr": {
        "status",
        "list",
        "view",
        "create",
        "edit",
        "comment",
        "review",
        "checks",
        "diff",
        "close",
        "reopen",
        "lock",
        "unlock",
    },
    # CI investigation workflow
    "workflow": {"list", "view", "run"},
    "run": {"list", "view", "watch", "download", "rerun"},
    # Supporting project data
    "repo": {"list", "view"},
    "release": {"list", "view", "download", "create", "edit", "upload"},
    "ruleset": {"list", "view", "check"},
    "label": {"list", "create", "edit"},
    "cache": {"list", "delete"},
    "search": {"code", "commits", "issues", "prs"},
}


def _gh_has_disallowed_cli_flags(args: list[str]) -> bool:
    for arg in args:
        if arg in {"--repo", "-R", "--hostname"}:
            return True
        if arg.startswith("--repo=") or arg.startswith("--hostname="):
            return True
        if arg.startswith("-R") and arg != "-R":
            return True
    return False


GITHUB_CLI = PlatformCli(
    display_name="GitHub",
    tool_name=GITHUB_TOOL_NAME,
    executable="gh",
    usage="<object> <action> [arguments...]",
    example="issue view 123",
    allow_commands=GITHUB_CLI_ALLOW_COMMANDS,
    has_disallowed_flags=_gh_has_disallowed_cli_flags,
    disallowed_flags_error=(
        "error: The repository and hostname are automatically set. Do not pass --repo, -R, or --hostname."
    ),
)


def _get_cached_github_cli_token(runtime: ToolRuntime[RuntimeCtx]) -> tuple[str, dict[str, str | float] | None]:
    """
    Get the cached GitHub CLI token and return state updates if needed.

    The token is cached using a lock to prevent concurrent state updates from parallel tool calls.

    Returns:
        A tuple of (token, state_updates). state_updates is None if no update is needed.
    """
    assert settings.GITHUB_INSTALLATION_ID is not None, "GITHUB_INSTALLATION_ID is not set"

    token = runtime.state.get("github_token")
    expires_at = runtime.state.get("github_token_expires_at")

    if not token or expires_at is None or timezone.now().timestamp() > expires_at:
        thread_id = runtime.config.get("configurable", {}).get("thread_id", runtime.context.repository.slug)

        with cache.lock(f"github_lock_{thread_id}", timeout=GITHUB_TOKEN_LOCK_TIMEOUT, blocking=True):
            cache_key = f"github_token_{thread_id}"

            if data := cache.get(cache_key):
                # this means a parallel tool call already acquired the lock and cached the token
                return data["github_token"], None

            # no token in cache, so we need to get a new one and cache it
            access_token = get_github_integration().get_access_token(settings.GITHUB_INSTALLATION_ID)

            ttl = access_token.expires_at.timestamp() - timezone.now().timestamp()
            data = {"github_token": access_token.token, "github_token_expires_at": access_token.expires_at.timestamp()}
            cache.set(cache_key, data, timeout=ttl)
            return access_token.token, data

    return token, None


async def run_github_subcommand(
    subcommand: str, runtime: ToolRuntime[RuntimeCtx], to_file: bool, *, backend: BackendProtocol
) -> str | Command:
    """Run a `gh` CLI subcommand on behalf of the ``gh`` tool, scoped to the run's repository.

    Returns a ``Command`` when a refreshed CLI token must be written back to state.
    """
    args = check_subcommand(GITHUB_CLI, subcommand)
    if isinstance(args, str):
        return args

    token, state_update = await asyncio.to_thread(_get_cached_github_cli_token, runtime)
    resource, action = args[0:2]
    is_run_log = resource == "run" and action == "view" and "--log" in args
    invocation = CliInvocation(
        args=[*args, "--repo", runtime.context.repository.slug],
        env={"GIT_TERMINAL_PROMPT": "0", "NO_COLOR": "1", "GH_TOKEN": token, "GH_PAGER": "cat"},
        postprocess=partial(clean_job_logs, git_platform=GitPlatform.GITHUB) if is_run_log else None,
    )
    output = await run_cli(GITHUB_CLI, invocation, runtime, to_file=to_file, backend=backend)
    if state_update:
        tool_message = ToolMessage(content=output, tool_call_id=runtime.tool_call_id)
        return Command(update={**state_update, "messages": [tool_message]})
    return output
