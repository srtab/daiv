from io import StringIO
from pathlib import Path
from tempfile import NamedTemporaryFile
from unittest.mock import patch

from django.core.management import call_command

import pytest

from automation.agents.mcp.schemas import CommonOptions, McpConfiguration, McpProxyConfig, StdioMcpServer


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


@patch("automation.agents.mcp.schemas.McpConfiguration.populate")
def test_mcp_proxy_config_prints_to_stdout_by_default(mock_populate, mock_mcp_configuration):
    """Test that the command prints JSON configuration to stdout when no output file is specified."""
    mock_populate.return_value = mock_mcp_configuration

    out = StringIO()
    call_command("mcp_proxy_config", stdout=out)

    output = out.getvalue()
    mock_populate.assert_called_once()

    # Verify the output is valid JSON with expected structure
    assert '"mcpProxy"' in output
    assert '"mcpServers"' in output
    assert '"baseURL": "http://localhost:9090"' in output
    assert '"test_server"' in output
    assert '"test-command"' in output


@patch("automation.agents.mcp.schemas.McpConfiguration.populate")
def test_mcp_proxy_config_writes_to_file(mock_populate, mock_mcp_configuration):
    """Test that the command writes JSON configuration to specified output file."""
    mock_populate.return_value = mock_mcp_configuration

    with NamedTemporaryFile(mode="w", delete=False, suffix=".json") as temp_file:
        temp_path = Path(temp_file.name)

    try:
        call_command("mcp_proxy_config", output=temp_path)

        mock_populate.assert_called_once()

        # Verify file was created and contains expected content
        assert temp_path.exists()
        content = temp_path.read_text()

        assert '"mcpProxy"' in content
        assert '"mcpServers"' in content
        assert '"baseURL": "http://localhost:9090"' in content
        assert '"test_server"' in content
        assert '"test-command"' in content
    finally:
        # Clean up
        if temp_path.exists():
            temp_path.unlink()


@patch("automation.agents.mcp.schemas.McpConfiguration.populate")
def test_mcp_proxy_config_uses_alias_names(mock_populate, mock_mcp_configuration):
    """Test that the JSON output uses proper alias names (camelCase)."""
    mock_populate.return_value = mock_mcp_configuration

    out = StringIO()
    call_command("mcp_proxy_config", stdout=out)

    output = out.getvalue()

    # Verify aliases are used instead of field names
    assert '"mcpProxy"' in output  # not "mcp_proxy"
    assert '"mcpServers"' in output  # not "mcp_servers"
    assert '"baseURL"' in output  # not "base_url"
    assert '"authTokens"' in output  # not "auth_tokens"
    assert '"panicIfInvalid"' in output  # not "panic_if_invalid"
    assert '"logEnabled"' in output  # not "log_enabled"
