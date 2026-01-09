from __future__ import annotations

import base64
import io
import json
import logging
import tarfile
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import httpx
from langchain.agents.middleware import AgentMiddleware, AgentState, ModelRequest, ModelResponse
from langchain.tools import ToolRuntime  # noqa: TC002
from langchain_core.tools import tool
from langgraph.typing import StateT  # noqa: TC002

from codebase.context import RuntimeCtx  # noqa: TC001
from codebase.utils import GitManager
from core.conf import settings
from core.sandbox.client import DAIVSandboxClient
from core.sandbox.schemas import RunCommandsRequest, RunCommandsResponse, StartSessionRequest

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langgraph.runtime import Runtime


logger = logging.getLogger("daiv.tools")

BASH_TOOL_NAME = "bash"

BASH_TOOL_DESCRIPTION = f"""\
Executes a bash command in a persistent shell session.

IMPORTANT: This tool is for terminal operations like tests, linters, formatters, npm, docker, git, etc. DO NOT use it for file operations (reading, writing, editing, searching, finding files) - use the specialized tools for this instead.

Before executing the command, please follow these steps:

1. Directory Verification:
   - If the command will create new directories or files, first use the ls tool to verify the parent directory exists and is the correct location
   - For example, before running "mkdir foo/bar", first use ls to check that "foo" exists and is the intended parent directory

2. Command Execution:
   - Always quote file paths that contain spaces with double quotes (e.g., cd "path with spaces/file.txt")
   - Examples of proper quoting:
     - cd "/Users/name/My Documents" (correct)
     - cd /Users/name/My Documents (incorrect - will fail)
     - python "/path/with spaces/script.py" (correct)
     - python /path/with spaces/script.py (incorrect - will fail)
   - After ensuring proper quoting, execute the command
   - Capture the output of the command

Usage notes:
  - The command parameter is required
  - Commands run in an isolated sandbox environment
  - Returns combined stdout/stderr output with exit code
  - If the output is very large, it may be truncated
  - Avoid using {BASH_TOOL_NAME} with the `find`, `grep`, `cat`, `head`, `tail`, `sed`, `awk`, or `echo` commands, unless explicitly instructed or when these commands are truly necessary for the task. Instead, always prefer using the dedicated tools for these commands:
    - File search: Use `glob` (NOT find or ls)
    - Content search: Use `grep` (NOT grep or rg)
    - Read files: Use `read_file` (NOT cat/head/tail)
    - Edit files: Use `edit_file` (NOT sed/awk)
    - Write files: Use `write_file` (NOT echo >/cat <<EOF)
    - Communication: Output text directly (NOT echo/printf)

  - When issuing multiple commands, use the ';' or '&&' operator to separate them. DO NOT use newlines (newlines are ok in quoted strings)
    - Use '&&' when commands depend on each other (e.g., "mkdir dir && cd dir")
    - Use ';' only when you need to run commands sequentially but don't care if earlier commands fail

  - Try to maintain your current working directory throughout the session by using absolute paths and avoiding usage of cd.
    - <good-example>pytest /foo/bar/tests</good-example>
    - <bad-example>cd /foo/bar && pytest tests</bad-example>

Examples:
    <good-example>
    {BASH_TOOL_NAME}(command="pytest /foo/bar/tests")
    {BASH_TOOL_NAME}(command="python /path/to/script.py")
    {BASH_TOOL_NAME}(command="npm install && npm test")
    </good-example>
    <bad-examples>
    {BASH_TOOL_NAME}(command="cd /foo/bar && pytest tests")  # Use absolute path instead
    {BASH_TOOL_NAME}(command="cat file.txt")  # Use read_file tool instead
    {BASH_TOOL_NAME}(command="find . -name '*.py'")  # Use glob tool instead
    {BASH_TOOL_NAME}(command="grep -r 'pattern' .")  # Use grep tool instead
    </bad-examples>

Write scope and boundaries:
  - Writes must stay strictly within the repository root; do not touch parent directories, `$HOME`, or follow symlinks that exit the repo.
  - VERY IMPORTANT: No write operations to Git allowed (no commit/checkout/rebase/push/etc.), only read operations are allowed.
  - Avoid high-impact/system-level actions.
  - No Docker builds/pushes or container/image manipulation.
  - No unscoped destructive operations.
  - No DB schema changes/migrations/seeds.
  - Do not edit secrets/credentials (e.g., `.env`) or CI settings.
"""  # noqa: E501

SANDBOX_SYSTEM_PROMPT = f"""\
## Bash tool `{BASH_TOOL_NAME}`

You have access to a `{BASH_TOOL_NAME}` tool for running shell commands in a persistent shell session.

Use this tool to run commands, scripts, tests, builds, and other shell operations."""


@tool(BASH_TOOL_NAME, description=BASH_TOOL_DESCRIPTION)
async def bash_tool(command: Annotated[str, "The command to execute."], runtime: ToolRuntime[RuntimeCtx]) -> str:
    """
    Tool to run a list of Bash commands in a persistent shell session rooted at the repository's root.
    """  # noqa: E501

    repo_working_dir = Path(runtime.context.repo.working_dir)
    response = await _run_bash_commands([command], repo_working_dir, runtime.state["session_id"])

    if response is None:
        return (
            "error: Failed to run command. Verify that the command is valid. "
            "If the commands are valid, maybe the bash tool is not working properly."
        )

    if response.patch:
        try:
            GitManager(runtime.context.repo).apply_patch(response.patch)
        except Exception:
            logger.exception("[%s] Error applying patch to the repository.", bash_tool.name)
            return "error: Failed to persist the changes. The bash tool is not working properly."

    return json.dumps([result.model_dump(mode="json") for result in response.results])


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

    def _exclude_git(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        """
        Filter tar members to exclude any `.git` paths.

        Args:
            info: The tar member info.

        Returns:
            The tar member info if it should be included, otherwise None.
        """
        # tar uses POSIX paths; Path(...).parts is safe here and keeps this readable.
        return None if ".git" in Path(info.name).parts else info

    with tarfile.open(fileobj=tar_archive, mode="w:gz") as tar:
        # Archive the repository *contents* (no top-level root folder), while excluding `.git` to avoid leaking
        # credentials/tokens and to reduce archive size.
        for child in repo_dir.iterdir():
            if child.name == ".git":
                continue
            tar.add(child, arcname=child.name, filter=_exclude_git)

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

    session_id: str
    """
    The sandbox session ID.
    """


class SandboxMiddleware(AgentMiddleware):
    """
    Middleware to start a sandbox session before running the commands.

    This middleware starts a sandbox session before agent starts the execution loop and
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

        self.tools = []
        self.close_session = close_session
        self.tools.append(bash_tool)

    async def abefore_agent(self, state: StateT, runtime: Runtime[RuntimeCtx]) -> dict[str, list] | None:
        """
        Start a sandbox session before the agent start the execution loop.

        Args:
            state (StateT): The state of the agent.
            runtime (Runtime[RuntimeCtx]): The runtime context.

        Returns:
            dict[str, list] | None: The state updates with the sandbox session.
        """
        if "session_id" in state and state["session_id"] is not None:
            return None

        session_id = await DAIVSandboxClient().start_session(
            StartSessionRequest(
                base_image=runtime.context.config.sandbox.base_image, extract_patch=True, persist_workdir=True
            )
        )
        return {"session_id": session_id}

    async def aafter_agent(self, state: StateT, runtime: Runtime[RuntimeCtx]) -> dict[str, list] | None:
        """
        Close the sandbox session after the agent finishes the execution loop.

        Args:
            state (StateT): The state of the agent.
            runtime (Runtime[RuntimeCtx]): The runtime context.

        Returns:
            dict[str, list] | None: The state updates with the closed sandbox session.
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
        request = request.override(
            system_prompt=request.system_prompt
            + "\n\n"
            + SANDBOX_SYSTEM_PROMPT.format(
                repo_working_dir="/archives" + Path(request.runtime.context.repo.working_dir).name
            )
        )

        return await handler(request)
