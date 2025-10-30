from __future__ import annotations

import base64
import io
import json
import logging
import subprocess  # noqa: S404
import tarfile
from typing import TYPE_CHECKING

import httpx
from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.tools import ToolRuntime  # noqa: TC002
from langchain_core.tools import tool
from langgraph.typing import StateT  # noqa: TC002
from unidiff import PatchSet

from automation.utils import has_file_changes, register_file_change
from codebase.base import FileChangeAction
from codebase.context import RuntimeCtx  # noqa: TC001
from core.conf import settings
from core.sandbox.client import DAIVSandboxClient
from core.sandbox.schemas import RunCommandsRequest, RunCommandsResponse, StartSessionRequest

if TYPE_CHECKING:
    from pathlib import Path

    from langgraph.runtime import Runtime
    from langgraph.store.base import BaseStore


logger = logging.getLogger("daiv.tools")

BASH_TOOL_NAME = "bash"
FORMAT_CODE_TOOL_NAME = "format_code"

NUM_LEADING_SLASH = 1


async def _update_store_and_ctx(patch: str, store: BaseStore, repository_root_dir: Path):
    """
    Update the store and the repository root directory with the changes from the patch.

    Args:
        patch (str): The patch with the file changes.
        store (BaseStore): The store to save the file changes to.
        repository_root_dir (Path): The root directory of the repository.
    """
    patch_set = PatchSet.from_string(patch)
    file_changes = []

    # We need to populate the file changes before applying the patch to ensure that we have the old file content.
    for patched_file in patch_set:
        source_path = patched_file.source_file.split("/", NUM_LEADING_SLASH)[-1]
        target_path = patched_file.target_file.split("/", NUM_LEADING_SLASH)[-1]

        fs_file_path = repository_root_dir / patched_file.path

        if patched_file.is_added_file:
            file_changes.append({
                "action": FileChangeAction.CREATE,
                "new_file_content": None,
                "new_file_path": target_path,
            })
        elif patched_file.is_removed_file:
            file_changes.append({
                "action": FileChangeAction.DELETE,
                "old_file_content": fs_file_path.read_text(),
                "old_file_path": source_path,
                "new_file_content": "",
            })
        elif patched_file.is_modified_file:
            file_changes.append({
                "action": FileChangeAction.UPDATE,
                "old_file_content": fs_file_path.read_text(),
                "old_file_path": source_path,
                "new_file_content": None,
            })
        elif patched_file.is_rename:
            file_changes.append({
                "action": FileChangeAction.MOVE,
                "old_file_content": fs_file_path.read_text(),
                "old_file_path": source_path,
                "new_file_content": None,
                "new_file_path": target_path,
            })
        else:
            continue

    # Apply the patch to the repository root directory.
    apply_result = subprocess.run(  # noqa: S603 No risk of command injection
        ["/usr/bin/git", "apply", "--reject", "--whitespace=nowarn"],
        input=patch,
        cwd=repository_root_dir,
        capture_output=True,
        text=True,
        check=False,
    )

    if apply_result.returncode != 0:
        raise RuntimeError(f"git apply failed: {apply_result.stdout}")

    # Register the file changes to the store .
    for file_change in file_changes:
        if file_change["action"] == FileChangeAction.UPDATE:
            file_change["new_file_content"] = (repository_root_dir / file_change["old_file_path"]).read_text()
        elif file_change["action"] in [FileChangeAction.CREATE, FileChangeAction.MOVE]:
            file_change["new_file_content"] = (repository_root_dir / file_change["new_file_path"]).read_text()

        await register_file_change(store, **file_change)


@tool(BASH_TOOL_NAME, parse_docstring=True)
async def bash_tool(commands: list[str], runtime: ToolRuntime[RuntimeCtx]) -> str:
    """
    Executes a given list of bash commands in a persistent shell session relative to the repository root directory, ensuring proper handling and security measures.

    **Usage rules:**
     - The commands are executed in the order they are provided.
     - When a command fails, the session is terminated and the results are returned.
     - Results are returned with output (truncated to 10000 characters) and exit code for each successful command.
     - Try to maintain your current working directory throughout the session by using absolute paths and avoiding usage of `cd`. You may use `cd` if the plan explicitly requests it. `pytest /foo/bar/tests cd /foo/bar && pytest tests`.
     - IMPORTANT: Only execute commands that are directly requested.
     - VERY IMPORTANT: You MUST avoid using search commands like `find` and `grep`. Instead use `grep` or `glob` to search. You MUST avoid read tools like `cat`, `head`, `tail`, and `ls`, and use `read` and `ls` to read files.

    **Common operations:**
     - When the plan includes package management operations:
         - **ALWAYS** use the project's package manager native commands to add / update / remove packages to ensure the lock file (if present) is regenerated automatically. Do **NOT** edit lock files by hand.

    Args:
        commands: The list of commands to execute.

    Returns:
        str: The output of the commands.
    """  # noqa: E501
    logger.info("[%s] Running bash commands: %s", bash_tool.name, commands)

    response = await _run_bash_commands(commands, runtime.context.repo_dir, runtime.state["session_id"])

    if response is None:
        return (
            "error: Failed to run commands. Verify that the commands are valid. "
            "If the commands are valid, maybe the bash tool is not working properly."
        ), None

    if response.patch:
        try:
            await _update_store_and_ctx(response.patch, runtime.store, runtime.context.repo_dir)
        except Exception:
            logger.exception("Error updating store and ctx.")
            return None

    return json.dumps([result.model_dump(mode="json") for result in response.results])


@tool(BASH_TOOL_NAME, parse_docstring=True)
async def read_only_bash_tool(commands: list[str], runtime: ToolRuntime[RuntimeCtx]) -> str:
    """
    Executes a given list of bash commands in a persistent shell session relative to the repository root directory without persisting any changes to the codebase.

    **Usage rules:**
     - The commands are executed in the order they are provided.
     - When a command fails, the session is terminated and the results are returned.
     - Results are returned with output (truncated to 10000 characters) and exit code for each successful command.
     - Try to maintain your current working directory throughout the session by using absolute paths and avoiding usage of `cd`. You may use `cd` if the plan explicitly requests it.
     - IMPORTANT: Only execute commands that are directly requested or are necessary to complete your task.
     - VERY IMPORTANT: This tool is READ-ONLY. Any changes made by the commands will NOT be persisted to the codebase. Use this tool for reading information, testing, or inspecting the codebase without making permanent changes.
     - VERY IMPORTANT: You MUST avoid using search commands like `find` and `grep`. Instead use `grep` or `glob` to search. You MUST avoid read tools like `cat`, `head`, `tail`, and `ls`, and use `read` and `ls` to read files.

    **Common operations:**
     - Use this tool when you need to run commands for inspection (e.g. generate translations and catch missing translations, linting tests without auto-fix and catch linting errors, etc.), testing, etc.

    Args:
        commands: The list of commands to execute.

    Returns:
        str: The output of the commands.
    """  # noqa: E501
    logger.info("[%s] Running read-only bash commands: %s", read_only_bash_tool.name, commands)

    response = await _run_bash_commands(
        commands, runtime.context.repo_dir, runtime.state["session_id"], extract_patch=False
    )

    if response is None:
        return (
            "error: Failed to run commands. Verify that the commands are valid. "
            "If the commands are valid, maybe the bash tool is not working properly."
        )

    return json.dumps([result.model_dump(mode="json") for result in response.results])


@tool(FORMAT_CODE_TOOL_NAME, parse_docstring=True)
async def format_code_tool(runtime: ToolRuntime[RuntimeCtx], force: bool = False) -> str:
    """
    Applies code formatting and linting fixes to the repository to resolve style and linting issues introduced by recent changes.

    **Usage rules:**
     - This tool runs the repository's configured formatting tool (e.g., `ruff format`, `black`, `prettier`, etc.).
     - Use this tool after making code changes to ensure compliance with the project's code style guidelines.
     - You must use your `edit`, `write`, `delete`, `rename` tools at least once in the conversation before formatting the code. This tool will error if you attempt to format the code without changing any files.
     - The tool will return the output of the formatting command if it fails with the details of the error for you to fix. If the formatting command succeeds, the tool will return a success message.
     - If the `force` parameter is set to `True`, the tool will format the code even if no changes were made to the repository.

    Args:
        force: Whether to force the formatting of the code.

    Returns:
        str: The JSON string of the results of the format code command execution.
    """  # noqa: E501
    logger.info("[%s] Formatting code (force: %s)", format_code_tool.name, force)

    if not force and not await has_file_changes(runtime.store):
        return "warning: No changes were made to any files, skipping formatting the code."

    response = await _run_bash_commands(
        runtime.context.config.sandbox.format_code, runtime.context.repo_dir, runtime.state["session_id"]
    )
    if response is None:
        return "error: Failed to format code. The format code tool is not working properly."

    # Return only the last failed result, respecting the fail fast flag.
    if response.results[-1].exit_code != 0:
        return response.results[-1].model_dump_json()

    if response.patch:
        try:
            await _update_store_and_ctx(response.patch, runtime.store, runtime.context.repo_dir)
        except Exception:
            logger.exception("[%s] Error updating store and ctx.", format_code_tool.name)
            return "error: Failed to format code. The format code tool is not working properly."

    return "success: Code formatted."


async def _run_bash_commands(
    commands: list[str], repo_dir: Path, session_id: str, extract_patch: bool = True
) -> RunCommandsResponse | None:
    """
    Run bash commands in the sandbox session.

    Args:
        commands: The list of commands to execute.
        repo_dir: The repository directory.
        session_id: The sandbox session ID.
        extract_patch: Whether to extract the patch with the changes made by the executed commands.

    Returns:
        The response from running the commands.
    """
    tar_archive = io.BytesIO()

    with tarfile.open(fileobj=tar_archive, mode="w:gz") as tar:
        tar.add(repo_dir, arcname=repo_dir.name)

    try:
        response = await DAIVSandboxClient().run_commands(
            session_id,
            RunCommandsRequest(
                commands=commands,
                workdir=repo_dir.name,
                archive=base64.b64encode(tar_archive.getvalue()).decode(),
                fail_fast=True,
                extract_patch=extract_patch,
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

        if read_only_bash:
            self.tools.append(read_only_bash_tool)
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
            StartSessionRequest(base_image=runtime.context.config.sandbox.base_image)
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
