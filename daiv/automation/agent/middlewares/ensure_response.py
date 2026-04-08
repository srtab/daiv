from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from langchain.agents.middleware import AgentState, after_model
from langchain_core.messages import ToolMessage

if TYPE_CHECKING:
    from langgraph.types import Command

logger = logging.getLogger("daiv.agent")

NO_OP_TOOL_NAME = "no_op"


@after_model(tools=[], name="EnsureNonEmptyResponseMiddleware")
async def ensure_non_empty_response(state: AgentState, runtime) -> dict[str, Any] | Command | None:
    """
    Middleware that catches empty LLM responses (no content and no tool calls)
    and injects a no-op tool call to prompt the LLM to try again.
    """
    last_msg = state["messages"][-1]
    has_content = bool(last_msg.text())
    has_tool_calls = bool(last_msg.tool_calls)

    if has_content or has_tool_calls:
        return None

    logger.warning("LLM returned an empty response, injecting no_op tool call to retry.")

    tc_id = str(uuid4())
    patched_msg = last_msg.model_copy(update={"tool_calls": [{"name": NO_OP_TOOL_NAME, "args": {}, "id": tc_id}]})
    no_op_tool_msg = ToolMessage(
        content=(
            "No operation performed. "
            "Your previous response was empty. "
            "Please continue with the task, ensuring you call at least one tool "
            "or provide a text response."
        ),
        name=NO_OP_TOOL_NAME,
        tool_call_id=tc_id,
    )

    return {"messages": [patched_msg, no_op_tool_msg]}
