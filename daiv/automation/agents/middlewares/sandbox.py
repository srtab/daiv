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
Executes a single shell command in a sandboxed environment (repository root).

This tool is primarily for terminal operations like running scripts, tests, builds, and other CLI workflows.

### Input
- command (required: string)
  - The shell command to execute from repository root.
  - Do not include `cd` (commands always run in repo root).
  - Prefer non-interactive flags (e.g., `-y`, `--yes`, `--no-progress`, `CI=1`) when applicable.
  - If you need multiple steps, combine commands with `&&` or `;`:
    - Use `&&` when later steps depend on earlier steps succeeding.
    - Use `;` only when you want later steps to run even if earlier ones fail.
  - Avoid newlines in the command string (newlines are okay only inside quoted strings).

### Prefer other tools for file operations (IMPORTANT)
Do NOT use this tool for:
- Reading files (avoid `cat`, `head`, `tail`) → use `read_file`
- Searching contents (avoid `grep`) → use `grep` tool
- Finding files (avoid `find`) → use `glob` tool
- Editing/writing/moving/deleting files → use `write_file` / `edit_file` / `rename` / `delete`

### Directory verification (only when creating new paths)
If your command will create new directories/files, first verify the parent directory exists using the dedicated `ls` tool.
Example:
- Before: `mkdir foo/bar`
- First: use `ls` (tool) to confirm `foo/` exists, then run the command.

### Quoting
Always quote paths that contain spaces:
- ✅ `python "path with spaces/script.py"`
- ✅ `pytest "tests/integration suite/"`
- ❌ `python path with spaces/script.py`

### Return value (Pydantic schema)
The tool returns a `RunCommandResult`:

- command: str
- output: str  (combined stdout/stderr). Output is truncated to MAX_OUTPUT_LENGTH lines.
- exit_code: int  (0 indicates success)

### Failure handling (recommended)
If exit_code != 0:
1. Read `output` for the error message and the failing step.
2. If the command chains multiple steps with `&&`, re-run only the failing step to isolate.
3. If a tool is prompting or hanging, re-run with non-interactive flags (`-y/--yes`, `CI=1`, etc.) or choose a non-interactive alternative.
4. If the issue looks like “file not found” / wrong path, verify paths using `ls` / `glob` tools and re-run with corrected relative paths.

### Write scope & boundaries
- Writes must stay strictly within the repository root; do not touch parent directories, `$HOME`, or follow symlinks that exit the repo.
- No Git operations available (no commit/checkout/rebase/push/etc.).
- Default to non-interactive commands; avoid prompts.
- Avoid high-impact/system-level actions.
- No system package managers or global installs (`apt-get`, `yum`, `brew`, etc.).
- No Docker builds/pushes or container/image manipulation.
- No unscoped destructive operations.
- No DB schema changes/migrations/seeds.
- Do not edit secrets/credentials (e.g., `.env`) or CI settings.

### Examples
Good:
- `{BASH_TOOL_NAME}(command="pytest tests/unit")`
- `{BASH_TOOL_NAME}(command="python tools/lint.py")`
- `{BASH_TOOL_NAME}(command="npm ci && npm test")`
- `{BASH_TOOL_NAME}(command="uv lock")`

Bad:
- `{BASH_TOOL_NAME}(command="cd src && pytest")`                    # never use `cd`
- `{BASH_TOOL_NAME}(command="cat file.txt | head -10")`             # use `read_file`
- `{BASH_TOOL_NAME}(command="find . -name '*.py'")`                 # use `glob`
- `{BASH_TOOL_NAME}(command="grep -r 'pattern' .")`                 # use `grep` tool
- `{BASH_TOOL_NAME}(command="rm -rf some/path")`                    # use `delete` tool
- `{BASH_TOOL_NAME}(command="sed -i 's/old/new/g' file.txt")`       # use `edit_file`
- `{BASH_TOOL_NAME}(command="mv file.txt file2.txt")`               # use `rename` tool
- `{BASH_TOOL_NAME}(command="echo 'Hello' > file.txt")`             # use `write_file`
"""  # noqa: E501

SANBOX_SYSTEM_PROMPT = f"""\
## Bash tool `{BASH_TOOL_NAME}`

You have access to a `{BASH_TOOL_NAME}` tool for running shell commands in a sandboxed environment.
Use it to run programs such as tests, builds, linters, formatters, and scripts.

### Working directory (CRITICAL)
- Every command starts in the **repository root directory**.
- **DO NOT use `cd`**.
- Use **relative paths from repo root** (e.g., `python scripts/run.py`, `pytest tests/unit`).

### When to use `{BASH_TOOL_NAME}`
- Running test suites (`pytest`, `npm test`, `go test`, etc.)
- Building projects (`make`, `npm run build`, `cargo build`, etc.)
- Running project scripts (`python tools/foo.py`, etc.)
- Running non-interactive CLI tasks required by the repo workflow

### When NOT to use `{BASH_TOOL_NAME}`
- Do not use it to read, search, or edit repository files. Use specialized tools instead:
  - `ls` (list directories)
  - `glob` (find files by pattern)
  - `grep` (search within files)
  - `read_file` (read file contents)
  - `write_file` / `edit_file` / `rename` / `delete` (modify filesystem)

### Examples
- ✅ `{BASH_TOOL_NAME}(command="make test")`
- ✅ `{BASH_TOOL_NAME}(command="python scripts/check.py")`
- ✅ `{BASH_TOOL_NAME}(command="npm ci && npm test")`
- ❌ `{BASH_TOOL_NAME}(command="cd subdir && pytest")` (never `cd`)
- ❌ `{BASH_TOOL_NAME}(command="cat README.md")` (use `read_file`)
- ❌ `{BASH_TOOL_NAME}(command="find . -name '*.py'")` (use `glob`)
- ❌ `{BASH_TOOL_NAME}(command="grep -R 'foo' .")` (use `grep` tool)
"""  # noqa: E501


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
        request = request.override(system_prompt=request.system_prompt + "\n\n" + SANBOX_SYSTEM_PROMPT)

        return await handler(request)
