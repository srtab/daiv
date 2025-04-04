from __future__ import annotations

import textwrap
import uuid
from typing import TYPE_CHECKING

from langchain_core.messages import AIMessage, ToolCall, ToolMessage
from langgraph.store.base import BaseStore  # noqa: TC002

if TYPE_CHECKING:
    from langchain_core.prompts.chat import MessageLikeRepresentation


def file_changes_namespace(repo_id: str, ref: str) -> tuple[str, ...]:
    """
    Get the namespace for the file changes.

    Args:
        repo_id: The ID of the source repository.
        ref: The reference of the source repository.

    Returns:
        The namespace for the file changes.
    """
    return ("file_changes", repo_id, ref)


def prepare_repository_files_as_messages(
    paths: list[str], repo_id: str, ref: str, store: BaseStore
) -> list[MessageLikeRepresentation]:
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
    from automation.tools.repository import RETRIEVE_FILE_CONTENT_NAME, RetrieveFileContentTool

    repository_file_contents = RetrieveFileContentTool(ignore_not_found=True).invoke(
        {"file_paths": paths, "intent": "[Manual call] Check current implementation of the files", "store": store},
        config={"configurable": {"source_repo_id": repo_id, "source_ref": ref}},
    )

    if not repository_file_contents:
        return []

    tool_call_id = f"call_{str(uuid.uuid4()).replace('-', '')}"

    return [
        AIMessage(
            content=textwrap.dedent(
                """\
                I'll help you apply the code changes as per the plan. Let me first understand the current implementation by retrieving the referenced files.
                """  # noqa: E501
            ),
            tool_calls=[
                ToolCall(
                    id=tool_call_id,
                    name=RETRIEVE_FILE_CONTENT_NAME,
                    args={"file_paths": paths, "intent": "[Manual call] Check current implementation of the files"},
                )
            ],
        ),
        ToolMessage(tool_call_id=tool_call_id, content=repository_file_contents),
    ]
