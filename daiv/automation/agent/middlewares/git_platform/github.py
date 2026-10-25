from __future__ import annotations

import re
from functools import partial
from typing import TYPE_CHECKING

from django.core.cache import cache
from django.utils import timezone

from langchain_core.messages import ToolMessage
from langgraph.types import Command

from accounts.credentials import platform_host
from automation.agent.middlewares.git_platform.cli import (
    AllowCommands,
    CliInvocation,
    PlatformCli,
    PolicyRefusal,
    check_subcommand,
    run_cli,
)
from automation.agent.middlewares.git_platform.cross_project import (
    REFUSAL_FOREIGN_REFERENCE,
    REFUSAL_FOREIGN_REFERENCE_UNAVAILABLE,
    CrossProjectOutcomes,
    decide_target,
    foreign_github_reference,
    mark_cross_project_body,
    record_refused_before_decision,
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

REFUSAL_SEARCH_SCOPE = (
    "error: '{qualifier}' widens the search beyond one repository: GitHub combines repo:, org: and user: qualifiers, "
    "and --owner, with the repository this tool sets, so other repositories would be searched too. Remove it, and set "
    "the `project` argument to search another repository where that argument is offered."
)
_SEARCH_SCOPE_QUALIFIER = re.compile(r"(?:^|[\s(])-?(?:repo|org|user):", re.IGNORECASE)


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


def _search_values(args: list[str]) -> list[str]:
    """The ``--search``/``-S`` values of an ``issue list`` or ``pr list``."""
    values = []
    for index, arg in enumerate(args):
        if arg in {"--search", "-S"} and index + 1 < len(args):
            values.append(args[index + 1])
        elif arg.startswith("--search="):
            values.append(arg.partition("=")[2])
        elif arg.startswith("-S") and len(arg) > 2:
            values.append(arg[2:].removeprefix("="))
    return values


def search_scope_widener(resource: str, action: str, args: list[str]) -> str | None:
    """The first ``repo:``, ``org:`` or ``user:`` qualifier, or ``--owner``, in a gh search.

    GitHub ORs these with the ``--repo`` the tool appends, so they reach repositories the call does not target.
    """
    if resource == "search":
        values = args
    elif resource in {"issue", "pr"} and action == "list":
        values = _search_values(args)
    else:
        return None
    for value in values:
        if value == "--owner" or value.startswith("--owner=") or _SEARCH_SCOPE_QUALIFIER.search(value):
            return value
    return None


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


def _cross_project_token_env(token: str) -> dict[str, str]:
    """Without GH_HOST, ``--repo owner/name`` means github.com; any other host reads GH_ENTERPRISE_TOKEN only."""
    host = platform_host(GitPlatform.GITHUB)
    token_name = "GH_TOKEN" if host.lower() == "github.com" else "GH_ENTERPRISE_TOKEN"  # noqa: S105
    return {"GH_HOST": host, token_name: token}


async def run_github_subcommand(
    subcommand: str,
    runtime: ToolRuntime[RuntimeCtx],
    to_file: bool,
    *,
    backend: BackendProtocol,
    project: str = "",
    cross_project_enabled: bool = False,
) -> str | Command:
    """Run a `gh` CLI subcommand on behalf of the ``gh`` tool.

    Returns a ``Command`` when a refreshed CLI token must be written back to state — never on the cross-project path,
    whose token belongs to a person and must not be checkpointed.

    ``project`` empty (or equal to the attached repository) is the unchanged path: the attached repository under the
    App installation token. Any other repository is executed with the acting person's own credential, and is refused
    rather than falling back.
    """
    args = check_subcommand(GITHUB_CLI, subcommand)
    if not isinstance(args, str) and (widener := search_scope_widener(args[0], args[1], args[2:])):
        args = PolicyRefusal(REFUSAL_SEARCH_SCOPE.format(qualifier=widener))
    if isinstance(args, PolicyRefusal):
        await record_refused_before_decision(runtime, provider=GitPlatform.GITHUB, project=project)
    if isinstance(args, str):
        return args

    resource, action, rest = args[0], args[1], args[2:]
    decision = await decide_target(
        resource,
        action,
        rest,
        project,
        runtime,
        provider=GitPlatform.GITHUB,
        cross_project_enabled=cross_project_enabled,
    )
    if decision.refusal is not None:
        # The refusal is the whole answer: retrying under the service identity is the leak this prevents.
        return decision.refusal

    target = decision.target or runtime.context.repository.slug
    if not decision.is_cross_project and (reference := foreign_github_reference(resource, action, rest, target=target)):
        refusal = REFUSAL_FOREIGN_REFERENCE if cross_project_enabled else REFUSAL_FOREIGN_REFERENCE_UNAVAILABLE
        return refusal.format(reference=reference, target=target)

    if decision.is_cross_project:
        # The installation token is DAIV's own and may live in checkpointed state; a person's token may not, so this
        # path mints none and produces no state update.
        state_update = None
        token_env = _cross_project_token_env(decision.token or "")
        args = mark_cross_project_body(args, provider=GitPlatform.GITHUB)
        outcomes = CrossProjectOutcomes(
            runtime=runtime, provider=GitPlatform.GITHUB, display_name=GITHUB_CLI.display_name, decision=decision
        )
    else:
        token, state_update = _get_cached_github_cli_token(runtime)
        token_env = {"GH_TOKEN": token}
        outcomes = None

    is_run_log = resource == "run" and action == "view" and "--log" in args
    invocation = CliInvocation(
        args=[*args, "--repo", target],
        env={"GIT_TERMINAL_PROMPT": "0", "NO_COLOR": "1", **token_env, "GH_PAGER": "cat"},
        postprocess=partial(clean_job_logs, git_platform=GitPlatform.GITHUB) if is_run_log else None,
        outcomes=outcomes,
    )
    output = await run_cli(GITHUB_CLI, invocation, runtime, to_file=to_file, backend=backend)
    if state_update:
        tool_message = ToolMessage(content=output, tool_call_id=runtime.tool_call_id)
        return Command(update={**state_update, "messages": [tool_message]})
    return output
