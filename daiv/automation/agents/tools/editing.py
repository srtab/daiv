from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.tools import ToolRuntime, tool

from automation.agents.utils import find_original_snippet
from automation.utils import check_file_read, register_file_read
from codebase.context import RuntimeCtx  # noqa: TC001

from .navigation import READ_TOOL_NAME

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger("daiv.tools")


EDIT_TOOL_NAME = "edit"
WRITE_TOOL_NAME = "write"
DELETE_TOOL_NAME = "delete"
RENAME_TOOL_NAME = "rename"

EDITING_TOOLS = [EDIT_TOOL_NAME, WRITE_TOOL_NAME, DELETE_TOOL_NAME, RENAME_TOOL_NAME]

EDIT_TOOL_DESCRIPTION = f"""\
Performs exact string replacements in files.

**Usage rules:**
 - You should use your `{READ_TOOL_NAME}` tool at least once in the conversation before editing. This tool will error if you attempt an edit without reading the file.
 - When editing text from `{READ_TOOL_NAME}` tool output, ensure you preserve the exact indentation (tabs/spaces) as it appears AFTER the line number prefix. The line number prefix format is: line number + 1 space. Everything after that space is the actual file content to match. Never include any part of the line number prefix in the `old_string` or `new_string`.
 - The edit will FAIL if `old_string` is not unique in the file. Either provide a larger string with more surrounding context to make it unique or use `replace_all` to change every instance of `old_string`.
 - Use `replace_all` for replacing and renaming strings across the file. This parameter is useful if you want to rename a variable for instance.
"""  # noqa: E501

WRITE_TOOL_DESCRIPTION = f"""\
Writes a file to the repository.

**Usage rules:**
 - This tool will overwrite the existing file if there is one at the provided path.
 - If this is an existing file, you should use the `{READ_TOOL_NAME}` tool first to read the file's contents. This tool will fail if you did not read the file first.
"""  # noqa: E501

DELETE_TOOL_DESCRIPTION = f"""\
Deletes a file or directory from the repository.

**Usage rules:**
 - For FILES: Deletes the specified file. You should read the file first using the `{READ_TOOL_NAME}` tool.
 - For DIRECTORIES: Recursively deletes the directory and ALL its contents. Use with EXTREME caution.
 - Before deleting a directory, you should use the `ls` or `glob` tool to examine its contents.
 - You must verify the path exists and understand what you're deleting before using this tool.
 - This operation is irreversible - exercise caution to avoid unintended data loss.
"""  # noqa: E501

RENAME_TOOL_DESCRIPTION = f"""\
Renames a file in the repository.

**Usage rules:**
 - This tool will rename the file if it exists.
 - If the file does not exist or the new path already exists, this tool will return an error.
 - You should use your `{READ_TOOL_NAME}` tool at least once in the conversation before renaming. This tool will error if you attempt to rename without reading the file.
"""  # noqa: E501


EDITING_TOOL_SYSTEM_PROMPT = f"""\
## File editing tools

You have access to a set of tools to apply changes to the files in the repository.

All file paths are relative to the repository root. You should use the `{READ_TOOL_NAME}` tool to read the file before you can edit it.

- {EDIT_TOOL_NAME}: Perform exact string replacements in files.
- {WRITE_TOOL_NAME}: Write a file.
- {DELETE_TOOL_NAME}: Delete a file or directory.
- {RENAME_TOOL_NAME}: Rename a file.
"""  # noqa: E501


@tool(EDIT_TOOL_NAME, description=EDIT_TOOL_DESCRIPTION)
async def edit_tool(
    file_path: Annotated[str, "The relative path to the file to modify."],
    old_string: Annotated[str, "The text to replace."],
    new_string: Annotated[str, "The text to replace it with (must be different from old_string)."],
    runtime: ToolRuntime[RuntimeCtx],
    replace_all: Annotated[bool, "Replace all occurences of `old_string` (default false)."] = False,
) -> str:
    """
    Tool to perform exact string replacements in files.
    """  # noqa: E501
    logger.debug("[%s] Editing file '%s'", edit_tool.name, file_path)

    resolved_file_path = (Path(runtime.context.repo.working_dir) / file_path.strip()).resolve()

    if not resolved_file_path.exists() or not resolved_file_path.is_file():
        logger.warning("[%s] The '%s' does not exist or is not a file.", edit_tool.name, file_path)
        return f"error: File '{file_path}' does not exist or is not a file."

    if await check_file_read(runtime.store, file_path.strip()) is False:
        logger.warning("[%s] The '%s' was not read before editing it.", edit_tool.name, file_path)
        return "error: You must read the file before editing it. Call the `read` tool to read the file first."

    if not (content := resolved_file_path.read_text()):
        return "error: The file exists but is empty. Use the `write` tool to write to it instead."

    if old_string == new_string:
        logger.warning("[%s] The old_string and the new_string are the same.", edit_tool.name)
        return (
            "error: The old_string and the new_string are the same. "
            "No changes will be made. Make sure you're not missing any changes."
        )

    if not (old_string_found := find_original_snippet(old_string, content, initial_line_threshold=1)):
        logger.warning("[%s] The '%s' was not found in the file.", edit_tool.name, old_string)
        return "error: The old_string was not found in the file. Please check the old_string and try again."

    if not replace_all and (old_string_found_count := len(old_string_found)) > 1:
        logger.warning(
            "[%s] The old_string is not unique in the file. Found %d occurrences.",
            edit_tool.name,
            old_string_found_count,
        )
        return (
            "error: The old_string is not unique in the file. "
            "Please provide the old_string with more surrounding context to make it unique."
        )

    replaced_content = content.replace(old_string_found[0], new_string, count=-1 if replace_all else 1)

    resolved_file_path.write_text(replaced_content)

    return f"success: Replaced `old_string` with `new_string` in file {file_path}"


@tool(WRITE_TOOL_NAME, description=WRITE_TOOL_DESCRIPTION)
async def write_tool(
    file_path: Annotated[str, "The relative path to the file to write to."],
    content: Annotated[str, "The content to write to the file."],
    runtime: ToolRuntime[RuntimeCtx],
) -> str:
    """
    Tool to write content to a file.
    """  # noqa: E501
    logger.debug("[%s] Writing to file '%s'", write_tool.name, file_path)

    resolved_file_path = (Path(runtime.context.repo.working_dir) / file_path.strip()).resolve()
    file_exists = resolved_file_path.exists()

    if file_exists and not resolved_file_path.is_file():
        logger.warning("[%s] The '%s' is not a file.", write_tool.name, file_path)
        return f"error: File '{file_path}' is not a file. Only use this tool to write to files."

    if runtime.context.repo.ignored([file_path]):
        logger.warning("[%s] The file '%s' matches patterns in .gitignore.", write_tool.name, file_path)
        return (
            f"error: Cannot create/write file '{file_path}' because it matches patterns in .gitignore. "
            "Files matching .gitignore should not be committed."
        )

    if file_exists and await check_file_read(runtime.store, file_path.strip()) is False:
        logger.warning("[%s] The '%s' was not read before writing to it.", write_tool.name, file_path)
        return "error: You must read the file before writing to it. Call the `read` tool to read the file first."

    # Create parent directories if they don't exist
    resolved_file_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_file_path.write_text(content)

    if runtime.store:
        # This file was just created and the llm already knows its content so we can register it as read to avoid
        # the need to read it again later.
        await register_file_read(runtime.store, file_path)

    return f"success: Wrote to file {file_path}"


@tool(DELETE_TOOL_NAME, description=DELETE_TOOL_DESCRIPTION)
async def delete_tool(
    path: Annotated[str, "The relative path to the file or directory to delete."], runtime: ToolRuntime[RuntimeCtx]
) -> str:
    """
    Tool to delete a file or directory from the repository.
    """  # noqa: E501
    logger.debug("[%s] Deleting path '%s'", delete_tool.name, path)

    resolved_path = (Path(runtime.context.repo.working_dir) / path.strip()).resolve()

    if not resolved_path.exists():
        logger.warning("[%s] The path '%s' does not exist.", delete_tool.name, path)
        return f"error: Path '{path}' does not exist."

    if resolved_path.is_file():
        if await check_file_read(runtime.store, path.strip()) is False:
            logger.warning("[%s] The file '%s' was not read before deleting it.", delete_tool.name, path)
            return "error: You must read the file before deleting it. Call the `read` tool to read the file first."

        try:
            resolved_path.unlink()
            return f"success: Deleted file '{path}'"
        except FileNotFoundError:
            logger.warning("[%s] The file '%s' does not exist.", delete_tool.name, path)
            return f"error: File '{path}' does not exist."

    elif resolved_path.is_dir():
        try:
            shutil.rmtree(resolved_path)
            return f"success: Deleted directory '{path}' and all its contents"
        except OSError:
            logger.warning("[%s] Failed to delete directory '%s'.", delete_tool.name, path, exc_info=True)
            return f"error: Failed to delete directory '{path}'."

    else:
        logger.warning("[%s] The path '%s' is neither a file nor a directory.", delete_tool.name, path)
        return f"error: Path '{path}' is neither a file nor a directory."


@tool(RENAME_TOOL_NAME, description=RENAME_TOOL_DESCRIPTION)
async def rename_tool(
    file_path: Annotated[str, "The relative path to the file to rename."],
    new_file_path: Annotated[str, "The new relative path to the file."],
    runtime: ToolRuntime[RuntimeCtx],
) -> str:
    """
    Tool to rename a file in the repository.
    """  # noqa: E501
    logger.debug("[%s] Renaming file '%s' to '%s'", rename_tool.name, file_path, new_file_path)

    repo_working_dir = Path(runtime.context.repo.working_dir)
    resolved_file_path = (repo_working_dir / file_path.strip()).resolve()
    resolved_new_file_path = (repo_working_dir / new_file_path.strip()).resolve()

    if not resolved_file_path.exists() or not resolved_file_path.is_file():
        logger.warning("[%s] The file '%s' does not exist or is not a file.", rename_tool.name, file_path)
        return f"error: File with path '{file_path}' does not exist or is not a file."

    if resolved_new_file_path.exists():
        logger.warning("[%s] The file '%s' already exists.", rename_tool.name, new_file_path)
        return f"error: File with path '{new_file_path}' already exists."

    if runtime.context.repo.ignored([new_file_path]):
        logger.warning(
            "[%s] The destination file '%s' matches patterns in .gitignore.", rename_tool.name, new_file_path
        )
        return (
            f"error: Cannot rename file to '{new_file_path}' because it matches patterns in .gitignore. "
            "Files matching .gitignore should not be committed."
        )

    if await check_file_read(runtime.store, file_path.strip()) is False:
        logger.warning("[%s] The '%s' was not read before renaming it.", rename_tool.name, file_path)
        return "error: You must read the file before renaming it. Call the `read` tool to read the file first."

    # Create parent directories for the new path if they don't exist
    resolved_new_file_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_file_path.rename(resolved_new_file_path)

    return f"success: Renamed file '{file_path}' to '{new_file_path}'"


class FileEditingMiddleware(AgentMiddleware):
    """
    Middleware to add the file editing tools and system prompt to the agent.
    """

    name = "file_editing_middleware"

    def __init__(self) -> None:
        """
        Initialize the middleware.
        """
        self.tools = [write_tool, edit_tool, delete_tool, rename_tool]

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelResponse:
        """
        Update the system prompt with the file editing system prompt.

        Args:
            request (ModelRequest): The request to the model.
            handler (Callable[[ModelRequest], Awaitable[ModelResponse]]): The handler to call the model.

        Returns:
            ModelResponse: The response from the model.
        """
        request = request.override(system_prompt=request.system_prompt + "\n\n" + EDITING_TOOL_SYSTEM_PROMPT)
        return await handler(request)
