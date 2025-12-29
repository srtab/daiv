from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware import AgentMiddleware

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain_core.messages import ToolMessage
    from langgraph.prebuilt.tool_node import ToolCallRequest
    from langgraph.types import Command


logger = logging.getLogger("daiv.tools")


DEFAULT_MAX_VALUE_CHARS = 100


class ToolCallLoggingMiddleware(AgentMiddleware):
    """
    Middleware to log all tool calls.
    """

    def __init__(self, *, max_value_chars: int = DEFAULT_MAX_VALUE_CHARS) -> None:
        """
        Initialize the middleware.

        Args:
            max_value_chars: The maximum number of characters to log for each value.
        """
        self.max_value_chars = max_value_chars

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]]
    ) -> ToolMessage | Command:
        """
        Wrap the tool call and log the tool call.

        Args:
            request: The tool call request.
            handler: The handler function to call with the modified request.

        Returns:
            ToolMessage | Command: The result of the tool call.
        """
        tool_name = self._tool_name(request)
        tool_call_id = self._tool_call_id(request)
        tool_args = request.tool_call.get("args") if isinstance(request.tool_call, dict) else None

        logger.info("[%s] Tool call (id=%s, %s)", tool_name, tool_call_id, self._format_args(tool_args))

        return await handler(request)

    def _format_args(self, value: Any) -> str:
        """
        Format the arguments for logging.

        Args:
            value: The value to format.

        Returns:
            The formatted value.
        """
        if isinstance(value, dict):
            items = []
            for k, v in value.items():
                if isinstance(v, str):
                    if len(v) <= self.max_value_chars:
                        items.append(f"{k}={v}")
                    else:
                        items.append(f"{k}={v[: self.max_value_chars]}...(truncated)")
                else:
                    items.append(f"{k}={repr(v)}")
            return ", ".join(items)
        return ""

    def _tool_name(self, request: ToolCallRequest) -> str:
        """
        Get the name of the tool.

        Args:
            request: The tool call request.

        Returns:
            The name of the tool.
        """
        if isinstance(request.tool_call, dict) and request.tool_call.get("name"):
            return str(request.tool_call["name"])
        if request.tool is not None and getattr(request.tool, "name", None):
            return str(request.tool.name)
        return "<unknown-tool>"

    def _tool_call_id(self, request: ToolCallRequest) -> str | None:
        """
        Get the ID of the tool call.

        Args:
            request: The tool call request.

        Returns:
            The ID of the tool call.
        """
        if isinstance(request.tool_call, dict):
            tool_call_id = request.tool_call.get("id")
            return str(tool_call_id) if tool_call_id is not None else None
        return None
