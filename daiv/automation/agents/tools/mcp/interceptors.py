from typing import TYPE_CHECKING

from langchain_core.tools import ToolException
from langchain_core.tools.base import _handle_tool_error, _handle_validation_error
from langchain_mcp_adapters.interceptors import ToolCallInterceptor as BaseToolCallInterceptor
from langchain_mcp_adapters.tools import MCPToolCallResult
from mcp.types import ContentBlock
from pydantic import ValidationError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain_mcp_adapters.tools import MCPToolCallRequest


class ToolCallInterceptor(BaseToolCallInterceptor):
    """
    Tool call interceptor that handle tool exceptions and validation errors.
    """

    async def __call__(
        self, request: MCPToolCallRequest, handler: Callable[[MCPToolCallRequest], Awaitable[MCPToolCallResult]]
    ) -> MCPToolCallResult:
        try:
            return await handler(request)
        except ToolException as e:
            return MCPToolCallResult(isError=True, content=[ContentBlock(text=_handle_tool_error(e, flag=True))])
        except ValidationError as e:
            return MCPToolCallResult(isError=True, content=[ContentBlock(text=_handle_validation_error(e, flag=True))])
