from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from langchain.tools import ToolRuntime

from automation.agents.tools.sandbox import bash_tool
from codebase.context import get_runtime_ctx

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
    ctx = get_runtime_ctx()

    if not ctx.config.sandbox.enabled or not ctx.config.sandbox.format_code:
        logger.info("Format code is disabled for this repository, skipping.")
        # If format code is disabled, we need to try to fix the linting issues by planning the remediation steps.
        # This is less effective than actually formatting the code, but it's better than nothing. For instance,
        # linting errors like whitespaces can be challenging to fix by an agent, or even impossible.
        return None

    # we need to pass a tool call in order to get the artifact, otherwise the tool will return only a string
    tool_message = await bash_tool.ainvoke({
        "name": bash_tool.name,
        "id": uuid.uuid4(),
        "args": {
            "commands": ctx.config.sandbox.format_code,
            "runtime": ToolRuntime(
                state=None, tool_call_id=uuid.uuid4(), config=None, context=None, store=store, stream_writer=None
            ),
        },
        "type": "tool_call",
    })
    return tool_message.artifact and tool_message.artifact.output
