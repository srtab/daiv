from __future__ import annotations

from typing import TYPE_CHECKING, Any

from deepagents.middleware.filesystem import EDIT_FILE_TOOL_DESCRIPTION as EDIT_FILE_TOOL_DESCRIPTION_BASE
from deepagents.middleware.filesystem import GLOB_TOOL_DESCRIPTION as GLOB_TOOL_DESCRIPTION_BASE
from deepagents.middleware.filesystem import GREP_TOOL_DESCRIPTION as GREP_TOOL_DESCRIPTION_BASE
from deepagents.middleware.filesystem import LIST_FILES_TOOL_DESCRIPTION as LIST_FILES_TOOL_DESCRIPTION_BASE
from deepagents.middleware.filesystem import READ_FILE_TOOL_DESCRIPTION as READ_FILE_TOOL_DESCRIPTION_BASE
from deepagents.middleware.filesystem import WRITE_FILE_TOOL_DESCRIPTION as WRITE_FILE_TOOL_DESCRIPTION_BASE
from deepagents.middleware.filesystem import FilesystemMiddleware as BaseFilesystemMiddleware
from deepagents.middleware.filesystem import FilesystemState
from langchain.tools import ToolRuntime
from langchain_core.prompts import SystemMessagePromptTemplate

from automation.agent.utils import copy_builtin_skills_to_backend

if TYPE_CHECKING:
    from deepagents.backends.protocol import BackendProtocol
    from langchain_core.runnables import RunnableConfig
    from langgraph.runtime import Runtime


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
## Filesystem Tools `ls`, `read_file`, {{#read_only}}write_file`, `edit_file`, {{/read_only}}`glob`, `grep`

You have access to a filesystem which you can interact with using these tools.
Tool-call arguments (ls/read_file{{#read_only}}/edit_file{{/read_only}}/etc.) MUST use absolute paths (start with "/").
User-visible output MUST NEVER contain "/repo/" and MUST use repo-relative paths.

 - ls: list files in a directory
 - read_file: read a file from the filesystem
{{^read_only}}
 - write_file: write to a file in the filesystem
 - edit_file: edit a file in the filesystem
{{/read_only}}
 - glob: find files matching a pattern (e.g., "**/*.py")
 - grep: search for text within files
""",
    "mustache",
)


def _get_backend2(
    backend: BackendProtocol, state: FilesystemState, runtime: Runtime, config: RunnableConfig
) -> BackendProtocol:
    if callable(backend):
        # Construct an artificial tool runtime to resolve backend factory
        tool_runtime = ToolRuntime(
            state=state,
            context=runtime.context,
            stream_writer=runtime.stream_writer,
            store=runtime.store,
            config=config,
            tool_call_id=None,
        )
        backend = backend(tool_runtime)
        if backend is None:
            raise AssertionError("FilesystemMiddleware requires a valid backend instance")
        return backend

    return backend


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
                "read_file": READ_FILE_TOOL_DESCRIPTION,
                "write_file": WRITE_FILE_TOOL_DESCRIPTION,
                "edit_file": EDIT_FILE_TOOL_DESCRIPTION,
                "list_files": LIST_FILES_TOOL_DESCRIPTION,
            },
        )
        super().__init__(
            *args, system_prompt=system_prompt, custom_tool_descriptions=custom_tool_descriptions, **kwargs
        )
        self.read_only = read_only
        if self.read_only:
            self.tools = [tool for tool in self.tools if tool.name not in ["edit_file", "write_file"]]

    async def abefore_agent(
        self, state: FilesystemState, runtime: Runtime, config: RunnableConfig
    ) -> dict[str, Any] | None:
        """
        Before the agent starts, add the builtin skills to the state.
        """
        files_to_update = await copy_builtin_skills_to_backend(_get_backend2(self.backend, state, runtime, config))
        if files_to_update:
            return {"files": files_to_update}
        return None
