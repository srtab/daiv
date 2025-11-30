from __future__ import annotations

import base64
import fnmatch
import json
import logging
import subprocess  # noqa: S404
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.tools import ToolRuntime, tool
from langchain_core.messages.content import ImageContentBlock

from automation.utils import register_file_read
from codebase.context import RuntimeCtx  # noqa: TC001
from codebase.repo_config import CONFIGURATION_FILE_NAME
from core.utils import extract_valid_image_mimetype

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger("daiv.tools")

READ_MAX_LINES = 500

GLOB_TOOL_NAME = "glob"
GREP_TOOL_NAME = "grep"
LS_TOOL_NAME = "ls"
READ_TOOL_NAME = "read"

NAVIGATION_TOOLS = [GLOB_TOOL_NAME, LS_TOOL_NAME, READ_TOOL_NAME, GREP_TOOL_NAME]


GLOB_TOOL_DESCRIPTION = f"""\
Find files by name using a glob pattern.

**Usage rules:**
 - Supports glob patterns like "*.js" or "src/*.ts".
 - Returns matching file paths sorted by name
 - Use this tool when you need to find files by name patterns.
 - You can call multiple tools in a single response. It is always better to speculatively perform multiple searches as a batch that are potentially useful.

Examples:
  Good examples:
    - {GLOB_TOOL_NAME}(pattern="**/*.ts")  # Find all TypeScript files recursively
    - {GLOB_TOOL_NAME}(pattern="**/*.test.js", path="src")  # Find test files only in src directory
    - {GLOB_TOOL_NAME}(pattern="*.config.js")  # Find config files in root (webpack.config.js, etc.)
    - {GLOB_TOOL_NAME}(pattern="**/README.md")  # Find all README files throughout the project
    - {GLOB_TOOL_NAME}(pattern="**/*.py", path="src/components")  # Constrained search in specific directory

  Bad examples (avoid these):
    - {GLOB_TOOL_NAME}(pattern="/home/user/project/*.py")  # Non-relative patterns unsupported
    - {GLOB_TOOL_NAME}(pattern="*.py", path="/absolute/path")  # Path must be relative
    - {GLOB_TOOL_NAME}(pattern="**/*")  # Too broad, returns all files
    - {GLOB_TOOL_NAME}(pattern="package.json")  # Use `{READ_TOOL_NAME}` tool for known files instead
    - {GLOB_TOOL_NAME}(pattern="**/tests/*.py")  # Use path="tests", pattern="*.py" instead
"""  # noqa: E501


GREP_TOOL_DESCRIPTION = f"""\
Search for files whose *contents* match a regex pattern.

**Usage rules:**
 - Supports full regex syntax (eg. "log.*Error", "function\\s+\\w+", etc.)
 - Filter files by pattern with the `include` parameter (eg. "*.js", "*.{{ts,tsx}}", etc.)
 - Returns file paths with at least one match sorted by name
 - Use this tool when you need to find files containing specific patterns
 - You can call multiple tools in a single response. It is always better to speculatively perform multiple searches as a batch that are potentially useful
 - Under the hood, this tool uses ripgrep
 - **Important:** The `path` parameter must be a directory relative path. If you want to search a single file, leave `path` as None and set `include` to the file path (e.g., "{CONFIGURATION_FILE_NAME}")

Examples:
  Good examples:
    - {GREP_TOOL_NAME}(pattern="useMemo\\(")  # Find React useMemo hook usage
    - {GREP_TOOL_NAME}(pattern="class\\s+\\w+", include="*.py")  # Find Python class definitions
    - {GREP_TOOL_NAME}(pattern="TODO|FIXME", path="src")  # Find TODO/FIXME comments in src directory
    - {GREP_TOOL_NAME}(pattern="function\\s+\\w+", include="*.{{js,ts}}")  # Find function declarations in JS/TS files
    - {GREP_TOOL_NAME}(pattern="import.*from", include="tests/test_utils.py")  # Search in a specific file using include

  Bad examples (avoid these):
    - {GREP_TOOL_NAME}(pattern="*.py")  # This is a glob pattern, not regex; use `{GLOB_TOOL_NAME}` tool instead
    - {GREP_TOOL_NAME}(pattern="myFunction", path="src/utils/helper.js")  # Path must be directory; use include="src/utils/helper.js" instead
    - {GREP_TOOL_NAME}(pattern="error", path="/absolute/path")  # Path must be relative
    - {GREP_TOOL_NAME}(pattern="[unclosed")  # Invalid regex pattern
    - {GREP_TOOL_NAME}(pattern=".*")  # Too broad, matches everything
"""  # noqa: E501


LS_TOOL_DESCRIPTION = f"""\
Lists files and directories in a given path. The path parameter must be a relative path. You should generally prefer the `{GLOB_TOOL_NAME}` and `{GREP_TOOL_NAME}` tools, if you know which directories to search. The results are sorted by name.
"""  # noqa: E501


READ_TOOL_DESCRIPTION = """\
Reads the content of a file from the repository. You can access any file directly by using this tool. If the User provides a path to a file assume that path is valid. It is okay to read a file that does not exist; an error will be returned.

**Usage rules:**
 - The `file_path` must be a relative path to the repository root.
 - Results are returned with line numbers starting at 1 (e.g., "1: line1\\n2: line2\\n3: line3")
 - If you read a file that exists but has empty contents you will receive a system reminder warning in place of file contents.
 - This tool allows you to read images (eg PNG, JPG, etc). When reading an image file the contents are presented visually.
 - You can optionally provide the `start_line` and `max_lines` parameters, useful for reading long files, but it's recommended to read the whole file by not providing these parameters.
 - When content is truncated, a message indicates the range shown and total lines available, guiding further reads (e.g., "[Showing lines 1-2000 of 5000 total lines. Use start_line parameter to read more.]").
"""  # noqa: E501

FILE_NAVIGATION_SYSTEM_PROMPT = f"""\
## File navigation tools

You have access to a filesystem which you can interact with using the following tools.
Use these tools to find the files and directories that are relevant to the task.

All file paths are relative to the repository root.

- {GLOB_TOOL_NAME}: Find files matching a glob pattern.
- {GREP_TOOL_NAME}: Search for files whose contents match a regex pattern.
- {LS_TOOL_NAME}: List files and directories in a directory.
- {READ_TOOL_NAME}: Read a file's contents."""


@tool(GLOB_TOOL_NAME, description=GLOB_TOOL_DESCRIPTION)
def glob_tool(
    pattern: Annotated[str, "Glob pattern to match files against. Non-relative patterns are unsupported."],
    runtime: ToolRuntime[RuntimeCtx],
    path: Annotated[
        str | None,
        "Directory to search in. If not specified, defaults to the repository root. Must be a relative path.",
    ] = None,
) -> str:
    """
    Tool to find files by name using a glob pattern.
    """  # noqa: E501
    logger.info("[%s] Finding files matching '%s' in %s", glob_tool.name, pattern, path or ".")

    repo_working_dir = Path(runtime.context.repo.working_dir)
    root = repo_working_dir if path is None else (repo_working_dir / path.strip()).resolve()

    # We assume that the root path is valid if it is not provided.
    if path is not None and (not root.exists() or not root.is_dir()):
        logger.warning("[%s] The '%s' does not exist or is not a directory.", glob_tool.name, path)
        return f"error: The '{path}' does not exist or is not a directory."

    if Path(pattern).anchor:
        return "error: Non-relative patterns are unsupported. Use a relative pattern."

    files = sorted(p.resolve().relative_to(root).as_posix() for p in root.rglob(pattern) if p.is_file())

    if not files:
        return "No files found matching the pattern."

    return "\n".join(files)


def _run_ripgrep(pattern: str, root: Path, include: str | None) -> list[str]:
    """
    Use ripgrep to list files with at least one match.
    - `-l` prints matching file paths once.
    - respects .gitignore by default.
    """
    cmd = ["rg", "-l", "--no-messages", pattern]

    if include:
        cmd += ["--glob", include]

    try:
        proc = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, check=False)  # noqa: S603
    except Exception:
        logger.warning("[%s] Failed to run ripgrep: %s", GREP_TOOL_NAME, cmd)
        raise

    return [(root / line.strip()).relative_to(root).as_posix() for line in proc.stdout.splitlines() if line.strip()]


@tool(GREP_TOOL_NAME, description=GREP_TOOL_DESCRIPTION)
def grep_tool(
    pattern: Annotated[str, "Regular expression to search for (e.g., 'useMemo\\(' or 'class\\s+HttpClient')."],
    runtime: ToolRuntime[RuntimeCtx],
    path: Annotated[
        str | None,
        "A directory to search in. If not specified, defaults to the repository root. "
        "Must be a directory relative path.",
    ] = None,
    include: Annotated[str | None, "Glob filter for file paths (e.g., '*.js', '*.{ts,tsx}', etc.)."] = None,
) -> str:
    """
    Tool to search for files whose contents match a regex pattern.
    """  # noqa: E501
    logger.info(
        "[%s] Searching for files matching '%s' in %s (include: '%s')", grep_tool.name, pattern, path or ".", include
    )

    repo_working_dir = Path(runtime.context.repo.working_dir)
    root = repo_working_dir if path is None else (repo_working_dir / path.strip()).resolve()

    # We assume that the root path is valid if it is not provided.
    if path is not None and (not root.exists() or not root.is_dir()):
        logger.warning("[%s] The '%s' does not exist or is not a directory.", grep_tool.name, path)
        return f"error: The '{path}' does not exist or is not a directory."

    try:
        files = _run_ripgrep(pattern, root, include)
    except Exception:
        return "error: Failed to run ripgrep, revise the arguments and try again."

    if not files:
        return "No files found matching the pattern."

    return "\n".join(files)


@tool(LS_TOOL_NAME, description=LS_TOOL_DESCRIPTION)
def ls_tool(path: Annotated[str, "The relative path to the repository root."], runtime: ToolRuntime[RuntimeCtx]) -> str:
    """
    Tool to list files and directories in a given path.
    """  # noqa: E501
    logger.info("[%s] Listing files in '%s'", ls_tool.name, path)

    root = (Path(runtime.context.repo.working_dir) / path.strip()).resolve()

    if not root.exists() or not root.is_dir():
        logger.warning("[%s] The '%s' does not exist or is not a directory.", ls_tool.name, path)
        return f"error: The '{path}' does not exist or is not a directory."

    if Path(path).anchor:
        return "error: The path is not a relative path."

    results = []
    for child in sorted(root.iterdir(), key=lambda p: p.name):
        entry_type = "dir" if child.is_dir() else "file"
        results.append({"type": entry_type, "path": child.relative_to(root).as_posix()})

    if not results:
        return "No files or directories found in the path."

    return json.dumps(results)


@tool(READ_TOOL_NAME, description=READ_TOOL_DESCRIPTION)
async def read_tool(
    file_path: Annotated[str, "The relative path to the file to read."],
    runtime: ToolRuntime[RuntimeCtx],
    start_line: Annotated[
        int, "The line number to start reading from. Only provide if the file is too large to read at once"
    ] = 1,
    max_lines: Annotated[
        int, "The number of lines to read. Only provide if the file is too large to read at once."
    ] = READ_MAX_LINES,
) -> str:
    """
    Tool to read the content of a file from the repository.
    """  # noqa: E501
    logger.info(
        "[%s] Reading file '%s' (start_line=%d, max_lines=%d)", read_tool.name, file_path, start_line, max_lines
    )

    resolved_file_path = (Path(runtime.context.repo.working_dir) / file_path.strip()).resolve()

    if (
        not resolved_file_path.exists()
        or not resolved_file_path.is_file()
        or any(fnmatch.fnmatch(file_path, pattern) for pattern in runtime.context.config.combined_exclude_patterns)
    ):
        logger.warning("[%s] The file '%s' does not exist or is not a file.", read_tool.name, file_path)
        return f"error: File '{file_path}' does not exist or is not a file."

    if runtime.store:
        # We don't need to store the content, just the fact that the file was read.
        await register_file_read(runtime.store, file_path)

    if any(fnmatch.fnmatch(file_path, pattern) for pattern in runtime.context.config.omit_content_patterns):
        # We can't return None on this cases, otherwise the llm will think the file does not exist and
        # try to create it on some specific scenarios.
        return "[File content was intentionally excluded by the repository configuration]"

    if not (content := resolved_file_path.read_text()):
        return f"warning: The file '{file_path}' exists but is empty."

    if mime_type := extract_valid_image_mimetype(content.encode()):
        return ImageContentBlock(type="image", base64=base64.b64encode(content.encode()).decode(), mime_type=mime_type)

    if start_line < 1:
        return f"error: start_line must be >= 1, got {start_line}."

    lines = content.splitlines()
    total_lines = len(lines)

    if start_line > total_lines:
        return f"error: start_line ({start_line}) exceeds total lines ({total_lines}) in file '{file_path}'."

    start_idx = start_line - 1
    end_idx = min(start_idx + max_lines, total_lines)
    selected_lines = lines[start_idx:end_idx]

    # Format output with actual line numbers from the file
    result_lines = [f"{i}: {line}" for i, line in enumerate(selected_lines, start=start_line)]

    is_truncated_at_start = start_line > 1
    is_truncated_at_end = end_idx < total_lines

    to_return = "\n".join(result_lines)

    if is_truncated_at_start or is_truncated_at_end:
        to_return += (
            f"\n[Showing lines {start_line}-{end_idx} of {total_lines} total lines. "
            "Use start_line parameter to read more.]"
        )

    return to_return


class FileNavigationMiddleware(AgentMiddleware):
    """
    Middleware for providing navigation tools to an agent.
    """

    name = "file_navigation_middleware"

    def __init__(self) -> None:
        """
        Initialize the navigation middleware.
        """
        self.tools = [glob_tool, grep_tool, ls_tool, read_tool]

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelResponse:
        """
        Update the system prompt with the navigation system prompt.

        Args:
            request: The model request being processed.
            handler: The handler function to call with the modified request.

        Returns:
            The model response from the handler.
        """
        request = request.override(system_prompt=request.system_prompt + "\n\n" + FILE_NAVIGATION_SYSTEM_PROMPT)

        return await handler(request)
