from __future__ import annotations

from deepagents.middleware.filesystem import EDIT_FILE_TOOL_DESCRIPTION as EDIT_FILE_TOOL_DESCRIPTION_BASE
from deepagents.middleware.filesystem import GLOB_TOOL_DESCRIPTION as GLOB_TOOL_DESCRIPTION_BASE
from deepagents.middleware.filesystem import GREP_TOOL_DESCRIPTION as GREP_TOOL_DESCRIPTION_BASE
from deepagents.middleware.filesystem import LIST_FILES_TOOL_DESCRIPTION as LIST_FILES_TOOL_DESCRIPTION_BASE
from deepagents.middleware.filesystem import READ_FILE_TOOL_DESCRIPTION as READ_FILE_TOOL_DESCRIPTION_BASE
from deepagents.middleware.filesystem import WRITE_FILE_TOOL_DESCRIPTION as WRITE_FILE_TOOL_DESCRIPTION_BASE
from deepagents.middleware.filesystem import FilesystemMiddleware as BaseFilesystemMiddleware
from langchain_core.prompts import SystemMessagePromptTemplate

REMINDER_ABSOLUTE_PATHS = """
IMPORTANT:
- Tool inputs/outputs use absolute paths (e.g. /repo/...).
- DO NOT output these absolute paths to the user.
- Convert to repo-relative paths in all user-visible text.
"""

GREP_TOOL_DESCRIPTION = GREP_TOOL_DESCRIPTION_BASE + "\n" + REMINDER_ABSOLUTE_PATHS

GLOB_TOOL_DESCRIPTION = GLOB_TOOL_DESCRIPTION_BASE + "\n" + REMINDER_ABSOLUTE_PATHS

LIST_FILES_TOOL_DESCRIPTION = LIST_FILES_TOOL_DESCRIPTION_BASE + "\n" + REMINDER_ABSOLUTE_PATHS

READ_FILE_TOOL_DESCRIPTION = READ_FILE_TOOL_DESCRIPTION_BASE + "\n" + REMINDER_ABSOLUTE_PATHS

WRITE_FILE_TOOL_DESCRIPTION = WRITE_FILE_TOOL_DESCRIPTION_BASE + "\n" + REMINDER_ABSOLUTE_PATHS

EDIT_FILE_TOOL_DESCRIPTION = EDIT_FILE_TOOL_DESCRIPTION_BASE + "\n" + REMINDER_ABSOLUTE_PATHS

DAIV_FILESYSTEM_SYSTEM_PROMPT = SystemMessagePromptTemplate.from_template(
    """\
## Filesystem Tools

You have access to a filesystem which you can interact with using these tools.
Tool-call arguments (ls/read_file{{^read_only}}/edit_file{{/read_only}}/etc.) MUST use absolute paths (start with "/").
User-visible output MUST NEVER contain "/repo/" and MUST use repo-relative paths (e.g. daiv/core/utils.py).""",
    "mustache",
)


class FilesystemMiddleware(BaseFilesystemMiddleware):
    """Extended FilesystemMiddleware with delete and rename tools.

    This middleware extends the standard FilesystemMiddleware to add delete
    and rename capabilities for the DAIV deep agent.

    Args:
        backend: Backend for file storage. Should be DAIVFilesystemBackend or DAIVStateBackend.
        system_prompt: Optional custom system prompt override.
        custom_tool_descriptions: Optional custom tool descriptions override.
        tool_token_limit_before_evict: Optional token limit before evicting a tool result.

    Example:
        ```python
        from automation.agent.backends.filesystem import FilesystemBackend
        from automation.agent.middlewares.file_system import FilesystemMiddleware

        backend = FilesystemBackend(root_dir="/workspace", virtual_mode=True)
        middleware = FilesystemMiddleware(backend=backend)
        ```
    """

    def __init__(self, *args, read_only: bool = False, **kwargs) -> None:
        """
        Initialize the FilesystemMiddleware.

        Args:
            *args: Additional arguments to pass to the superclass.
            read_only: Whether the filesystem is read-only.
            **kwargs: Additional keyword arguments to pass to the superclass.
        """
        system_prompt = kwargs.pop("system_prompt", DAIV_FILESYSTEM_SYSTEM_PROMPT.format(read_only=read_only).content)
        custom_tool_descriptions = kwargs.pop(
            "custom_tool_descriptions",
            {
                "grep": GREP_TOOL_DESCRIPTION,
                "glob": GLOB_TOOL_DESCRIPTION,
                "ls": LIST_FILES_TOOL_DESCRIPTION,
                "read_file": READ_FILE_TOOL_DESCRIPTION,
                "write_file": WRITE_FILE_TOOL_DESCRIPTION,
                "edit_file": EDIT_FILE_TOOL_DESCRIPTION,
            },
        )
        super().__init__(
            *args, system_prompt=system_prompt, custom_tool_descriptions=custom_tool_descriptions, **kwargs
        )
        excluded_tools = ["execute"]

        if read_only:
            excluded_tools.append("edit_file")
            excluded_tools.append("write_file")

        self.tools = [tool for tool in self.tools if tool.name not in excluded_tools]
