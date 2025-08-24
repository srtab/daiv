from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from automation.agents.tools.sandbox import bash_tool
from codebase.context import get_repository_ctx

if TYPE_CHECKING:
    from langgraph.store.base import BaseStore

logger = logging.getLogger("daiv.agents")


async def apply_format_code_node(store: BaseStore) -> str | None:
    """
    Apply format code to the repository to fix the linting issues in the pipeline.

    Args:
        store (BaseStore): The store to use for caching.

    Returns:
        str | None: The output of the last command, or None if format code is disabled.
    """
    ctx = get_repository_ctx()

    if not ctx.config.commands.enabled():
        logger.info("Format code is disabled for this repository, skipping.")
        # If format code is disabled, we need to try to fix the linting issues by planning the remediation steps.
        # This is less effective than actually formatting the code, but it's better than nothing. For instance,
        # linting errors like whitespaces can be challenging to fix by an agent, or even impossible.
        return None

    tool_message = await bash_tool.ainvoke({
        "commands": [ctx.config.commands.install_dependencies, ctx.config.commands.format_code],
        "store": store,
    })
    return tool_message.artifact and tool_message.artifact["output"]  # Return the output of the last command
