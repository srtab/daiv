from __future__ import annotations

import base64
import fnmatch
import json
import logging
import subprocess  # noqa: S404
from pathlib import Path

from langchain.tools import ToolRuntime, tool
from langchain_core.messages.content import ImageContentBlock

from automation.utils import register_file_read
from codebase.context import RuntimeCtx  # noqa: TC001
from core.utils import extract_valid_image_mimetype

logger = logging.getLogger("daiv.tools")

GLOB_TOOL_NAME = "glob"
GREP_TOOL_NAME = "grep"
LS_TOOL_NAME = "ls"
READ_TOOL_NAME = "read"
NAVIGATION_TOOLS = [GLOB_TOOL_NAME, LS_TOOL_NAME, READ_TOOL_NAME, GREP_TOOL_NAME]

READ_MAX_LINES = 2000


@tool(GLOB_TOOL_NAME, parse_docstring=True)
def glob_tool(pattern: str, runtime: ToolRuntime[RuntimeCtx], path: str | None = None) -> str:
    """
    Find files by name using a glob pattern.

    **Usage rules:**
    - Supports glob patterns like "*.js" or "src/*.ts".
    - Returns matching file paths sorted by name.
    - Use this tool when you need to find files by name patterns.
    - You have the capability to call multiple tools in a single response. It is always better to speculatively perform multiple searches as a batch that are potentially useful.

    Args:
        pattern (str): Glob pattern to match files against. Non-relative patterns are unsupported. (e.g., "**/*.js", "src/**/*.ts", "*.py", "*.md", etc.)
        path (str | None): Directory to search in. If not specified, defaults to the repository root. Must be a relative path.

    Returns:
        A list of file paths with at least one match sorted by name.
    """  # noqa: E501
    logger.debug("[%s] Finding files matching '%s' in %s", glob_tool.name, pattern, path or "repository root")

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


@tool(GREP_TOOL_NAME, parse_docstring=True)
def grep_tool(
    pattern: str, runtime: ToolRuntime[RuntimeCtx], path: str | None = None, include: str | None = None
) -> str:
    """
    Search for files whose *contents* match a regex pattern.

    **Usage rules:**
    - Supports full regex syntax (eg. "log.*Error", "function\\s+\\w+", etc.)
    - Filter files by pattern with the `include` parameter (eg. "*.js", "*.{ts,tsx}", etc.)
    - Returns file paths with at least one match sorted by name
    - Use this tool when you need to find files containing specific patterns
    - You have the capability to call multiple tools in a single response. It is always better to speculatively perform multiple searches as a batch that are potentially useful
    - Under the hood, this tool uses ripgrep
    - **Important:** The `path` parameter must be a directory relative path. If you want to search a single file, leave `path` as None and set `include` to the file path (e.g., ".daiv.yml").

    Args:
        pattern (str): Regular expression to search for (e.g., 'useMemo\\(' or 'class\\s+HttpClient').
        path (str | None): A directory to search in. If not specified, defaults to the repository root. Must be a directory relative path.
        include (str | None): Glob filter for file paths (e.g., '*.js', '*.{ts,tsx}', etc.).

    Returns:
        str: A list of file paths with at least one match sorted by name.
    """  # noqa: E501
    logger.debug(
        "[%s] Finding files matching '%s' in %s (include: '%s')",
        grep_tool.name,
        pattern,
        path or "repository root",
        include,
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


@tool(LS_TOOL_NAME, parse_docstring=True)
def ls_tool(path: str, runtime: ToolRuntime[RuntimeCtx]) -> str:
    """
    Lists files and directories in a given path. The path parameter must be a relative path. You should generally prefer the `glob` and `grep` tools, if you know which directories to search. The results are sorted by name.

    Args:
        path (str): The relative path to the repository root.

    Returns:
        A JSON object with the following fields:
        - type: The type of the entry (e.g., "file", "dir")
        - path: The relative path to the entry. (e.g., "file.txt", "dir/file.txt", "dir/subdir/file.txt", etc.)
    """  # noqa: E501
    logger.debug("[%s] Listing files in %s", ls_tool.name, path)

    root = (Path(runtime.context.repo.working_dir) / path.strip()).resolve()

    if not root.exists() or not root.is_dir():
        logger.warning("[%s] The '%s' does not exist or is not a directory.", ls_tool.name, path)
        return f"error: The '{path}' does not exist or is not a directory."

    if Path(path).anchor:
        return "error: Non-relative paths are unsupported. Use a relative path."

    results = []
    for child in sorted(root.iterdir(), key=lambda p: p.name):
        entry_type = "dir" if child.is_dir() else "file"
        results.append({"type": entry_type, "path": child.relative_to(root).as_posix()})

    if not results:
        return "No files or directories found in the path."

    return json.dumps(results)


@tool(READ_TOOL_NAME, parse_docstring=True)
async def read_tool(file_path: str, runtime: ToolRuntime[RuntimeCtx]) -> str:
    """
    Reads the full content of a file from the repository. You can access any file directly by using this tool. If the User provides a path to a file assume that path is valid. It is okay to read a file that does not exist; an error will be returned.

    **Usage rules:**
     - The `file_path` must be a relative path to the repository root.
     - Results are returned with line numbers starting at 1 (e.g., "1: line1\\n2: line2\\n3: line3")
     - You have the capability to call multiple tools in a single response. It is always better to speculatively read multiple files as a batch that are potentially useful.
     - If you read a file that exists but has empty contents you will receive a system reminder warning in place of file contents.
     - This tool allows you to read images (eg PNG, JPG, etc). When reading an image file the contents are presented visually.

    Args:
        file_path (str): The relative path to the file to read.

    Returns:
        str: The content of the file.
    """  # noqa: E501
    logger.debug("[%s] Reading file '%s'", read_tool.name, file_path)

    resolved_file_path = (Path(runtime.context.repo.working_dir) / file_path.strip()).resolve()

    if (
        not resolved_file_path.exists()
        or not resolved_file_path.is_file()
        or any(fnmatch.fnmatch(file_path, pattern) for pattern in runtime.context.config.combined_exclude_patterns)
    ):
        logger.warning("[%s] The '%s' does not exist or is not a file.", read_tool.name, file_path)
        return f"error: File '{file_path}' does not exist or is not a file."

    if runtime.store:
        # We don't need to store the content, just the fact that the file was read.
        await register_file_read(runtime.store, file_path)

    if any(fnmatch.fnmatch(file_path, pattern) for pattern in runtime.context.config.omit_content_patterns):
        # We can't return None on this cases, otherwise the llm will think the file does not exist and
        # try to create it on some specific scenarios.
        return "[File content was intentionally excluded by the repository configuration]"

    if not (content := resolved_file_path.read_text()):
        return "warning: The file exists but is empty."

    # If the file is an image, return the image template.
    if mime_type := extract_valid_image_mimetype(content.encode()):
        return ImageContentBlock(type="image", base64=base64.b64encode(content.encode()).decode(), mime_type=mime_type)

    return "\n".join(f"{i + 1}: {line}" for i, line in enumerate(content.splitlines()))
