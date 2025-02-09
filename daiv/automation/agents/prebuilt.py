from __future__ import annotations

import logging
import textwrap
import uuid

from langchain_core.messages import AIMessage, BaseMessage, ToolCall, ToolMessage
from langgraph.store.base import BaseStore  # noqa: TC002

from automation.tools.repository import RETRIEVE_FILE_CONTENT_NAME, RetrieveFileContentTool

logger = logging.getLogger("daiv.agents")


def prepare_repository_files_as_messages(
    repo_id: str, ref: str, paths: list[str], store: BaseStore
) -> list[BaseMessage]:
    """
    Prepare repository files as messages to preload them in agents to reduce their execution time.

    This is useful for agents that use plan and execute reasoning.

    Args:
        repo_id (str): The ID of the repository.
        ref (str): The reference of the repository.
        paths (list[str]): The paths of the files to preload.
        store (BaseStore): The used store for file changes.

    Returns:
        list[AIMessage | ToolMessage]: The messages to preload in agents.
    """
    retrieve_file_content_tool = RetrieveFileContentTool(return_not_found_message=False)

    tool_calls = []
    tool_call_messages = []

    for path in set(paths):
        if repository_file_content := retrieve_file_content_tool.invoke(
            {"file_path": path, "intent": "[Manual call] Check current implementation", "store": store},
            config={"configurable": {"source_repo_id": repo_id, "source_ref": ref}},
        ):
            tool_call_id = str(uuid.uuid4())
            tool_calls.append(
                ToolCall(
                    id=tool_call_id,
                    name=RETRIEVE_FILE_CONTENT_NAME,
                    args={"file_path": path, "intent": "Check current implementation"},
                )
            )
            tool_call_messages.append(ToolMessage(content=repository_file_content, tool_call_id=tool_call_id))

    if not tool_calls:
        return []

    return [
        AIMessage(
            content=textwrap.dedent(
                """\
                I'll help you execute these tasks precisely. Let's go through them step by step.

                <explanation>
                First, I'll examine the existing files to understand the current implementation and ensure our changes align with the codebase patterns. Let me retrieve the relevant files.
                </explanation>
                """  # noqa: E501
            ),
            tool_calls=tool_calls,
        ),
        *tool_call_messages,
    ]
