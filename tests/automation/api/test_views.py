from unittest.mock import patch

import pytest
from ninja.testing import TestAsyncClient

from accounts.models import APIKey
from automation.agents.tools.mcp.schemas import CommonOptions, McpConfiguration, McpProxyConfig, StdioMcpServer
from daiv.api import api


@pytest.fixture
def mock_mcp_configuration():
    """Create a mock MCP configuration for testing."""
    return McpConfiguration(
        mcp_proxy=McpProxyConfig(
            base_url="http://localhost:9090",
            addr=":9090",
            name="test-proxy",
            version="1.0.0",
            options=CommonOptions(auth_tokens=["test-token"], panic_if_invalid=False, log_enabled=True),
        ),
        mcp_servers={"test_server": StdioMcpServer(command="test-command")},
    )


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_get_mcp_proxy_config_requires_authentication():
    """Test that the endpoint returns 401/403 when no authentication is provided."""
    client = TestAsyncClient(api)
    response = await client.get("/automation/mcp-proxy/config/")

    assert response.status_code in [401, 403]


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_get_mcp_proxy_config_with_valid_api_key(django_user_model, mock_mcp_configuration):
    """Test that the endpoint returns 200 with valid API key."""
    # Create user and API key
    user = await django_user_model.objects.acreate(username="testuser_valid_key", email="test@example.com")
    api_key_instance = await APIKey.objects.create_key(name="test-key", user=user)

    with patch("automation.agents.tools.mcp.schemas.McpConfiguration.populate") as mock_populate:
        mock_populate.return_value = mock_mcp_configuration

        client = TestAsyncClient(api)
        response = await client.get(
            "/automation/mcp-proxy/config/", headers={"Authorization": f"Bearer {api_key_instance[1]}"}
        )

        assert response.status_code == 200
        mock_populate.assert_called_once()

    await user.adelete()


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_get_mcp_proxy_config_returns_valid_json(django_user_model, mock_mcp_configuration):
    """Test that the response structure matches McpConfiguration schema."""
    # Create user and API key
    user = await django_user_model.objects.acreate(username="testuser_valid_json", email="test@example.com")
    api_key_instance = await APIKey.objects.create_key(name="test-key", user=user)

    with patch("automation.agents.tools.mcp.schemas.McpConfiguration.populate") as mock_populate:
        mock_populate.return_value = mock_mcp_configuration

        client = TestAsyncClient(api)
        response = await client.get(
            "/automation/mcp-proxy/config/", headers={"Authorization": f"Bearer {api_key_instance[1]}"}
        )

        assert response.status_code == 200
        data = response.json()

        # Verify structure matches schema
        assert "mcpProxy" in data
        assert "mcpServers" in data
        assert "baseURL" in data["mcpProxy"]
        assert "addr" in data["mcpProxy"]
        assert "name" in data["mcpProxy"]
        assert "version" in data["mcpProxy"]
        assert "test_server" in data["mcpServers"]

    await user.adelete()


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_get_mcp_proxy_config_uses_aliases(django_user_model, mock_mcp_configuration):
    """Test that the response uses camelCase field names (aliases)."""
    # Create user and API key
    user = await django_user_model.objects.acreate(username="testuser_aliases", email="test@example.com")
    api_key_instance = await APIKey.objects.create_key(name="test-key", user=user)

    with patch("automation.agents.tools.mcp.schemas.McpConfiguration.populate") as mock_populate:
        mock_populate.return_value = mock_mcp_configuration

        client = TestAsyncClient(api)
        response = await client.get(
            "/automation/mcp-proxy/config/", headers={"Authorization": f"Bearer {api_key_instance[1]}"}
        )

        assert response.status_code == 200
        data = response.json()

        # Verify aliases are used (camelCase)
        assert "mcpProxy" in data
        assert "mcpServers" in data
        assert "baseURL" in data["mcpProxy"]
        assert "authTokens" in data["mcpProxy"]["options"]
        assert "panicIfInvalid" in data["mcpProxy"]["options"]
        assert "logEnabled" in data["mcpProxy"]["options"]

        # Verify snake_case is NOT used
        response_str = response.content.decode()
        assert "mcp_proxy" not in response_str
        assert "mcp_servers" not in response_str
        assert "base_url" not in response_str
        assert "auth_tokens" not in response_str

    await user.adelete()
