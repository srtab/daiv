from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware import AgentMiddleware

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain_core.messages import ToolMessage
    from langgraph.prebuilt.tool_node import ToolCallRequest
    from langgraph.types import Command


logger = logging.getLogger("daiv.tools")


class ToolCallLoggingMiddleware(AgentMiddleware):
    """
    Middleware to log all tool calls (start/end, duration, and errors).
    """

    def __init__(self, *, max_value_chars: int = 0) -> None:
        self.max_value_chars = max_value_chars

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]]
    ) -> ToolMessage | Command:
        """
        Wrap the tool call and log the start and end of the tool call.

        Args:
            request: The tool call request.
            handler: The handler function to call with the modified request.

        Returns:
            ToolMessage | Command: The result of the tool call.
        """
        tool_name = self._tool_name(request)
        tool_call_id = self._tool_call_id(request)
        tool_args = request.tool_call.get("args") if isinstance(request.tool_call, dict) else None

        start = time.perf_counter()
        logger.info("[%s] Tool call started (id=%s, args=%s)", tool_name, tool_call_id, self._preview(tool_args))

        try:
            result = await handler(request)
        except Exception:
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            logger.exception("[%s] Tool call failed (id=%s, duration_ms=%d)", tool_name, tool_call_id, elapsed_ms)
            raise

        elapsed_ms = int((time.perf_counter() - start) * 1000)

        if hasattr(result, "content"):
            # ToolMessage
            status = getattr(result, "status", None)
            logger.info(
                "[%s] Tool call finished (id=%s, status=%s, duration_ms=%d)",
                tool_name,
                tool_call_id,
                status,
                elapsed_ms,
            )
        else:
            # Command (state update / jump)
            goto = getattr(result, "goto", None)
            logger.info(
                "[%s] Tool call finished (id=%s, duration_ms=%d, goto=%s)", tool_name, tool_call_id, elapsed_ms, goto
            )

        return result

    def _preview(self, value: Any) -> str:
        try:
            rendered = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            rendered = repr(value)
        if len(rendered) <= self.max_value_chars:
            return rendered
        return rendered[: self.max_value_chars] + "...(truncated)"

    def _tool_name(self, request: ToolCallRequest) -> str:
        if isinstance(request.tool_call, dict) and request.tool_call.get("name"):
            return str(request.tool_call["name"])
        if request.tool is not None and getattr(request.tool, "name", None):
            return str(request.tool.name)
        return "<unknown-tool>"

    def _tool_call_id(self, request: ToolCallRequest) -> str | None:
        if isinstance(request.tool_call, dict):
            tool_call_id = request.tool_call.get("id")
            return str(tool_call_id) if tool_call_id is not None else None
        return None
