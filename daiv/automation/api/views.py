from django.http import HttpRequest  # noqa: TC002

from ninja import Router

from automation.agents.tools.mcp.schemas import McpConfiguration

from .security import AuthBearer

router = Router(auth=AuthBearer(), tags=["mcp"])


@router.get("/mcp-proxy/config")
async def get_mcp_proxy_config(request: HttpRequest):
    """
    Get the MCP proxy configuration.

    This endpoint serves the MCP proxy configuration dynamically.
    Authentication is required via API key.
    """
    config = McpConfiguration.populate()
    return config.model_dump(by_alias=True, exclude_none=True)
