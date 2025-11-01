from __future__ import annotations

import logging

from langchain.tools import ToolRuntime, tool

from automation.agents.utils import find_original_snippet
from automation.utils import check_file_read, register_file_change, register_file_read
from codebase.base import FileChangeAction
from codebase.context import RuntimeCtx  # noqa: TC001

logger = logging.getLogger("daiv.tools")


EDIT_TOOL_NAME = "edit"
WRITE_TOOL_NAME = "write"
DELETE_TOOL_NAME = "delete"
RENAME_TOOL_NAME = "rename"

EDITING_TOOLS = [EDIT_TOOL_NAME, WRITE_TOOL_NAME, DELETE_TOOL_NAME, RENAME_TOOL_NAME]


@tool(EDIT_TOOL_NAME, parse_docstring=True)
async def edit_tool(
    file_path: str, old_string: str, new_string: str, runtime: ToolRuntime[RuntimeCtx], replace_all: bool = False
) -> str:
    """
    Performs exact string replacements in files.

    **Usage rules:**
    - You must use your `read` tool at least once in the conversation before editing. This tool will error if you attempt an edit without reading the file.
    - When editing text from `read` tool output, ensure you preserve the exact indentation (tabs/spaces) as it appears AFTER the line number prefix. The line number prefix format is: line number + 1 space. Everything after that space is the actual file content to match. Never include any part of the line number prefix in the `old_string` or `new_string`.
    - The edit will FAIL if `old_string` is not unique in the file. Either provide a larger string with more surrounding context to make it unique or use `replace_all` to change every instance of `old_string`.
    - Use `replace_all` for replacing and renaming strings across the file. This parameter is useful if you want to rename a variable for instance.

    Args:
        file_path (str): The relative path to the file to modify.
        old_string (str): The text to replace.
        new_string (str): The text to replace it with (must be different from old_string)
        replace_all (bool): Replace all occurences of `old_string` (default false)

    Returns:
        str: A message indicating the success of the editing or an error message if the operation failed.
    """  # noqa: E501
    logger.debug("[%s] Editing file '%s'", edit_tool.name, file_path)

    resolved_file_path = (runtime.context.repo_dir / file_path.strip()).resolve()

    if not resolved_file_path.exists() or not resolved_file_path.is_file():
        logger.warning("[%s] The '%s' does not exist or is not a file.", edit_tool.name, file_path)
        return f"error: File '{file_path}' does not exist or is not a file."

    if await check_file_read(runtime.store, file_path.strip()) is False:
        logger.warning("[%s] The '%s' was not read before editing it.", edit_tool.name, file_path)
        return "error: You must read the file before editing it."

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

    await register_file_change(
        store=runtime.store,
        action=FileChangeAction.UPDATE,
        old_file_content=content,
        old_file_path=file_path,
        new_file_content=replaced_content,
    )

    return f"success: Replaced `old_string` with `new_string` in file {file_path}"


@tool(WRITE_TOOL_NAME, parse_docstring=True)
async def write_tool(file_path: str, content: str, runtime: ToolRuntime[RuntimeCtx]) -> str:
    """
    Writes a file to the repository.

    **Usage rules:**
    - This tool will overwrite the existing file if there is one at the provided path.
    - If this is an existing file, you MUST use the `read` tool first to read the file's contents. This tool will fail if you did not read the file first.

    Args:
        file_path (str): The relative path to the file to write to.
        content (str): The content to write to the file.

    Returns:
        str: A message indicating the success of the writing.
    """  # noqa: E501
    logger.debug("[%s] Writing to file '%s'", write_tool.name, file_path)

    resolved_file_path = (runtime.context.repo_dir / file_path.strip()).resolve()
    file_exists = resolved_file_path.exists()

    if file_exists and not resolved_file_path.is_file():
        logger.warning("[%s] The '%s' is not a file.", write_tool.name, file_path)
        return f"error: File '{file_path}' is not a file. Only use this tool to write to files."

    if file_exists and await check_file_read(runtime.store, file_path.strip()) is False:
        logger.warning("[%s] The '%s' was not read before writing to it.", write_tool.name, file_path)
        return "error: You must read the file before writing to it."

    previous_content = resolved_file_path.read_text() if file_exists else ""

    # Create parent directories if they don't exist
    resolved_file_path.parent.mkdir(parents=True, exist_ok=True)

    resolved_file_path.write_text(content)

    if runtime.store:
        # This file was just created and the llm already knows its content so we can register it as read to avoid
        # the need to read it again later.
        await register_file_read(runtime.store, file_path)

    await register_file_change(
        store=runtime.store,
        action=FileChangeAction.UPDATE if file_exists else FileChangeAction.CREATE,
        old_file_content=previous_content,
        old_file_path=file_path,
        new_file_content=content,
    )

    return f"success: Wrote to file {file_path}"


@tool(DELETE_TOOL_NAME, parse_docstring=True)
async def delete_tool(file_path: str, runtime: ToolRuntime[RuntimeCtx]) -> str:
    """
    Deletes a file from the repository.

    **Usage rules:**
    - This tool will delete the file if there is one at the provided path.
    - Do not use this tool to delete directories or non-file entities.
    - Exercise caution to avoid unintended data loss.
    - You must use your `read` tool at least once in the conversation before deleting. This tool will error if you attempt to delete without reading the file.

    Args:
        file_path (str): The relative path to the file to delete.

    Returns:
        str: A message indicating the success of the deletion.
    """  # noqa: E501
    logger.debug("[%s] Deleting file '%s'", delete_tool.name, file_path)

    resolved_file_path = (runtime.context.repo_dir / file_path.strip()).resolve()

    if not resolved_file_path.exists() or not resolved_file_path.is_file():
        logger.warning("[%s] The file '%s' does not exist or is not a file.", delete_tool.name, file_path)
        return f"error: File '{file_path}' does not exist or is not a file."

    if await check_file_read(runtime.store, file_path.strip()) is False:
        logger.warning("[%s] The '%s' was not read before deleting it.", delete_tool.name, file_path)
        return "error: You must read the file before deleting it."

    previous_content = resolved_file_path.read_text()

    try:
        resolved_file_path.unlink()
    except FileNotFoundError:
        logger.warning("[%s] The file '%s' does not exist.", delete_tool.name, file_path)
        return f"error: File '{file_path}' does not exist."

    await register_file_change(
        store=runtime.store,
        action=FileChangeAction.DELETE,
        old_file_content=previous_content,
        old_file_path=file_path,
        new_file_content="",
    )

    return f"success: Deleted file {file_path}"


@tool(RENAME_TOOL_NAME, parse_docstring=True)
async def rename_tool(file_path: str, new_file_path: str, runtime: ToolRuntime[RuntimeCtx]) -> str:
    """
    Renames a file in the repository.

    **Usage rules:**
    - This tool will rename the file if it exists.
    - If the file does not exist or the new path already exists, this tool will return an error.
    - You must use your `read` tool at least once in the conversation before renaming. This tool will error if you attempt to rename without reading the file.

    Args:
        file_path (str): The relative path to the file to rename.
        new_file_path (str): The new relative path to the file.

    Returns:
        str: A message indicating the success of the renaming.
    """  # noqa: E501
    logger.debug("[%s] Renaming file '%s' to '%s'", rename_tool.name, file_path, new_file_path)

    resolved_file_path = (runtime.context.repo_dir / file_path.strip()).resolve()
    resolved_new_file_path = (runtime.context.repo_dir / new_file_path.strip()).resolve()

    if not resolved_file_path.exists() or not resolved_file_path.is_file():
        logger.warning("[%s] The file '%s' does not exist or is not a file.", rename_tool.name, file_path)
        return f"error: File with path '{file_path}' does not exist or is not a file."

    if resolved_new_file_path.exists():
        logger.warning("[%s] The file '%s' already exists.", rename_tool.name, new_file_path)
        return f"error: File with path '{new_file_path}' already exists."

    if await check_file_read(runtime.store, file_path.strip()) is False:
        logger.warning("[%s] The '%s' was not read before renaming it.", rename_tool.name, file_path)
        return "error: You must read the file before renaming it."

    file_content = resolved_file_path.read_text()

    # Create parent directories for the new path if they don't exist
    resolved_new_file_path.parent.mkdir(parents=True, exist_ok=True)

    resolved_file_path.rename(resolved_new_file_path)

    await register_file_change(
        store=runtime.store,
        action=FileChangeAction.MOVE,
        old_file_content=file_content,
        old_file_path=file_path,
        new_file_path=new_file_path,
    )

    return f"success: Renamed file '{file_path}' to '{new_file_path}'"
