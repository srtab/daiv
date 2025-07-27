from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from langchain_core.runnables import RunnableConfig  # noqa: TC002

from automation.tools.sandbox import RunSandboxCommandsTool
from core.config import RepositoryConfig

if TYPE_CHECKING:
    from langgraph.store.base import BaseStore

logger = logging.getLogger("daiv.agents")


async def apply_format_code(source_repo_id: str, source_ref: str | None, store: BaseStore) -> str | None:
    """
    Apply format code to the repository to fix the linting issues in the pipeline.

    Args:
        source_repo_id (str): The ID of the source repository.
        source_ref (str | None): The reference of the source repository.
        store (BaseStore): The store to use for caching.

    Returns:
        str | None: The output of the last command, or None if format code is disabled.
    """
    repo_config = RepositoryConfig.get_config(source_repo_id)

    if not repo_config.commands.enabled():
        logger.info("Format code is disabled for this repository, skipping.")
        # If format code is disabled, we need to try to fix the linting issues by planning the remediation steps.
        # This is less effective than actually formatting the code, but it's better than nothing. For instance,
        # linting errors like whitespaces can be challenging to fix by an agent, or even impossible.
        return None

    tool_message = await RunSandboxCommandsTool().ainvoke(
        {
            "name": "run_commands",
            "args": {
                "commands": [repo_config.commands.install_dependencies, repo_config.commands.format_code],
                "intent": "[Manual run] Format code in the repository to fix the pipeline issue.",
                "store": store,
            },
            "id": str(uuid.uuid4()),
            "type": "tool_call",
        },
        config=RunnableConfig(configurable={"source_repo_id": source_repo_id, "source_ref": source_ref}),
    )
    return tool_message.artifact[-1].output  # Return the output of the last command
