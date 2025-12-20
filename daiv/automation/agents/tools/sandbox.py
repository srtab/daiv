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
from codebase.repo_config import CONFIGURATION_FILE_NAME
from codebase.utils import GitManager
from core.conf import settings
from core.sandbox.client import DAIVSandboxClient
from core.sandbox.schemas import MAX_OUTPUT_LENGTH, RunCommandsRequest, RunCommandsResponse, StartSessionRequest

from .editing import DELETE_TOOL_NAME, EDIT_TOOL_NAME, RENAME_TOOL_NAME, WRITE_TOOL_NAME
from .navigation import GLOB_TOOL_NAME, GREP_TOOL_NAME, LS_TOOL_NAME, READ_TOOL_NAME

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langgraph.runtime import Runtime


logger = logging.getLogger("daiv.tools")

BASH_TOOL_NAME = "bash"
INSPECT_BASH_TOOL_NAME = "inspect_bash"
FORMAT_CODE_TOOL_NAME = "format_code"


BASH_TOOL_DESCRIPTION = f"""\
Executes a given command in the sandbox environment with proper handling and security measures.

Before executing the command, please follow these steps:

1. Directory Verification:
   - If the command will create new directories or files, first use the `ls` tool to verify the parent directory exists and is the correct location
   - For example, before running "mkdir foo/bar", first use `ls` to check that "foo" exists and is the intended parent directory

2. Command Execution:
   - Always quote file paths that contain spaces with double quotes (e.g., `cd "path with spaces/file.txt"`)
   - Examples of proper quoting:
     - `cd "/Users/name/My Documents"` (correct)
     - `cd /Users/name/My Documents` (incorrect - will fail)
     - `python "/path/with spaces/script.py"` (correct)
     - `python /path/with spaces/script.py` (incorrect - will fail)
   - After ensuring proper quoting, execute the command
   - Capture the output of the command

Usage notes:
  - The command parameter is required
  - Commands run in an isolated sandbox environment and are executed in the repository root
  - Returns combined stdout/stderr output with exit code
  - If the output is very large, it may be truncated
  - VERY IMPORTANT: You MUST avoid using search commands like find and grep. Instead use the grep, glob tools to search. You MUST avoid read tools like cat, head, tail, and use read_file to read files.
  - When issuing multiple commands, use the ';' or '&&' operator to separate them. DO NOT use newlines (newlines are ok in quoted strings)
    - Use '&&' when commands depend on each other (e.g., "mkdir dir && cd dir")
    - Use ';' only when you need to run commands sequentially but don't care if earlier commands fail
  - The sandbox working directory is always repository root. NEVER use cd commands. All file paths should be relative to the root directory.

**Write scope & boundaries:**
 - Writes must stay strictly within the repository root; do not touch parent dirs, `$HOME`, or follow symlinks that exit the repo.
 - **No Git commands** (no add/commit/checkout/rebase/push).
 - Default to **non-interactive**: add flags/env to avoid prompts (`-y/--yes`, `CI=1`, `--no-progress`, etc.).
 - Avoid high-impact/system-level actions.
 - No system package managers or global installs (`apt-get`, `yum`, `brew`, etc.).
 - No Docker builds/pushes or container/image manipulation.
 - No unscoped destructive ops (e.g., `rm -rf` outside targeted paths).
 - No DB schema changes/migrations/seeds.
 - No editing secrets/credentials (e.g., `.env`) or CI settings.

**Examples:**
  Good examples:
    - {BASH_TOOL_NAME}(command="pytest foo/bar/tests")
    - {BASH_TOOL_NAME}(command="python path/to/script.py")
    - {BASH_TOOL_NAME}(command="npm install && npm test")
    - {BASH_TOOL_NAME}(command="uv lock")

  Bad examples (avoid these):
    - {BASH_TOOL_NAME}(command="cd /foo/bar && pytest tests")  # Avoid using `cd` to change the working directory
    - {BASH_TOOL_NAME}(command="cat file.txt | head -10")  # Use {READ_TOOL_NAME} tool instead
    - {BASH_TOOL_NAME}(command="find . -name '*.py'")  # Use {GLOB_TOOL_NAME} tool instead
    - {BASH_TOOL_NAME}(command="grep -r 'pattern' .")  # Use {GREP_TOOL_NAME} tool instead
    - {BASH_TOOL_NAME}(command="rm -rf /foo/bar")  # Use {DELETE_TOOL_NAME} tool instead
    - {BASH_TOOL_NAME}(command="sed -i 's/old/new/g' file.txt")  # Use {EDIT_TOOL_NAME} tool instead
    - {BASH_TOOL_NAME}(command="mv file.txt file2.txt")  # Use {RENAME_TOOL_NAME} tool instead
    - {BASH_TOOL_NAME}(command="echo 'Hello, world!' > file.txt")  # Use {WRITE_TOOL_NAME} tool instead
"""  # noqa: E501

INSPECT_BASH_TOOL_DESCRIPTION = f"""\
Execute a list of given commands in an EPHEMERAL investigation sandbox to gather information. The sandbox is READ-ONLY, so no changes persist to the actual repository.

The commands are RECONNAISSANCE, not DEPLOYMENT:
- ✅ Run diagnostic commands to understand the current state
- ✅ Observe what WOULD happen if changes were made
- ✅ Collect this information to create plans
- ❌ NEVER claim you've made changes to the repository
- ❌ NEVER say modifications have been applied

<example>
User: "@daiv run lint-fix to fix linting errors"
Assistant: *Calls `{INSPECT_BASH_TOOL_NAME}` to execute the lint-fix command...*
Command output: "Fixed 2 errors"
<commentary>
The command output shows 2 errors CAN be fixed. Therefore, the assistant will need to include a fix command to the plan so that the plan execution will fix the errors.
</commentary>
</example>

<example>
User: "@daiv install new dependencies X"
Assistant: *Calls `{INSPECT_BASH_TOOL_NAME}` to execute the install command...*
Command output: "Installed 36 packages"
<commentary>
The command output shows 36 packages will need installation. Therefore, the assistant will need to include an install command to the plan so that the plan execution will install the dependencies.
</commentary>
</example>

<example>
User: "@daiv run tests"
Assistant: *Calls `{INSPECT_BASH_TOOL_NAME}` to execute the tests command...*
Command output: "Tests failed: 3"
Assistant: *Investigate the failures to plan fixes...*
<commentary>
The command output shows 3 tests failed. Therefore, the assistant will need to investigate the failures to plan fixes so that the plan executor will fix the failures.
The assistant will include the commands to execute the tests again to the plan executor to verify if the fixes worked.
</commentary>
</example>

**Usage notes:**
 - Commands are executed from repository root
 - Returns combined stdout/stderr output with exit code for each command
 - Execution will halt and return the output if the one of the commands fails with a non-zero exit code
 - Output is informational only and will be truncated to {MAX_OUTPUT_LENGTH} lines
 - Prefer using `--no-color`, `--json`, `--quiet` flags to reduce noise when available
 - Prefer relative paths and avoid usage of `cd`
 - **IMPORTANT: You MUST avoid using this tool for file reading (`cat`, `head`, `tail`), searching (`grep`, `find`), and listing (`ls`). Prefer using specialized tools instead:**
   - File reading → use `{READ_TOOL_NAME}` tool
   - File searching → use `{GREP_TOOL_NAME}` or `{GLOB_TOOL_NAME}` tools
   - Directory listing → use `{LS_TOOL_NAME}` tool

Examples:
  Good examples:
    - {INSPECT_BASH_TOOL_NAME}(commands=["pytest foo/bar/tests"])
    - {INSPECT_BASH_TOOL_NAME}(commands=["python path/to/script.py"])
    - {INSPECT_BASH_TOOL_NAME}(commands=["ruff check"])  # Check only, not --fix
    - {INSPECT_BASH_TOOL_NAME}(commands=["pytest --collect-only"])
    - {INSPECT_BASH_TOOL_NAME}(commands=["npm ls --depth=0"])

  Bad examples (avoid these):
    - {INSPECT_BASH_TOOL_NAME}(commands=["cd foo/bar", "pytest tests"])  # Use relative path instead
    - {INSPECT_BASH_TOOL_NAME}(commands=["cat file.txt"])  # Use {READ_TOOL_NAME} tool instead
    - {INSPECT_BASH_TOOL_NAME}(commands=["find . -name '*.py'"])  # Use {GLOB_TOOL_NAME} tool instead
    - {INSPECT_BASH_TOOL_NAME}(commands=["grep -r 'pattern' ."])  # Use {GREP_TOOL_NAME} tool instead
    - {INSPECT_BASH_TOOL_NAME}(commands=["ls -la foo/bar"])  # Use {LS_TOOL_NAME} tool instead
"""  # noqa: E501

FORMAT_CODE_TOOL_DESCRIPTION = f"""\
Applies code formatting and linting fixes using the repository's configured formatter in `{CONFIGURATION_FILE_NAME}` configuration file.

**When to use:**
- **After making code changes** to ensure style compliance and minimize linting issues
- **You can call this tool** once you've modified code files (e.g., via {EDIT_TOOL_NAME}/{WRITE_TOOL_NAME}/{DELETE_TOOL_NAME}/{RENAME_TOOL_NAME} tools)
- **When the plan includes code formatting and linting fixes** to ensure style compliance and minimize linting issues
- **Tip:** The tool warns if no code changes were made to the repository (use `force=True` to override)
- **Returns:** Success message or error details for troubleshooting
"""  # noqa: E501

SANBOX_SYSTEM_PROMPT = f"""\
## Bash tool `{BASH_TOOL_NAME}`

You have access to an `{BASH_TOOL_NAME}` tool for running shell commands in a sandboxed environment.
Use this tool to run commands, scripts, tests, builds, and other shell operations.

**CRITICAL: The sandbox always starts in the repository root directory. DO NOT use `cd` commands. All paths in your commands should be relative paths, or you can rely on the fact that you're already in the root directory.**

Examples of correct usage:
- ✅ `make test` (already in the repository root dir)
- ✅ `python scripts/test.py` (relative path)
- ❌ `cd / && make test` (WRONG - don't use cd)
- ❌ `cd /daiv && python script.py` (WRONG - use relative paths instead)

- `{BASH_TOOL_NAME}`: run a shell command in the sandbox (returns output and exit code)"""  # noqa: E501

SANDBOX_PLAN_SYSTEM_PROMPT = f"""\
## Shell Commands Guidance

Include standard, safe shell commands in your plan when they are explicitly mentioned by the user or clearly required for the task (e.g., "install X" → package manager command, "run tests" → test runner command). If a shell command could be destructive, flag it for confirmation in the plan.

**Package Management**

 - Using the project's native package manager for add/update/remove packages is the preferred approach; it will regenerate lockfiles automatically and keep the lockfiles up to date (never edit lockfiles by hand)
 - Detect the package manager from lockfiles/manifests (e.g., package.json, Makefile, pyproject.toml, etc.)
 - Skipping regression tests for basic package operations is fine unless the user asks otherwise

## Bash Tool

You have access to an `{INSPECT_BASH_TOOL_NAME}` tool for running shell commands in an ephemeral investigation sandbox to gather information for your plan.

Use this tool to run commands, scripts, tests, builds, and other shell operations. The commands are run in a disposable container where outputs are informational only. **No changes persist to the actual repository** - think of it as a "read-only" diagnostic environment.

**Remember:** `{INSPECT_BASH_TOOL_NAME}` tool is a TELESCOPE for observing the repository, not a WRENCH for fixing it. You gather intelligence to create plans, you don't execute changes.
"""  # noqa: E501

FORMAT_CODE_SYSTEM_PROMPT = f"""\
## Format Code Tool

You have access to a `{FORMAT_CODE_TOOL_NAME}` tool for applying code formatting and linting fixes to the repository to resolve style and linting issues. **Modifies files in-place.**
"""  # noqa: E501


@tool(BASH_TOOL_NAME, description=BASH_TOOL_DESCRIPTION)
async def bash_tool(command: Annotated[str, "The command to execute."], runtime: ToolRuntime[RuntimeCtx]) -> str:
    """
    Tool to run a list of Bash commands in a persistent shell session rooted at the repository's root.
    """  # noqa: E501
    logger.info("[%s] Running bash command: %s", bash_tool.name, command)

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


@tool(INSPECT_BASH_TOOL_NAME, description=INSPECT_BASH_TOOL_DESCRIPTION)
async def inspect_bash_tool(
    commands: Annotated[list[str], "The list of commands to execute."], runtime: ToolRuntime[RuntimeCtx]
) -> str:
    """
    Tool to execute commands in an EPHEMERAL investigation sandbox.
    """  # noqa: E501
    logger.info("[%s] Running read-only bash commands: %s", inspect_bash_tool.name, commands)

    repo_working_dir = Path(runtime.context.repo.working_dir)
    response = await _run_bash_commands(commands, repo_working_dir, runtime.state["session_id"])

    if response is None:
        return (
            "error: Failed to run commands. Verify that the commands are valid. "
            "If the commands are valid, maybe the bash tool is not working properly."
        )

    return json.dumps([result.model_dump(mode="json") for result in response.results])


@tool(FORMAT_CODE_TOOL_NAME, description=FORMAT_CODE_TOOL_DESCRIPTION)
async def format_code_tool(
    placeholder: Annotated[str, "Unused parameter (for compatibility). Leave empty."],
    runtime: ToolRuntime[RuntimeCtx],
    force: Annotated[bool, "Whether to force the formatting of the code."] = False,
) -> str:
    """
    Tool to apply code formatting and linting fixes to the repository to resolve style and linting issues.
    """  # noqa: E501
    logger.info("[%s] Formatting code (force: %s)", format_code_tool.name, force)

    git_manager = GitManager(runtime.context.repo)

    if not force and not git_manager.is_dirty():
        return "warning: No changes were made to any files, skipping formatting the code."

    repo_working_dir = Path(runtime.context.repo.working_dir)
    response = await _run_bash_commands(
        runtime.context.config.sandbox.format_code, repo_working_dir, runtime.state["session_id"]
    )
    if response is None:
        return "error: Failed to format code. The format code tool is not working properly."

    if response.patch:
        try:
            git_manager.apply_patch(response.patch)
        except Exception:
            logger.exception("[%s] Error applying patch to the repository.", format_code_tool.name)
            return "error: Failed to format code. The format code tool is not working properly."

    # Return only the last failed result, respecting the fail fast flag.
    if response.results[-1].exit_code != 0:
        return response.results[-1].model_dump_json()

    return "success: Code formatted."


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
        # Ignore .git directory to avoid including it in the archive and risking to include access tokens used
        # to clone the repository.
        tar.add(repo_dir, arcname=repo_dir.name, filter=lambda info: None if ".git" in info.name.split("/") else info)

    try:
        response = await DAIVSandboxClient().run_commands(
            session_id,
            RunCommandsRequest(
                commands=commands,
                workdir=repo_dir.name,
                archive=base64.b64encode(tar_archive.getvalue()).decode(),
                fail_fast=True,
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

    def __init__(
        self,
        *,
        read_only_bash: bool = False,
        include_format_code: bool = False,
        format_system_prompt: str = FORMAT_CODE_SYSTEM_PROMPT,
    ):
        """
        Initialize the middleware.
        """
        assert settings.SANDBOX_API_KEY is not None, "SANDBOX_API_KEY is not set"

        self.tools = []
        self.read_only_bash = read_only_bash
        self.include_format_code = include_format_code
        self.format_system_prompt = format_system_prompt

        if read_only_bash:
            self.tools.append(inspect_bash_tool)
        else:
            self.tools.append(bash_tool)

        if include_format_code:
            self.tools.append(format_code_tool)

    async def abefore_agent(self, state: StateT, runtime: Runtime[RuntimeCtx]) -> dict[str, list] | None:
        """
        Start a sandbox session before the agent start the execution loop.

        Args:
            state (StateT): The state of the agent.
            runtime (Runtime[RuntimeCtx]): The runtime context.

        Returns:
            dict[str, list] | None: The state updates with the sandbox session.
        """
        session_id = await DAIVSandboxClient().start_session(
            StartSessionRequest(
                base_image=runtime.context.config.sandbox.base_image,
                # Extract a patch with the changes made by the commands. Not needed for read-only bash.
                extract_patch=not self.read_only_bash,
                # Persist the workdir between commands if not read-only to avoid loosing the changes made by
                # the commands, like creating a folder in one iteration and creating/gen a file in that folder in
                # the next iteration.
                persist_workdir=not self.read_only_bash,
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
        await DAIVSandboxClient().close_session(state["session_id"])

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
        system_prompt = SANBOX_SYSTEM_PROMPT

        if self.read_only_bash:
            system_prompt = SANDBOX_PLAN_SYSTEM_PROMPT

        if self.include_format_code:
            system_prompt += "\n\n" + self.format_system_prompt

        request = request.override(system_prompt=request.system_prompt + "\n\n" + system_prompt)

        return await handler(request)
