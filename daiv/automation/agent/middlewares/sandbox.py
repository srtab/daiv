from __future__ import annotations

import asyncio
import io
import json
import logging
import tarfile
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, NotRequired, cast

import httpx
from deepagents.backends.protocol import FILE_NOT_FOUND
from deepagents.backends.utils import validate_path
from langchain.agents.middleware import AgentMiddleware, AgentState, ModelRequest, ModelResponse, ToolCallRequest
from langchain.agents.middleware.types import OmitFromOutput
from langchain.tools import ToolRuntime  # noqa: TC002
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, tool
from langgraph.typing import StateT  # noqa: TC002

from automation.agent.constants import SKILLS_CACHE_PATH
from automation.agent.middlewares.file_system import (
    EDIT_FILE_TOOL,
    EDIT_SUCCESS_PREFIX,
    WRITE_SUCCESS_PREFIX,
    WRITE_TOOL_NAMES,
    DAIVCompositeBackend,
    DAIVFilesystemBackend,
    SandboxSyncer,
    format_sync_error,
)
from codebase.context import RuntimeCtx  # noqa: TC001
from codebase.utils import GitManager, IgnoreCheck, files_changed_from_patch
from core.conf import settings
from core.sandbox.client import DAIVSandboxClient
from core.sandbox.command_parser import CommandParseError, parse_command
from core.sandbox.command_policy import CommandPolicy, DenialReason, evaluate_command_policy, parse_rule
from core.sandbox.schemas import RunCommandsRequest, RunCommandsResponse, StartSessionRequest
from core.site_settings import site_settings

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from typing import Any

    from deepagents.backends.protocol import BackendProtocol
    from langgraph.runtime import Runtime
    from langgraph.types import Command


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
- On success, returns a JSON object `{"commands": [...], "files_changed": [...]}`.
  - `commands` is a list of per-command result objects including at least `command`, `output`, and `exit_code`.
  - `files_changed` is a list of workspace modifications made by these commands (`{"path", "op"}` with op in `modified`/`added`/`deleted`/`renamed`; renames also carry `from_path`). Empty array when nothing changed.
- You MUST inspect each command's `exit_code` and treat non-zero values or tracebacks in `output` as failures, not as successful verification.
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
- When you need to install project dependencies, first look for the project's manifest or lockfile (e.g., setup.py, pyproject.toml, package.json, Cargo.toml, go.mod, environment.yml) and use the ecosystem's standard bulk install command. Only install individual packages as a fallback.

Use dedicated tools when available:
- Use `ls`, `glob`, `grep`, `read_file`, `edit_file`, `write_file` instead of doing file listing/search/read/edit/write in bash.

Result interpretation:
- Successful calls return a JSON object with `commands` (per-command results: `command`, `output`, `exit_code`) and `files_changed` (workspace mutations: path + op).
- Always check each `exit_code` and treat non-zero codes or Python tracebacks in `output` as failures that require investigation or fixes.
- If the tool returns a plain string starting with `error:` instead of JSON, treat it as a sandbox/tool failure, not as a passing check.

Repeated failure policy:
- If 2 consecutive commands return the same `error:` string indicating that the bash tool is not working properly, assume command execution is unavailable for this conversation. Do not attempt a third command.
- After that point, stop invoking `{BASH_TOOL_NAME}`, switch to static reasoning only (code reading/search), and clearly mention that you cannot run commands.

Dedicated-tool failure policy:
- If a dedicated tool (for example `gitlab`, `gh`, `web_search`, or `web_fetch`) exists for the task, do NOT use bash to reproduce or bypass that tool.
- If a dedicated tool fails due to validation, permissions, unsupported scope, or policy, do NOT retry the same action with bash, Python subprocesses, `curl`, or the underlying CLI.
- Do not use bash to invoke `gitlab`, `gh`, `python -m gitlab`, `gh api`, or direct platform API calls as a workaround.
- If a dedicated tool fails, either use another explicitly supported dedicated tool or stop and explain the limitation.

Environment awareness:
- The sandbox is a minimal container. Common tools (test runners, package managers, linters) may not be installed.
- If a command fails with "command not found", do NOT search for the binary (e.g., with `which`, `find`, or `type`) or retry with a different package manager or runner (e.g., switching from `uv` to `pipenv` to raw `python`). Accept the tool is unavailable and verify your changes using other means (linting, type-checking, code review).
- If a command runs but fails due to missing infrastructure (database connection refused, environment variables, ModuleNotFoundError after dependency install), do not retry with a different approach. The sandbox lacks the required runtime environment — fall back to static verification instead.

Safety / boundaries (never do these):
- Do not access or print secrets/credentials.
- Do not run destructive or system-level commands.
- Assume offline unless the user explicitly asks for network-dependent actions.

Git safety (highest priority):
- NEVER update git config.
- NEVER commit or push, even if the user asks.
- NEVER run destructive git commands (e.g., push --force, reset --hard, checkout ., restore ., clean -f, branch -D), even if the user asks.
- VERY IMPORTANT: If a user request is prohibited by these rules, respond without running bash."""  # noqa: E501


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
    hint = (
        " This capability is intentionally unavailable — do not rephrase or try synonyms. "
        "The Git middleware commits and pushes file changes automatically at turn-end "
        "(see the Git context section in the system prompt)."
        if matched.startswith("git ")
        else " This capability is intentionally unavailable — do not rephrase."
    )
    return (
        f"error: Command blocked by policy ({reason_label}): "
        f"the command or one of its sub-commands matches the rule '{matched}'.{hint}"
    )


def _make_repo_archive(working_dir: str) -> bytes:
    """Tar the contents of `working_dir` (members are relative to working_dir)."""
    repo_dir = Path(working_dir)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for child in repo_dir.iterdir():
            tf.add(child, arcname=child.name)
    return buf.getvalue()


def _make_skills_archive(skills_dir: Path) -> bytes | None:
    """Tar the contents of ``skills_dir`` (members relative to it). Return ``None`` if missing/empty."""
    if not skills_dir.is_dir():
        return None
    try:
        children = list(skills_dir.iterdir())
    except OSError:
        logger.warning(
            "Could not read skills directory '%s'; seeding without skills archive", skills_dir, exc_info=True
        )
        return None
    if not children:
        return None
    buf = io.BytesIO()
    try:
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for child in children:
                tf.add(child, arcname=child.name)
    except OSError, tarfile.TarError:
        logger.warning(
            "Failed to build skills archive from '%s'; seeding without skills archive", skills_dir, exc_info=True
        )
        return None
    return buf.getvalue()


def _agent_root_prefix(agent_root: str) -> str:
    return agent_root.rstrip("/") + "/"


def _resolve_repo_backend(backend: BackendProtocol, agent_root: str) -> BackendProtocol:
    """Return the backend that owns ``agent_root``.

    Composite-aware so callers that need to dispatch on backend shape (e.g. the
    gitignore guard, which calls ``DAIVFilesystemBackend._to_path``) see the
    underlying backend, not the ``DAIVCompositeBackend`` wrapper.
    """
    if isinstance(backend, DAIVCompositeBackend):
        return backend.resolve_backend_for(_agent_root_prefix(agent_root))
    return backend


def _refuse_write_outside_agent_root(request: ToolCallRequest, file_path: str, repo_prefix: str) -> ToolMessage:
    """Reject a write_file/edit_file whose target is not under ``agent_root``.

    Skills (under ``/skills/``) live on a shared host directory in this process; if the
    write succeeded, the sandbox-sync rollback would then ``backend.delete`` it,
    clobbering a skill file other concurrent agent runs depend on. Refuse up front.
    """
    return ToolMessage(
        content=(
            f"Refused: '{file_path}' is outside the working directory '{repo_prefix.rstrip('/')}/'. "
            f"Filesystem tools may only write under that prefix."
        ),
        tool_call_id=request.tool_call["id"],
        name=request.tool_call["name"],
        status="error",
    )


async def _read_backend_bytes(backend: BackendProtocol, virtual_path: str) -> bytes | None:
    """Return the file's bytes via ``adownload_files``, or ``None`` on absence/error.

    Logs transient/permission errors so a backend hiccup during ``_mirror_edit``'s
    pre-snapshot can be diagnosed — without a log the snapshot just silently turns into
    a defer-to-upstream, and a subsequent sandbox-sync failure has no rollback to fire.
    Missing-file responses (``FILE_NOT_FOUND``) stay silent: that's the expected branch
    when ``edit_file`` is invoked on a path upstream will reject anyway.
    """
    response = (await backend.adownload_files([virtual_path]))[0]
    if response.error == FILE_NOT_FOUND:
        return None
    if response.error is not None:
        logger.warning("backend read failed for %s: %s", virtual_path, response.error)
        return None
    return response.content


async def _run_bash_commands(
    client: DAIVSandboxClient, commands: list[str], session_id: str
) -> RunCommandsResponse | None:
    """Run bash commands in the existing sandbox session using the supplied long-lived client."""
    try:
        return await client.run_commands(session_id, RunCommandsRequest(commands=commands, fail_fast=True))
    except httpx.RequestError:
        logger.exception("Unexpected error calling sandbox API.")
        return None
    except httpx.HTTPStatusError as e:
        logger.exception("Status code %s calling sandbox API: %s", e.response.status_code, e.response.text)
        return None


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
    Middleware to manage a sandbox session for running commands and mirroring filesystem writes.

    Owns the per-run sandbox session lifecycle (start in ``abefore_agent``, close in
    ``aafter_agent``) and exposes:

    - The ``bash`` tool for running shell commands inside the sandbox.
    - A wrapper around upstream's ``write_file``/``edit_file`` tools that mirrors each
      successful local write to the sandbox so subsequent ``bash`` invocations see a
      coherent filesystem.

    A single ``DAIVSandboxClient`` is opened in ``abefore_agent`` and reused for both
    bash execution and write mirroring.

    Args:
        backend: Filesystem backend the agent's tools operate on. Must be the same instance
            that backs upstream's ``FilesystemMiddleware``.
        agent_root: Virtual path prefix the agent's filesystem tools see (e.g. ``/repo``).
            Used to validate inbound paths and map them to sandbox-side ``/repo/<rel>``.
        close_session: Whether to close the session after the agent finishes the execution
            loop. Set to ``False`` when used in subagents so the parent agent owns session
            lifecycle.

    Example:
        ```python
        from langchain.agents import create_agent

        agent = create_agent(
            model="openai:gpt-4o",
            middleware=[SandboxMiddleware(backend=backend, agent_root="/repo")],
        )
        ```
    """

    state_schema = SandboxState

    def __init__(self, *, backend: BackendProtocol, agent_root: str, close_session: bool = True):
        if site_settings.sandbox_api_key is None:
            raise RuntimeError("Sandbox API key is not configured. Set DAIV_SANDBOX_API_KEY or use the config UI.")

        self._backend = backend
        self._agent_root = agent_root
        self.close_session = close_session
        self._client: DAIVSandboxClient | None = None
        self._syncer: SandboxSyncer | None = None
        self.tools = [self._build_bash_tool()]

    def _build_bash_tool(self) -> BaseTool:
        """Build a bash tool bound to this middleware's per-run sandbox client."""

        @tool(BASH_TOOL_NAME, description=BASH_TOOL_DESCRIPTION)
        async def bash_tool(
            command: Annotated[str, "The command to execute."], runtime: ToolRuntime[RuntimeCtx]
        ) -> str:
            """Run a Bash command in the persistent shell session of this run."""
            denial_error = _check_command_policy(command, runtime)
            if denial_error:
                return denial_error

            if self._client is None:
                raise RuntimeError("SandboxMiddleware bash tool invoked before abefore_agent opened the sandbox client")

            response = await _run_bash_commands(self._client, [command], runtime.state["session_id"])
            if response is None:
                return (
                    "error: Sandbox call failed (transport or HTTP error — see server logs). "
                    "The bash tool may be unavailable for this run."
                )

            if response.patch:
                try:
                    GitManager(runtime.context.gitrepo).apply_patch(response.patch)
                except Exception:
                    logger.exception("[%s] Error applying patch to the repository.", BASH_TOOL_NAME)
                    return "error: Failed to persist the changes. The bash tool is not working properly."

            return json.dumps({
                "commands": [result.model_dump(mode="json") for result in response.results],
                "files_changed": files_changed_from_patch(response.patch),
            })

        return bash_tool

    async def abefore_agent(self, state: StateT, runtime: Runtime[RuntimeCtx]) -> dict[str, str] | None:
        """
        Open the per-run sandbox client and lazily start a session.

        Starts the session, seeds the workspace from the on-disk repo, and
        cleans up the session on seed failure to avoid container leaks. The
        client stays open for the rest of the agent run so every bash call
        and (for the file-system middleware) every file mirror reuses the
        same connection pool.

        Args:
            state (StateT): The state of the agent.
            runtime (Runtime[RuntimeCtx]): The runtime context.

        Returns:
            dict[str, str] | None: The state updates with the sandbox session ID.
        """
        client = DAIVSandboxClient()
        await client.open()
        self._client = client
        self._syncer = SandboxSyncer(backend=self._backend, agent_root=self._agent_root, client=client)

        try:
            if not self.close_session and "session_id" in state:
                # Subagent path: parent owns session lifecycle; we just keep our client open
                # so bash calls reuse the pool.
                return None

            session_id = await client.start_session(
                StartSessionRequest(
                    base_image=runtime.context.config.sandbox.base_image,
                    extract_patch=True,
                    network_enabled=runtime.context.config.sandbox.network_enabled,
                    memory_bytes=runtime.context.config.sandbox.memory_bytes,
                    cpus=runtime.context.config.sandbox.cpus,
                )
            )
            try:
                working_dir = Path(runtime.context.gitrepo.working_dir)
                repo_archive, skills_archive = await asyncio.gather(
                    asyncio.to_thread(_make_repo_archive, str(working_dir)),
                    asyncio.to_thread(_make_skills_archive, SKILLS_CACHE_PATH),
                )
                await client.seed_session(session_id, repo_archive=repo_archive, skills_archive=skills_archive)
            except Exception:
                # Log the cause keyed to ``session_id`` before unwinding — the outer
                # ``except BaseException`` re-raises bare, so without this breadcrumb a
                # build/seed failure shows up only as whatever LangGraph chooses to log.
                logger.exception("Failed to build or seed sandbox session %s", session_id)
                try:
                    await client.close_session(session_id)
                except Exception:
                    logger.exception("Failed to close session %s after seed failure", session_id)
                raise
        except BaseException:
            # Release the client we just opened; otherwise its httpx pool leaks for the run.
            await client.close()
            self._client = None
            self._syncer = None
            raise
        return {"session_id": session_id}

    async def aafter_agent(self, state: StateT, runtime: Runtime[RuntimeCtx]) -> dict[str, str] | None:
        """
        Close the sandbox session and the long-lived client after the agent finishes.

        ``aafter_agent`` only runs on a successful agent loop; on agent failure the
        client is not awaited closed and may leak its httpx pool until process
        teardown. Acceptable today because each run is short-lived; revisit if
        agent runs grow long enough that leaked pools matter.

        Args:
            state (StateT): The state of the agent.
            runtime (Runtime[RuntimeCtx]): The runtime context.

        Returns:
            dict[str, str] | None: The state updates with the closed sandbox session ID.
        """
        client = self._client
        try:
            if client is not None and self.close_session and "session_id" in state and state["session_id"] is not None:
                session_id = state["session_id"]
                try:
                    await client.close_session(session_id)
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code
                    if status in (404, 409):
                        logger.debug("Sandbox session %s already closed (status=%s)", session_id, status)
                    else:
                        logger.exception(
                            "Sandbox session %s close returned status=%s; container may have leaked", session_id, status
                        )
                except httpx.RequestError:
                    logger.exception("Sandbox session %s close request failed; container may have leaked", session_id)
                return {"session_id": None}
            return None
        finally:
            if client is not None:
                # Wrap teardown so a transport-level failure can't mask whatever close_session
                # (or another caller) was already raising.
                try:
                    await client.close()
                except Exception:
                    logger.exception("Failed to close sandbox httpx client; pool may have leaked")
                self._client = None
                self._syncer = None

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelResponse:
        """
        Append the sandbox system prompt to the request.

        Args:
            request: The model request being processed.
            handler: The handler function to call with the modified request.

        Returns:
            The model response from the handler.
        """
        return await handler(request.override(system_prompt=request.system_prompt + "\n\n" + SANDBOX_SYSTEM_PROMPT))

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]]
    ) -> ToolMessage | Command[Any]:
        """
        Mirror successful local ``write_file``/``edit_file`` results to the sandbox session.

        Tool dispatch happens through ``ToolNode.tools_by_name``, which is built once at
        agent-creation time. Wrapping the tool object in ``awrap_model_call`` only changes
        what is bound to the model — the dispatched tool is still upstream's. ``awrap_tool_call``
        intercepts the actual dispatch, so the mirror runs whenever the model calls the tool.

        Args:
            request: The tool call request.
            handler: The handler executing the tool.

        Returns:
            The tool result, or a replacement ``ToolMessage`` describing a sync error.
        """
        if request.tool is None or request.tool.name not in WRITE_TOOL_NAMES or self._syncer is None:
            return await handler(request)

        if request.tool.name == EDIT_FILE_TOOL:
            return await self._mirror_edit(request, handler)
        return await self._mirror_write(request, handler)

    async def _mirror_write(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]]
    ) -> ToolMessage | Command[Any]:
        """Dispatch ``write_file`` and mirror the new file to the sandbox under the syncer lock.

        The lock serialises with concurrent writes/edits in the same run so a rollback
        cannot race with a sibling tool's edit on the same path.

        Rollback calls ``backend.delete`` (subclass method) to undo a just-created file
        when the sync fails. ``stat_mode`` (subclass method) reads the post-write mode
        bits so ``PutMutation.mode`` mirrors ``+x`` for executable scripts on the
        sandbox side.
        """
        assert self._syncer is not None  # guarded by awrap_tool_call
        syncer = self._syncer
        args = request.tool_call["args"]
        file_path = args["file_path"]
        content = args["content"]
        ctx = cast("RuntimeCtx", request.runtime.context)
        backend = self._backend

        try:
            virtual_path = validate_path(file_path)
        except ValueError:
            return await handler(request)

        repo_prefix = _agent_root_prefix(self._agent_root)
        if not virtual_path.startswith(repo_prefix):
            return _refuse_write_outside_agent_root(request, file_path, repo_prefix)

        # `git add -A` silently drops gitignored paths, so a successful write would
        # never reach the MR. Fail-closed: any unexpected error from path resolution
        # or `git check-ignore` (surfaced as ``IgnoreCheck.UNKNOWN``) refuses the
        # write, since the alternative is silent data loss.
        repo_backend = _resolve_repo_backend(backend, self._agent_root)
        if isinstance(repo_backend, DAIVFilesystemBackend):
            disk_target = repo_backend._to_path(virtual_path)
            git_manager = GitManager(ctx.gitrepo)
            ignore_result = await asyncio.to_thread(git_manager.is_path_ignored, disk_target)
            if ignore_result is IgnoreCheck.IGNORED:
                return ToolMessage(
                    content=(
                        f"Refused: '{file_path}' matches a `.gitignore` rule — `git add -A` "
                        f"would silently drop it from the commit, so the change would not "
                        f"appear in the merge request. Pick a path that is not ignored, or "
                        f"run `git check-ignore -v <path>` via bash to find the matching rule "
                        f"(it may live in a parent `.gitignore`, `.git/info/exclude`, or the "
                        f"user's global ignore file) before changing it."
                    ),
                    tool_call_id=request.tool_call["id"],
                    name=request.tool_call["name"],
                    status="error",
                )
            if ignore_result is IgnoreCheck.UNKNOWN:
                return ToolMessage(
                    content=(
                        f"Refused: could not determine whether '{file_path}' is gitignored "
                        f"(`git check-ignore` failed; see server logs). Retry, or run "
                        f"`git check-ignore -v <path>` via bash to inspect."
                    ),
                    tool_call_id=request.tool_call["id"],
                    name=request.tool_call["name"],
                    status="error",
                )

        async with syncer.lock:
            result = await handler(request)
            if not _is_text_success(result, WRITE_SUCCESS_PREFIX):
                return result

            async def _rollback() -> bool:
                return await cast("Any", backend).delete(virtual_path)

            mode = await cast("Any", backend).stat_mode(virtual_path)

            error = await syncer.mirror(
                runtime=request.runtime,
                virtual_path=virtual_path,
                content_bytes=content.encode(),
                mode=mode,
                rollback=_rollback,
            )
            return _replace_tool_message(result, error) if error else result

    async def _mirror_edit(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]]
    ) -> ToolMessage | Command[Any]:
        """Dispatch ``edit_file`` and mirror the post-edit file under the syncer lock.

        Snapshots pre-edit bytes + mode outside the lock so rollback can restore them on
        sync failure. The lock then serialises dispatch through mirror so a sibling write
        cannot interleave with our restore.

        Reads use ``backend.adownload_files`` (protocol); rollback uses
        ``backend.aupload_files`` (protocol). ``stat_mode`` (subclass) provides the
        outgoing mutation's mode bits so ``+x`` mirrors to the sandbox; the disk
        write itself preserves mode automatically via POSIX ``O_TRUNC`` overwrite.
        """
        assert self._syncer is not None  # guarded by awrap_tool_call
        syncer = self._syncer
        backend = self._backend
        args = request.tool_call["args"]
        file_path = args["file_path"]

        try:
            virtual_path = validate_path(file_path)
        except ValueError:
            return await handler(request)

        repo_prefix = _agent_root_prefix(self._agent_root)
        if not virtual_path.startswith(repo_prefix):
            return _refuse_write_outside_agent_root(request, file_path, repo_prefix)

        # Snapshot must happen before dispatch so we can rebuild the file on sync failure.
        # If anything fails here (invalid path, missing file), let upstream produce the
        # canonical "not found" error instead of inventing our own.
        pre_bytes = await _read_backend_bytes(backend, virtual_path)
        if pre_bytes is None:
            return await handler(request)
        pre_mode = await cast("Any", backend).stat_mode(virtual_path)

        async with syncer.lock:
            result = await handler(request)
            if not _is_text_success(result, EDIT_SUCCESS_PREFIX):
                return result

            async def _rollback() -> bool:
                try:
                    responses = await backend.aupload_files([(virtual_path, pre_bytes)])
                except Exception:
                    logger.exception("rollback aupload_files failed for %s", virtual_path)
                    return False
                if responses[0].error is not None:
                    logger.error("rollback aupload returned error for %s: %s", virtual_path, responses[0].error)
                    return False
                return True

            post_bytes = await _read_backend_bytes(backend, virtual_path)
            if post_bytes is None:
                return _replace_tool_message(
                    result,
                    format_sync_error(
                        "failed to prepare sandbox sync: post-edit read returned no content",
                        rollback_ok=await _rollback(),
                    ),
                )

            error = await syncer.mirror(
                runtime=request.runtime,
                virtual_path=virtual_path,
                content_bytes=post_bytes,
                mode=pre_mode,
                rollback=_rollback,
            )
            return _replace_tool_message(result, error) if error else result


def _is_text_success(result: ToolMessage | Command[Any], prefix: str) -> bool:
    """True iff ``result`` is a non-error ToolMessage whose text content starts with ``prefix``."""
    if not isinstance(result, ToolMessage) or result.status == "error":
        return False
    return isinstance(result.content, str) and result.content.startswith(prefix)


def _replace_tool_message(original: ToolMessage | Command[Any], error_text: str) -> ToolMessage:
    """Return a new ``ToolMessage`` carrying ``error_text`` keyed to the same tool call.

    Caller must have verified ``original`` is a successful ``ToolMessage``; only used after
    upstream dispatch succeeded but the post-dispatch sync step failed.
    """
    assert isinstance(original, ToolMessage)
    return ToolMessage(content=error_text, tool_call_id=original.tool_call_id, name=original.name, status="error")
