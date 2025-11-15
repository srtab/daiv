from __future__ import annotations

import base64
import io
import json
import logging
import tarfile
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import httpx
from langchain.agents.middleware import AgentMiddleware, AgentState
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
    from langgraph.runtime import Runtime


logger = logging.getLogger("daiv.tools")

BASH_TOOL_NAME = "bash"
INSPECT_BASH_TOOL_NAME = "inspect_bash"
FORMAT_CODE_TOOL_NAME = "format_code"


BASH_TOOL_DESCRIPTION = f"""\
Execute a list of Bash commands in a persistent shell session rooted at the repository's root. Use this tool to apply the changes required by an execution plan (formatters, codegen, package manager ops). All writes must remain inside the repository.

## Purpose

 - Execute project-native CLIs that perform writes (e.g., formatters with --fix, code generators, package manager install/update/remove).
 - Do not use it for file I/O tasks that the companion tools handle directly.

## Session & Execution

 - Persistent shell session across tool calls; commands run in order; pipelines/compound commands allowed.
 - Start in repo root. Prefer absolute paths. Avoid `cd` unless the plan explicitly requires it.

## Output Contract

 - Success path: each command returns `exit_code` and raw output (stdout+stderr merged), truncated to {MAX_OUTPUT_LENGTH} lines.
 - Stop on first non-zero exit: execution halts; ONLY the failed command's `exit_code` and raw output are returned.

## Write Scope & Boundaries

 - Writes must stay strictly within the repository root; do not touch parent dirs, `$HOME`, or follow symlinks that exit the repo.
 - **No Git commands** (no add/commit/checkout/rebase/push).
 - Default to **non-interactive**: add flags/env to avoid prompts (`-y/--yes`, `CI=1`, `--no-progress`, etc.).
 - Avoid high-impact/system-level actions:
 - No system package managers or global installs (`apt-get`, `yum`, `brew`, etc.).
 - No Docker builds/pushes or container/image manipulation.
 - No unscoped destructive ops (e.g., `rm -rf` outside targeted paths).
 - No DB schema changes/migrations/seeds.
 - No editing secrets/credentials (e.g., `.env`) or CI settings.

## Preferred Companion Tools (use instead of shell equivalents)

 - Reads/search/listing: {GLOB_TOOL_NAME} (discovery), {GREP_TOOL_NAME} (content search), {LS_TOOL_NAME} (directory metadata), {READ_TOOL_NAME} (file contents).
 - Writes/FS changes: {WRITE_TOOL_NAME} (create file), {EDIT_TOOL_NAME} (replace old content with new), {DELETE_TOOL_NAME} (remove file), {RENAME_TOOL_NAME} (rename file).
 - Therefore, **avoid** shell substitutes like `touch`, `echo > file`, `sed -i`, `rm`, `mv`, `cp` when the companion tools can perform the same operation.
 - Fall back to Bash only when the companion tools cannot achieve the goal or when invoking project-native CLIs that must perform writes.

## When to Use

 - Apply formatter fixes (`eslint --fix`, `ruff --fix`, `black -w`);
 - Run code generators/scaffolding;
 - Manage dependencies via the project's package manager.

## When Not to Use

 - Any file creation/edit/rename/delete that the {WRITE_TOOL_NAME}/{EDIT_TOOL_NAME}/{RENAME_TOOL_NAME}/{DELETE_TOOL_NAME} tools can do.
 - Raw reads/search/listing that {GLOB_TOOL_NAME}/{GREP_TOOL_NAME}/{LS_TOOL_NAME}/{READ_TOOL_NAME} can handle.
 - Any operation outside the repository root or involving Git/system-level changes.

## Examples

  Good examples:
    - {BASH_TOOL_NAME}(commands=["pytest /foo/bar/tests"])
    - {BASH_TOOL_NAME}(commands=["python /path/to/script.py"])
    - {BASH_TOOL_NAME}(commands=["npm install", "npm test"])
    - {BASH_TOOL_NAME}(commands=["uv lock"])

  Bad examples (avoid these):
    - {BASH_TOOL_NAME}(commands=["cd /foo/bar", "pytest tests"])  # Use absolute path instead
    - {BASH_TOOL_NAME}(commands=["cat file.txt | head -10"])  # Use {READ_TOOL_NAME} tool instead
    - {BASH_TOOL_NAME}(commands=["find . -name '*.py'"])  # Use {GLOB_TOOL_NAME} tool instead
    - {BASH_TOOL_NAME}(commands=["grep -r 'pattern' ."])  # Use {GREP_TOOL_NAME} tool instead
    - {BASH_TOOL_NAME}(commands=["rm -rf /foo/bar"])  # Use {DELETE_TOOL_NAME} tool instead
    - {BASH_TOOL_NAME}(commands=["sed -i 's/old/new/g' file.txt"])  # Use {EDIT_TOOL_NAME} tool instead
    - {BASH_TOOL_NAME}(commands=["mv file.txt file2.txt"])  # Use {RENAME_TOOL_NAME} tool instead
    - {BASH_TOOL_NAME}(commands=["echo 'Hello, world!' > file.txt"])  # Use {WRITE_TOOL_NAME} tool instead
"""  # noqa: E501

INSPECT_BASH_TOOL_DESCRIPTION = f"""\
Execute commands in an ephemeral investigation sandbox to gather information for your plan.

## Understanding This Tool

**What it does:**
This tool runs commands in a disposable container where outputs are informational only. No changes persist to the actual repository - think of it as a "read-only" diagnostic environment.

**Your goal:**
Gather information to create an accurate plan. You're in the planning phase, not the execution phase.

## Mental Model

Commands run in a sandbox → Observe what exists/what would happen → Use findings to build your plan

**Key insight:** When you see "Fixed 2 errors", translate this as "2 errors exist that need fixing" and add the fix to your plan.

## Interpreting Outputs

| Command Output | What It Means | Your Action |
|----------------|---------------|-------------|
| "Fixed X errors" | X errors exist that can be fixed | Add fix command to plan |
| "Installed Y packages" | Y packages will need installation | Add install to plan |
| "Tests passed" | Tests currently pass | No action needed |
| "X errors found" | X errors exist | Investigate and plan fixes |

## Best Use Cases

**Use this tool for information gathering:**
 - Diagnostic commands (`ruff check`, `mypy`, `pytest --collect-only`)
 - Version checks (`tool --version`)
 - Dry runs (`npm run build --dry-run`)
 - Listing/inspection (`npm ls`, `pip list`)

**Use specialized tools instead for:**
 - File reading → Use {READ_TOOL_NAME} tool for better formatting
 - File searching → Use {GREP_TOOL_NAME}/{GLOB_TOOL_NAME} tools for efficiency
 - Directory listing → Use {LS_TOOL_NAME} tool for structured output

## Command Execution Details

 - Commands run from repository root
 - Output truncated to {MAX_OUTPUT_LENGTH} lines
 - Use `--no-color`, `--json`, `--quiet` flags to reduce noise when available
 - Execution stops on first non-zero exit code
 - Prefer absolute paths over `cd`

## Examples:
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
- **After making code changes** to ensure style compliance and minimize linting issues.
- **You can call this tool automatically** once you've modified files via {EDIT_TOOL_NAME}/{WRITE_TOOL_NAME}/{DELETE_TOOL_NAME}/{RENAME_TOOL_NAME} tools.
- **Tip:** The tool warns if no changes were made (use `force=True` to override).
- **Returns:** Success message or error details for troubleshooting.
"""  # noqa: E501


@tool(BASH_TOOL_NAME, description=BASH_TOOL_DESCRIPTION)
async def bash_tool(
    commands: Annotated[list[str], "The list of commands to execute."], runtime: ToolRuntime[RuntimeCtx]
) -> str:
    """
    Tool to run a list of Bash commands in a persistent shell session rooted at the repository's root.
    """  # noqa: E501
    logger.info("[%s] Running bash commands: %s", bash_tool.name, commands)

    repo_working_dir = Path(runtime.context.repo.working_dir)
    response = await _run_bash_commands(commands, repo_working_dir, runtime.state["session_id"])

    if response is None:
        return (
            "error: Failed to run commands. Verify that the commands are valid. "
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

    # Return only the last failed result, respecting the fail fast flag.
    if response.results[-1].exit_code != 0:
        return response.results[-1].model_dump_json()

    if response.patch:
        try:
            git_manager.apply_patch(response.patch)
        except Exception:
            logger.exception("[%s] Error applying patch to the repository.", format_code_tool.name)
            return "error: Failed to format code. The format code tool is not working properly."

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
        tar.add(repo_dir, arcname=repo_dir.name, filter=lambda info: None if info.name.startswith(".git") else info)

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

    name = "sandbox_middleware"

    state_schema = SandboxState

    def __init__(self, *, read_only_bash: bool = False, include_format_code: bool = False):
        """
        Initialize the middleware.
        """
        assert settings.SANDBOX_API_KEY is not None, "SANDBOX_API_KEY is not set"

        super().__init__()

        self.tools = []
        self.read_only_bash = read_only_bash

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
