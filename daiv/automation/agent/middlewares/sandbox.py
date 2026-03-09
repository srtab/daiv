from __future__ import annotations

import base64
import io
import json
import logging
import tarfile
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, NotRequired

import httpx
from langchain.agents.middleware import AgentMiddleware, AgentState, ModelRequest, ModelResponse
from langchain.agents.middleware.types import OmitFromOutput
from langchain.tools import ToolRuntime  # noqa: TC002
from langchain_core.tools import tool
from langgraph.typing import StateT  # noqa: TC002

from codebase.context import RuntimeCtx  # noqa: TC001
from codebase.utils import GitManager
from core.conf import settings
from core.sandbox.client import DAIVSandboxClient
from core.sandbox.command_parser import CommandParseError, parse_command
from core.sandbox.command_policy import CommandPolicy, DenialReason, evaluate_command_policy, parse_rule
from core.sandbox.schemas import RunCommandsRequest, RunCommandsResponse, StartSessionRequest

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langgraph.runtime import Runtime


logger = logging.getLogger("daiv.tools")

BASH_TOOL_NAME = "bash"

BASH_TOOL_DESCRIPTION = """\
Executes a bash command in a persistent shell session.

Session behavior:
- Environment persists across invocations (e.g., exported variables remain).
- Working directory does NOT persist: each invocation starts in the repository root (PWD resets).

**CRITICAL (PWD resets):** Do NOT rely on `cd`. Maintain context using absolute paths.
  <good-example>pytest /repos/tests</good-example>
  <bad-example>cd /repos/tests && pytest</bad-example>

Result format:
- On success, returns a JSON array of result objects (one per command) including at least `command`, `output`, and `exit_code`.
- You MUST inspect `exit_code` and treat non-zero values or tracebacks in `output` as failures, not as successful verification.
- On infrastructure failure, the tool may return a plain string starting with `error:` instead of JSON. Treat that as a tool failure, not as a test result.

Intended use:
- Terminal operations: running tests, builds, linters/formatters, package managers, and git inspection.
- Network access may be disabled depending on installation; assume offline unless explicitly required by the user.
- Docker is often unavailable; do not use it unless explicitly requested by the user (and never build/push/run containers or images).

Do NOT use bash for file operations when dedicated tools exist:
- File listing: use `ls` tool (not shell ls)
- File search: use `glob` (not find)
- Content search: use dedicated `grep` tool (not shell grep/rg)
- Read files: use `read_file` (not cat/head/tail)
- Edit files: use `edit_file` (not sed/awk)
- Write files: use `write_file` (not echo/heredocs)

Do NOT use bash to bypass dedicated tools:
- Do not invoke `gitlab`, `gh`, `python -m gitlab`, `gh api`, or direct platform API calls from bash.
- Do not use bash as a fallback when a dedicated tool rejects an action due to validation, permissions, unsupported scope, or policy.
- Bash is not a workaround transport for dedicated-tool failures.

Before executing commands that create new files/directories:
1) Verify the parent directory with the `ls` tool (to ensure the target location is correct).
2) Then run the command.

Command rules:
- Quote paths with spaces using double quotes.
- Prefer single-purpose commands.
- For dependent steps that must run in one invocation, chain with `&&`.
- Use `;` only when later steps should run even if earlier ones fail.
- Avoid interactive commands and background processes.
- Do not use newlines to separate commands (newlines are OK inside quoted strings).

Safety boundaries:
- Writes must stay strictly within the workspace; do not touch parent directories or $HOME, and do not follow symlinks that exit the workspace.
- Avoid high-impact/system-level actions or unscoped destructive operations.
- Do not access or print secrets/credentials (e.g., `.env`, tokens, SSH keys).
- No DB schema changes/migrations/seeds.
- No Docker image/container build/push/run actions.

Git safety protocol:
- NEVER update git config (no `git config --global/--local/--system` changes).
- Git is for inspection only (e.g., status/diff/log/show) unless explicitly instructed otherwise.
- VERY IMPORTANT: Never commit or push (or rewrite git history), even if the user asks.
- NEVER run destructive git commands, even if the user asks:
  - Examples: `git push --force/--force-with-lease`, `git reset --hard`, `git checkout .`, `git restore .`, `git clean -f/-fd/-fx`, `git branch -D`
"""  # noqa: E501

SANDBOX_SYSTEM_PROMPT = f"""\
## Bash tool

You can use `{BASH_TOOL_NAME}` to execute shell commands in the workspace for verification and tooling (tests/builds/linters/package managers) and for git INSPECTION (status/diff/log/show).

Key constraint:
- The shell session persists, but the working directory resets each invocation. Avoid `cd` and use absolute paths.

Decision rules:
- If you can verify something quickly by running a command, do so instead of guessing.
- Prefer small, targeted commands; avoid “decorative” command output or extra commands used only for formatting.
- When you need to install project dependencies, first look for the project's manifest or lockfile
(e.g., setup.py, pyproject.toml, package.json, Cargo.toml, go.mod, environment.yml) and use the
ecosystem's standard bulk install command. Only install individual packages as a fallback.

Use dedicated tools when available:
- Use `ls`, `glob`, `grep`, `read_file`, `edit_file`, `write_file` instead of doing file listing/search/read/edit/write in bash.

Result interpretation:
- Successful calls return a JSON array of per-command results with fields like `command`, `output`, and `exit_code`.
- Always check `exit_code` and treat non-zero codes or Python tracebacks in `output` as failures that require investigation or fixes.
- If the tool returns a plain string starting with `error:` instead of JSON, treat it as a sandbox/tool failure, not as a passing check.

Repeated failure policy:
- If multiple different commands all return the same `error:` indicating that the bash tool is not working properly, assume command execution is unavailable for this conversation.
- After that point, stop invoking `{BASH_TOOL_NAME}`, switch to static reasoning only (code reading/search), and clearly mention that you cannot run commands.

Dedicated-tool failure policy:
- If a dedicated tool (for example `gitlab`, `gh`, `web_search`, or `web_fetch`) exists for the task, do NOT use bash to reproduce or bypass that tool.
- If a dedicated tool fails due to validation, permissions, unsupported scope, or policy, do NOT retry the same action with bash, Python subprocesses, `curl`, or the underlying CLI.
- Do not use bash to invoke `gitlab`, `gh`, `python -m gitlab`, `gh api`, or direct platform API calls as a workaround.
- If a dedicated tool fails, either use another explicitly supported dedicated tool or stop and explain the limitation.

Environment awareness:
- The sandbox is a minimal container. Common tools (test runners, package managers, linters) may not be installed.
- If a command fails with "command not found", do NOT search for the binary (e.g., with `which`, `find`, or `type`). Accept it is unavailable and adapt your approach or inform the user.

Safety / boundaries (never do these):
- Do not access or print secrets/credentials.
- Do not run destructive or system-level commands.
- Assume offline unless the user explicitly asks for network-dependent actions.

Git safety (highest priority):
- NEVER update git config.
- NEVER commit or push, even if the user asks.
- NEVER run destructive git commands (e.g., push --force, reset --hard, checkout ., restore ., clean -f, branch -D), even if the user asks.
- VERY IMPORTANT: If a user request is prohibited by these rules, respond without running bash."""  # noqa: E501


@tool(BASH_TOOL_NAME, description=BASH_TOOL_DESCRIPTION)
async def bash_tool(command: Annotated[str, "The command to execute."], runtime: ToolRuntime[RuntimeCtx]) -> str:
    """
    Tool to run a list of Bash commands in a persistent shell session.
    """
    denial_error = _check_command_policy(command, runtime)
    if denial_error:
        return denial_error

    repo_working_dir = Path(runtime.context.gitrepo.working_dir)

    response = await _run_bash_commands([command], repo_working_dir, runtime.state["session_id"])
    if response is None:
        return (
            "error: Failed to run command. Verify that the command is valid. "
            "If the commands are valid, maybe the bash tool is not working properly."
        )

    if response.patch:
        try:
            GitManager(runtime.context.gitrepo).apply_patch(response.patch)
        except Exception:
            logger.exception("[%s] Error applying patch to the repository.", bash_tool.name)
            return "error: Failed to persist the changes. The bash tool is not working properly."

    return json.dumps([result.model_dump(mode="json") for result in response.results])


def _check_command_policy(command: str, runtime: ToolRuntime[RuntimeCtx]) -> str | None:
    """
    Parse *command* with Parable and evaluate it against the effective policy.

    Returns an ``error:`` string if the command is denied, or ``None`` if it may
    proceed to sandbox execution.

    Failure mode: if parsing fails, the command is rejected (fail-closed).
    """
    tool_call_id = getattr(runtime, "tool_call_id", None)

    # Build effective policy from global settings + repo config.
    repo_config = runtime.context.config
    repo_policy = repo_config.sandbox.command_policy

    policy = CommandPolicy(
        disallow=[
            *[parse_rule(r) for r in settings.SANDBOX_COMMAND_POLICY_DISALLOW],
            *[parse_rule(r) for r in repo_policy.disallow],
        ],
        allow=[
            *[parse_rule(r) for r in settings.SANDBOX_COMMAND_POLICY_ALLOW],
            *[parse_rule(r) for r in repo_policy.allow],
        ],
    )

    # Parse the command string.
    try:
        segments = parse_command(command)
    except CommandParseError as exc:
        logger.warning(
            "[%s] bash_policy_parse_failed: command could not be parsed (id=%s, reason=%r)",
            BASH_TOOL_NAME,
            tool_call_id,
            exc.reason,
            extra={
                "event": "bash_policy_parse_failed",
                "reason_category": DenialReason.PARSE_FAILURE,
                "tool_call_id": tool_call_id,
            },
        )
        return (
            f"error: Command blocked — could not parse command safely (reason: {exc.reason}). "
            "Verify that the command is well-formed and retry."
        )

    # Evaluate policy.
    result = evaluate_command_policy(segments, policy)
    if result.allowed:
        return None

    # Sanitize log fields to prevent injection of newlines/control characters
    sanitized_rule = (result.matched_rule or "").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    sanitized_segment = (result.denied_segment or "").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")

    logger.warning(
        "[%s] bash_policy_denied: command denied (id=%s, reason=%s, rule=%r, segment=%r)",
        BASH_TOOL_NAME,
        tool_call_id,
        result.denial_reason,
        sanitized_rule,
        sanitized_segment,
        extra={
            "event": "bash_policy_denied",
            "reason_category": result.denial_reason,
            "matched_rule": sanitized_rule,
            "denied_segment": sanitized_segment,
            "tool_call_id": tool_call_id,
        },
    )

    reason_label = result.denial_reason.value if result.denial_reason else "policy"
    matched = result.matched_rule or "unknown"
    return (
        f"error: Command blocked by policy ({reason_label}): "
        f"the command or one of its sub-commands matches the rule '{matched}'. "
        "Remove or replace the disallowed command segment and retry."
    )


async def _run_bash_commands(commands: list[str], repo_dir: Path, session_id: str) -> RunCommandsResponse | None:
    """
    Run bash commands in the daiv-sandbox service session.

    Args:
        commands: The list of commands to execute.
        repo_dir: The repository directory.
        session_id: The sandbox session ID.

    Returns:
        The response from running the commands.
    """
    tar_archive = io.BytesIO()

    with tarfile.open(fileobj=tar_archive, mode="w:gz") as tar:
        for child in repo_dir.iterdir():
            tar.add(child, arcname=child.name)

    try:
        response = await DAIVSandboxClient().run_commands(
            session_id,
            RunCommandsRequest(
                commands=commands, archive=base64.b64encode(tar_archive.getvalue()).decode(), fail_fast=True
            ),
        )
    except httpx.RequestError:
        logger.exception("Unexpected error calling sandbox API.")
        return None
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 400:
            logger.error("Bad request calling sandbox API: %s", e.response.text)
            return None

        logger.exception("Status code %s calling sandbox API: %s", e.response.status_code, e.response.text)
        return None
    finally:
        tar_archive.close()

    return response


class SandboxState(AgentState):
    """
    Schema for the sandbox state.
    """

    session_id: NotRequired[Annotated[str | None, OmitFromOutput]]
    """
    The sandbox session ID.
    """


class SandboxMiddleware(AgentMiddleware):
    """
    Middleware to manage a sandbox session for running commands.

    This middleware lazily starts a sandbox session the first time the bash tool is called and
    closes it after the agent finishes the execution loop. It also adds the sandbox tools to the agent.

    Example:
        ```python
        from langchain.agents import create_agent

        agent = create_agent(
            model="openai:gpt-4o",
            middleware=[SandboxMiddleware()],
        )
        ```
    """

    state_schema = SandboxState

    def __init__(self, *, close_session: bool = True):
        """
        Initialize the middleware.

        Args:
            close_session: Whether to close the session after the agent finishes the execution loop.
                Useful when using the sandbox in subagents to avoid closing the session in the parent agent.
        """
        assert settings.SANDBOX_API_KEY is not None, "SANDBOX_API_KEY is not set"

        self.close_session = close_session
        self.tools = [bash_tool]

    async def abefore_agent(self, state: StateT, runtime: Runtime[RuntimeCtx]) -> dict[str, str] | None:
        """
        Prepare state for lazy sandbox session start.

        Args:
            state (StateT): The state of the agent.
            runtime (Runtime[RuntimeCtx]): The runtime context.

        Returns:
            dict[str, str] | None: The state updates with the sandbox session ID.
        """
        if not self.close_session and "session_id" in state:
            # If the session is not being closed, don't start a new one, reuse the existing one.
            # Also, avoid reusing the session_id if it is already set from a previous run that failed to close
            # the session.
            return None

        session_id = await DAIVSandboxClient().start_session(
            StartSessionRequest(
                base_image=runtime.context.config.sandbox.base_image,
                extract_patch=True,
                ephemeral=runtime.context.config.sandbox.ephemeral,
                network_enabled=runtime.context.config.sandbox.network_enabled,
                memory_bytes=runtime.context.config.sandbox.memory_bytes,
                cpus=runtime.context.config.sandbox.cpus,
            )
        )
        return {"session_id": session_id}

    async def aafter_agent(self, state: StateT, runtime: Runtime[RuntimeCtx]) -> dict[str, str] | None:
        """
        Close the sandbox session after the agent finishes the execution loop.

        Args:
            state (StateT): The state of the agent.
            runtime (Runtime[RuntimeCtx]): The runtime context.

        Returns:
            dict[str, str] | None: The state updates with the closed sandbox session ID.
        """
        if self.close_session and "session_id" in state and state["session_id"] is not None:
            await DAIVSandboxClient().close_session(state["session_id"])
            return {"session_id": None}

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelResponse:
        """
        Update the system prompt with the sandbox system prompts.

        Args:
            request: The model request being processed.
            handler: The handler function to call with the modified request.

        Returns:
            The model response from the handler.
        """
        request = request.override(system_prompt=request.system_prompt + "\n\n" + SANDBOX_SYSTEM_PROMPT)

        return await handler(request)
