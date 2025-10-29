from __future__ import annotations

import base64
import io
import json
import logging
import subprocess  # noqa: S404
import tarfile
import uuid
from textwrap import dedent
from typing import TYPE_CHECKING

import httpx
from langchain.tools import ToolRuntime  # noqa: TC002
from langchain_core.tools import tool
from langgraph.typing import StateT  # noqa: TC002
from unidiff import PatchSet

from automation.utils import has_file_changes, register_file_change
from codebase.base import FileChangeAction
from codebase.context import RuntimeCtx
from core.conf import settings
from core.sandbox import run_sandbox_commands, start_sandbox_session
from core.sandbox.schemas import RunCommandResult, RunCommandsRequest, StartSessionRequest

if TYPE_CHECKING:
    from pathlib import Path

    from langchain_core.messages import ToolMessage
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


@tool(BASH_TOOL_NAME, parse_docstring=True, response_format="content_and_artifact")
async def bash_tool(commands: list[str], runtime: ToolRuntime[RuntimeCtx]) -> tuple[str, RunCommandResult | None]:
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
    logger.debug("[%s] Running commands: %s", bash_tool.name, commands)

    assert settings.SANDBOX_API_KEY is not None, "SANDBOX_API_KEY is not set"

    # Start the sandbox session to ensure that the sandbox session is started before running the commands.
    # If the sandbox session already exists in the store, it will be reused.
    await start_sandbox_session(
        StartSessionRequest(base_image=runtime.context.config.sandbox.base_image), runtime.store
    )

    tar_archive = io.BytesIO()
    with tarfile.open(fileobj=tar_archive, mode="w:gz") as tar:
        tar.add(runtime.context.repo_dir, arcname=runtime.context.repo_dir.name)

    try:
        response = await run_sandbox_commands(
            RunCommandsRequest(
                commands=commands,
                workdir=runtime.context.repo_dir.name,
                archive=base64.b64encode(tar_archive.getvalue()).decode(),
                fail_fast=True,
                extract_patch=True,
            ),
            runtime.store,
        )
    except httpx.RequestError:
        logger.exception("[%s] Unexpected error calling sandbox API.", bash_tool.name)
        return "error: Failed to run commands. The bash tool is not working properly.", None
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 400:
            logger.error("[%s] Bad request calling sandbox API: %s", bash_tool.name, e.response.text)
            return (
                "error: Failed to run commands. Verify that the commands are valid. "
                "If the commands are valid, maybe the bash tool is not working properly."
            ), None

        logger.exception(
            "[%s] Status code %s calling sandbox API: %s", bash_tool.name, e.response.status_code, e.response.text
        )
        return "error: Failed to run commands. The bash tool is not working properly.", None
    finally:
        tar_archive.close()

    if response.patch:
        try:
            await _update_store_and_ctx(response.patch, runtime.store, runtime.context.repo_dir)
        except Exception:
            logger.exception("[%s] Error updating store and ctx.", bash_tool.name)
            return "error: Failed to finalize the commands.", None

    return json.dumps([result.model_dump(mode="json") for result in response.results]), response.results[-1]


@tool(FORMAT_CODE_TOOL_NAME, parse_docstring=True)
async def format_code_tool(placeholder: str, runtime: ToolRuntime[RuntimeCtx]) -> str:
    """
    Applies code formatting and linting fixes to the repository to resolve style and linting issues introduced by recent changes.

    **Usage rules:**
     - This tool runs the repository's configured formatting tool (e.g., `ruff format`, `black`, `prettier`, etc.).
     - Use this tool after making code changes to ensure compliance with the project's code style guidelines.
     - You must use your `edit`, `write`, `delete`, `rename` tools at least once in the conversation before formatting the code. This tool will error if you attempt to format the code without changing any files.
     - The tool will return the output of the formatting command if it fails with the details of the error for you to fix. If the formatting command succeeds, the tool will return a success message.

    Args:
        placeholder: Unused parameter (for compatibility). Leave empty.

    Returns:
        str: The output of the format code command execution.
    """  # noqa: E501
    if not runtime.context.config.sandbox.enabled or not runtime.context.config.sandbox.format_code:
        return "warning: Format code is not enabled for this repository."

    if not await has_file_changes(runtime.store):
        return "warning: No changes were made to any files, skipping formatting the code."

    tool_call_id = uuid.uuid4()

    # we need to pass a tool call in order to get the artifact, otherwise the tool will return only a string
    tool_message: ToolMessage = await bash_tool.ainvoke({
        "type": "tool_call",
        "name": bash_tool.name,
        "id": tool_call_id,
        "args": {
            "commands": runtime.context.config.sandbox.format_code,
            "runtime": ToolRuntime[RuntimeCtx, StateT](
                state=runtime.state,
                tool_call_id=tool_call_id,
                config=runtime.config,
                context=runtime.context,
                store=runtime.store,
                stream_writer=runtime.stream_writer,
            ),
        },
    })

    if tool_message.artifact is None:
        return "error: Failed to format code. The format code tool is not working properly."
    elif tool_message.artifact.exit_code != 0:
        return dedent(f"""\
            error: Failed to format code:
            <command>{tool_message.artifact.command}</command>
            <exit_code>{tool_message.artifact.exit_code}</exit_code>
            <output>{tool_message.artifact.output}</output>""")

    return "success: Code formatted."
