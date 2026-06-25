from __future__ import annotations

import asyncio
import io
import json
import logging
import tarfile
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, NotRequired

import httpx
from langchain.agents.middleware import AgentMiddleware, AgentState, ModelRequest, ModelResponse
from langchain.agents.middleware.types import OmitFromOutput
from langchain.tools import ToolRuntime  # noqa: TC002
from langchain_core.tools import BaseTool, tool
from langgraph.config import get_config
from langgraph.typing import StateT  # noqa: TC002

from automation.agent.conf import settings as agent_settings
from automation.agent.constants import BUILTIN_SKILLS_PATH
from automation.agent.middlewares.file_system import SandboxFileBackend  # noqa: TC001
from codebase.context import RuntimeCtx, SandboxRuntime  # noqa: TC001
from core.conf import settings
from core.sandbox.client import DAIVSandboxClient, is_transient_sandbox_error
from core.sandbox.command_parser import CommandParseError, parse_command
from core.sandbox.command_policy import CommandPolicy, DenialReason, evaluate_command_policy, parse_rule
from core.sandbox.schemas import RunCommandsResponse, StartSessionRequest
from core.site_settings import site_settings

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langgraph.runtime import Runtime


logger = logging.getLogger("daiv.tools")

BASH_TOOL_NAME = "bash"

BASH_TOOL_DESCRIPTION = """\
Executes a bash command in a persistent shell session.

Session behavior:
- Environment persists across invocations (e.g., exported variables remain).
- Working directory does NOT persist: each invocation starts in the repository root `/workspace/repo` (PWD resets). You may also read/write elsewhere under `/workspace` (e.g. the `/workspace/tmp` scratchpad) using absolute paths.

**CRITICAL (PWD resets):** Do NOT rely on `cd`. Maintain context using absolute paths.
  <good-example>pytest /workspace/repo/tests</good-example>
  <bad-example>cd /workspace/repo/tests && pytest</bad-example>

Result format:
- On success, returns a JSON object `{"commands": [...]}`.
  - `commands` is a list of per-command result objects including at least `command`, `output`, and `exit_code`.
- You MUST inspect each command's `exit_code` and treat non-zero values or tracebacks in `output` as failures, not as successful verification.
- On infrastructure failure, the tool may return a plain string starting with `error:` instead of JSON. Treat that as a tool failure, not as a test result.

Intended use:
- Terminal operations: running tests, builds, linters/formatters, package managers, and git inspection.
- Network access may be disabled depending on installation; assume offline unless explicitly required by the user.
- Docker is often unavailable; do not use it unless explicitly requested by the user (and never build/push/run containers or images).

Do NOT use bash for file operations when dedicated tools exist:
- File listing: use `ls` tool (not shell ls)
- File search: use `glob` (not find)
- Content search: use the dedicated `grep` tool — it takes a regex (not shell grep/rg)
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
- Successful calls return a JSON object with `commands` (per-command results: `command`, `output`, `exit_code`).
- Always check each `exit_code` and treat non-zero codes or Python tracebacks in `output` as failures that require investigation or fixes.
- If the tool returns a plain string starting with `error:` instead of JSON, treat it as a sandbox/tool failure, not as a passing check.

Infrastructure failure policy:
- The bash tool reports infrastructure problems (not command results) as a string starting with `error:`. Read it: some say the problem is temporary and invite a single retry of the same command; others say the tool is unavailable for the rest of the conversation. Follow the instruction the error gives you.
- Stop invoking `{BASH_TOOL_NAME}` once an error says the tool is unavailable, OR once the same `error:` string occurs on two consecutive commands. After that, switch to static reasoning only (code reading/search) and clearly mention that you cannot run commands.

Dedicated-tool failure policy:
- If a dedicated tool (for example `gitlab`, `gh`, `web_search`, or `web_fetch`) exists for the task, do NOT use bash to reproduce or bypass that tool.
- If a dedicated tool fails due to validation, permissions, unsupported scope, or policy, do NOT retry the same action with bash, Python subprocesses, `curl`, or the underlying CLI.
- Do not use bash to invoke `gitlab`, `gh`, `python -m gitlab`, `gh api`, or direct platform API calls as a workaround.
- If a dedicated tool fails, either use another explicitly supported dedicated tool or stop and explain the limitation.

Environment awareness:
- The sandbox is a minimal container. Common tools (test runners, package managers, linters) may not be installed.
- If a command fails with "command not found", do NOT search for the binary (e.g., with `which`, `find`, or `type`) or retry with a different package manager or runner (e.g., switching from `uv` to `pipenv` to raw `python`). Accept the tool is unavailable and verify your changes using other means (linting, type-checking, code review).
- If a command runs but fails due to missing infrastructure (database connection refused, environment variables, ModuleNotFoundError after dependency install), do not retry with a different approach. The sandbox lacks the required runtime environment — fall back to static verification instead.

Git repository layout:
- Only the source branch is checked out locally; target branches (e.g., `main`) exist as remote-tracking refs only. Use `origin/<branch>` for any target — `git diff origin/main...HEAD`, not `git diff main...HEAD`. The bare-name form fails with `fatal: ambiguous argument`.
- Run `git branch -a` if you need to confirm what refs are available.

Safety / boundaries (never do these):
- Do not access or print secrets/credentials.
- Do not run destructive or system-level commands.
- Assume offline unless the user explicitly asks for network-dependent actions.

Git safety (highest priority):
- NEVER update git config.
- NEVER commit or push, even if the user asks.
- NEVER run destructive git commands (e.g., push --force, reset --hard, checkout ., restore ., clean -f, branch -D), even if the user asks.
- VERY IMPORTANT: If a user request is prohibited by these rules, respond without running bash.

## Scratchpad (`/workspace/tmp`)
`/workspace/tmp` is an ephemeral per-run scratchpad shared between your file tools and bash. Use it for temporary scripts, generated data, fetched inputs, and intermediate step outputs. Files under `/workspace/tmp` are NEVER committed and are discarded when the run ends. Anything that must reach the merge/pull request must be written under the repository working directory (`/workspace/repo`) instead, never `/workspace/tmp`."""  # noqa: E501


def _check_command_policy(command: str, runtime: ToolRuntime[RuntimeCtx]) -> str | None:
    """
    Parse *command* with Parable and evaluate it against the effective policy.

    Returns an ``error:`` string if the command is denied, or ``None`` if it may
    proceed to sandbox execution.

    Failure mode: if parsing fails, the command is rejected (fail-closed).
    """
    tool_call_id = getattr(runtime, "tool_call_id", None)

    # Build effective policy from global settings + repo config.
    repo_policy = runtime.context.sandbox.command_policy

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
        else " This capability is intentionally unavailable — do not rephrase or try synonyms."
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


def _make_global_skills_archive() -> bytes | None:
    """Tar builtin + custom global skills (members relative to their skill dir).

    These are the only-disk sources for global skills; seeding them into the sandbox's
    ``/workspace/skills`` is the single provisioning step (one RPC), replacing per-file
    uploads. Custom skills are added after builtins so a same-named custom skill overrides.
    Returns ``None`` if there is nothing to pack.
    """
    roots: list[Path] = [BUILTIN_SKILLS_PATH]
    custom = agent_settings.CUSTOM_SKILLS_PATH
    if custom is not None and custom.is_dir():
        roots.append(custom)

    members: dict[str, Path] = {}
    for root in roots:
        if not root.is_dir():
            continue
        try:
            children = list(root.iterdir())
        except OSError:
            logger.warning("Could not read skills root '%s'; skipping for seed archive", root, exc_info=True)
            continue
        for child in children:
            if child.is_dir() and (child.name.startswith(".") or child.name == "__pycache__"):
                continue
            members[child.name] = child  # later root (custom) overrides builtin

    if not members:
        return None

    buf = io.BytesIO()
    try:
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for name, path in members.items():
                tf.add(path, arcname=name)
    except OSError, tarfile.TarError:
        logger.warning("Failed to build global skills archive; seeding without skills", exc_info=True)
        return None
    return buf.getvalue()


class SandboxEgressUnavailableError(RuntimeError):
    """Raised when a session's resolved egress policy cannot be provisioned because the sandbox has
    no egress proxy configured (HTTP 404 'Egress proxy not configured' — no shared egress CA).
    Fail-closed: an environment that requires a restricted egress policy must not run without that
    policy in force."""


class BashFailure(Enum):
    """Why a bash invocation produced no result, mapped to the guidance the agent gets.

    The agent only knows what the tool message and system prompt tell it — it has no view of
    transport details — so the failure is classified here into one of two actionable buckets:

    - ``TRANSIENT``: a momentary transport/server hiccup (no HTTP response, or a retryable status
      like a timeout/rate-limit/5xx). A retry may succeed.
    - ``PERMANENT``: the sandbox rejected the call in a way a retry will not fix (auth, session
      gone, malformed request). The bash tool is unusable for the rest of the run.
    """

    TRANSIENT = "transient"
    PERMANENT = "permanent"


# Agent-facing guidance for a bash call that returned no result. Both deliberately start with
# `error:` so the agent treats them as infrastructure failures, not command output (per the tool
# description and system prompt). The TRANSIENT text is kept byte-stable (no status codes, no
# per-call detail) so the system prompt's "two identical `error:` strings ⇒ stop" backstop still
# fires when a retry fails the same way.
_BASH_TRANSIENT_ERROR = (
    "error: The sandbox did not return a result for this command — a temporary problem reaching it "
    "interrupted the call, so the command may or may not have run. This is usually transient. "
    "You may retry this exact command ONCE. If the retry returns this same error, stop using the "
    "bash tool for the rest of this conversation and tell the user you were unable to run commands."
)

_BASH_PERMANENT_ERROR = (
    "error: The bash tool is unavailable for the rest of this conversation. The sandbox rejected "
    "the call in a way that will not recover by retrying, and no command was executed. Do NOT call "
    "the bash tool again — further calls will fail the same way."
)

_BASH_FAILURE_MESSAGES = {BashFailure.TRANSIENT: _BASH_TRANSIENT_ERROR, BashFailure.PERMANENT: _BASH_PERMANENT_ERROR}


async def _run_bash_commands(backend: SandboxFileBackend, commands: list[str]) -> RunCommandsResponse | BashFailure:
    """Run bash commands through the run's bound :class:`SandboxFileBackend`.

    On success returns the :class:`RunCommandsResponse`. Degrades only ``httpx`` transport/HTTP
    errors to a :class:`BashFailure` (classified transient vs. permanent) so the bash tool can hand
    the agent self-contained guidance — retry once vs. stop using the tool — instead of crashing the
    run. Other failures are intentionally NOT caught and propagate: a malformed or schema-mismatched
    200 body (``json.JSONDecodeError`` / ``pydantic.ValidationError``) and an unbound-backend
    ``RuntimeError`` are wire/programming bugs that should fail loud rather than masquerade as a soft
    sandbox failure.
    """
    try:
        return await backend.run_commands(commands, fail_fast=True)
    except httpx.RequestError:
        # No HTTP response at all (timeout, connection refused, network blip): the sandbox is
        # momentarily unreachable, so a retry may connect — transient.
        logger.exception("Transport error calling sandbox API; treating as transient.")
        return BashFailure.TRANSIENT
    except httpx.HTTPStatusError as e:
        logger.exception("Status code %s calling sandbox API: %s", e.response.status_code, e.response.text)
        return BashFailure.TRANSIENT if is_transient_sandbox_error(e) else BashFailure.PERMANENT


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
    Middleware to manage a sandbox session for running commands against the workspace.

    Manages the per-run sandbox *session* lifecycle (start + seed in ``abefore_agent``, stop/remove
    in ``aafter_agent``) and exposes:

    - The ``bash`` tool for running shell commands inside the sandbox.
    - Late-binding of the run's session onto the injected ``/workspace`` :class:`SandboxFileBackend`,
      so the agent's file tools (``read_file``/``write_file``/``edit_file``/``ls``/``glob``/``grep``)
      operate directly against the sandbox — it is the one true store, with no local mirror to keep
      in sync.

    The ``DAIVSandboxClient`` (transport) is **injected** — opened once per run by
    ``set_runtime_ctx`` and injected here by ``create_daiv_agent`` at graph-build time. The
    middleware borrows it; it never opens or closes it.

    Args:
        agent_root: Virtual path prefix the agent's filesystem tools see (e.g.
            ``/workspace/repo``); the agent's repo root.
        client: The run-scoped sandbox client opened by ``set_runtime_ctx`` and injected here.
        sandbox_backend: The concrete ``SandboxFileBackend`` the run's session is bound onto and
            that backs the ``bash`` tool. Subagents receive the *same* instance the parent agent
            uses (forwarded from ``create_daiv_agent``), so they share the parent-bound backend
            rather than binding their own.
        close_session: Whether to close the session after the agent finishes the execution
            loop. Set to ``False`` when used in subagents so the parent agent owns session
            lifecycle.

    Example:
        ```python
        from langchain.agents import create_agent

        agent = create_agent(
            model="openai:gpt-4o",
            middleware=[SandboxMiddleware(agent_root="/workspace/repo", client=client, sandbox_backend=backend)],
        )
        ```
    """

    state_schema = SandboxState

    def __init__(
        self,
        *,
        agent_root: str,
        client: DAIVSandboxClient | None = None,
        sandbox_backend: SandboxFileBackend | None = None,
        close_session: bool = True,
    ):
        if site_settings.sandbox_api_key is None:
            raise RuntimeError("Sandbox API key is not configured. Set DAIV_SANDBOX_API_KEY or use the config UI.")
        self._agent_root = agent_root
        self.close_session = close_session
        self._client = client
        self._sandbox_backend = sandbox_backend
        self.tools = [self._build_bash_tool()]

    def _build_bash_tool(self) -> BaseTool:
        """Build a bash tool that runs commands through this middleware's bound SandboxFileBackend."""

        @tool(BASH_TOOL_NAME, description=BASH_TOOL_DESCRIPTION)
        async def bash_tool(
            command: Annotated[str, "The command to execute."], runtime: ToolRuntime[RuntimeCtx]
        ) -> str:
            """Run a Bash command in the persistent shell session of this run."""
            denial_error = _check_command_policy(command, runtime)
            if denial_error:
                return denial_error

            if self._sandbox_backend is None:
                raise RuntimeError("SandboxMiddleware bash tool invoked before abefore_agent bound the sandbox backend")

            result = await _run_bash_commands(self._sandbox_backend, [command])
            if isinstance(result, BashFailure):
                return _BASH_FAILURE_MESSAGES[result]

            # The sandbox is authoritative: bash mutations already live in /workspace/repo,
            # so there is no local repo to keep in sync here.
            return json.dumps({"commands": [r.model_dump(mode="json") for r in result.results]})

        return bash_tool

    def _bind_session(self, session_id: str) -> None:
        """Bind the run's session onto the injected SandboxFileBackend (main agent only).

        Subagents receive the already-bound parent backend and short-circuit in ``abefore_agent``
        (``close_session=False`` + ``session_id`` already in state) before reaching this method, so
        they never re-bind. The ``is not None`` guard below stays defensive regardless.
        """
        if self._sandbox_backend is not None:
            self._sandbox_backend.bind_session(session_id)

    @staticmethod
    def _conversation_thread_id() -> str | None:
        """Read the conversation ``thread_id`` from the active run config.

        Agent-level middleware hooks receive a langgraph ``Runtime`` (which has no ``config``
        attribute), so the thread_id is read from the run config contextvar via ``get_config()``.
        Returns None when called outside a runnable context — the session is then force-removed
        (the behavior for non-chat automation runs).
        """
        try:
            config = get_config()
        except RuntimeError:
            # No runnable context (e.g. a non-chat automation run): disable reuse silently but
            # leave a breadcrumb so a future invocation path that unexpectedly lacks the context
            # — and thus never reuses sessions — is diagnosable rather than invisible.
            logger.debug("No runnable context; sandbox session reuse disabled for this run")
            return None
        return config.get("configurable", {}).get("thread_id")

    @staticmethod
    async def _session_exists(client: DAIVSandboxClient, session_id: str) -> bool:
        """Whether ``session_id`` still exists on the sandbox (restarting it if stopped).

        ``client.session_exists`` already maps a 404 (container genuinely gone) to ``False`` without
        raising, so reaching the ``except`` means a non-404 status or a transport error: we couldn't
        confirm the session, not that it's gone. We soft-fail to ``False`` so a flaky liveness check
        triggers a fresh create rather than failing the run — but the prior container may still be
        alive and is now abandoned (state["session_id"] gets overwritten), so log that it may have
        leaked and will be reclaimed by the sandbox reaper.
        """
        try:
            return await client.session_exists(session_id)
        except httpx.HTTPError:
            logger.exception(
                "Could not validate sandbox session %s for reuse; creating a fresh session. The prior "
                "container may have leaked and will be reclaimed by the sandbox reaper.",
                session_id,
            )
            return False

    @staticmethod
    async def _provision_egress(client: DAIVSandboxClient, session_id: str, sb: SandboxRuntime | None) -> None:
        """Provision the sidecar egress policy for a network-enabled session that requires it.

        No-op when there is no resolved sandbox runtime, the env defines no egress policy, or network
        is off. Only a **404** (egress proxy not configured on the sandbox — no shared egress CA,
        unambiguous and permanent) is converted to the actionable ``SandboxEgressUnavailableError``.
        On a fresh create this 404 is effectively unreachable: a network-enabled ``start_session`` is
        rejected up front (400) when the sandbox has no egress proxy, so the run aborts before reaching
        here. It survives for warm reuse — if the shared CA is rotated out between the session's start
        and a later turn's re-provision, ``configure_egress`` 404s and we still want the actionable
        diagnosis. Every other status propagates unchanged, including a 409 — on the egress endpoint 409
        is ambiguous ("Session is busy" transient lock contention, or "Session has no egress proxy") and
        the project already classifies 409 as transient (``TRANSIENT_SANDBOX_STATUS``), so it must not be
        mislabeled as a permanent "configure the proxy" diagnosis. A propagated error still fails closed:
        the caller's setup path aborts the run.
        """
        if sb is None or sb.egress is None or not sb.network_enabled:
            return
        try:
            await client.configure_egress(session_id, sb.egress)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise SandboxEgressUnavailableError(
                    "The resolved sandbox environment requires the egress proxy, but the sandbox returned "
                    "404 for egress provisioning. Configure the shared egress CA (EGRESS_CA_CERT_FILE + "
                    "EGRESS_CA_KEY_FILE) on the sandbox deployment to enable the egress proxy."
                ) from exc
            raise

    async def abefore_agent(self, state: StateT, runtime: Runtime[RuntimeCtx]) -> dict[str, str] | None:
        """
        Bind a sandbox session — reusing the prior turn's warm session when possible.

        The run-scoped client is supplied at construction (opened by set_runtime_ctx); this hook only
        manages the *session*. Warm reuse reads ``state["session_id"]`` — the checkpointer persists it
        per thread_id, so a prior turn's session is reachable directly from state (no separate store).
        We confirm it still exists on the sandbox (``session_exists`` also restarts a stopped
        container); a reaped/missing session falls through to a fresh create + seed.
        """
        client = self._client
        if client is None:
            raise RuntimeError("SandboxMiddleware requires an injected run-scoped sandbox client.")

        if not self.close_session and "session_id" in state:
            # Subagent path: the parent owns the session and already bound the shared backend (which
            # this subagent received and uses for its bash tool). Our injected client serves only the
            # non-None guard above. Nothing to set up.
            return None

        prior_session_id = state.get("session_id")
        if prior_session_id and await self._session_exists(client, prior_session_id):
            self._bind_session(prior_session_id)
            # Re-provision egress on every warm reuse so the fail-closed guarantee holds on resumed
            # turns too. A failure here propagates and aborts the run (fail-closed). Unlike the
            # fresh-create path below we deliberately do NOT force-close the container: it's a healthy
            # resumable session, the failure may be transient (e.g. a 409 "Session is busy"), and the
            # next turn re-runs this check (or the reaper reclaims it). Force-closing here would throw
            # away a good warm container on a transient blip.
            await self._provision_egress(client, prior_session_id, runtime.context.sandbox)
            logger.info("Reusing warm sandbox session %s", prior_session_id)
            return {"session_id": prior_session_id}

        sb = runtime.context.sandbox
        session_id = await client.start_session(
            StartSessionRequest(
                base_image=sb.base_image,
                network_enabled=sb.network_enabled,
                memory_bytes=sb.memory_bytes,
                cpus=sb.cpus,
                environment=sb.env_vars or None,
            )
        )
        try:
            working_dir = Path(runtime.context.gitrepo.working_dir)
            repo_archive, skills_archive = await asyncio.gather(
                asyncio.to_thread(_make_repo_archive, str(working_dir)), asyncio.to_thread(_make_global_skills_archive)
            )
            await client.seed_session(session_id, repo_archive=repo_archive, skills_archive=skills_archive)
            await self._provision_egress(client, session_id, sb)
        except Exception:
            logger.exception("Failed to build or seed sandbox session %s", session_id)
            try:
                await client.close_session(session_id, force=True)
            except Exception:
                logger.exception("Failed to close session %s after seed failure", session_id)
            raise
        self._bind_session(session_id)
        return {"session_id": session_id}

    async def aafter_agent(self, state: StateT, runtime: Runtime[RuntimeCtx]) -> dict[str, str] | None:
        """
        Stop the sandbox session after the agent finishes.

        The run-scoped transport is closed by set_runtime_ctx, not here. A resumable conversation
        (thread_id present) keeps its session warm (server *stop*) AND leaves ``session_id`` in state
        so the next turn reuses it. A one-shot run (no thread) force-removes the container and clears
        ``session_id`` from state.
        """
        client = self._client
        if client is not None and self.close_session and "session_id" in state and state["session_id"] is not None:
            session_id = state["session_id"]
            resumable = self._conversation_thread_id() is not None
            try:
                await client.close_session(session_id, force=not resumable)
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
            # Resumable: keep session_id in state (warm reuse next turn). One-shot: clear it.
            return None if resumable else {"session_id": None}
        return None

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
