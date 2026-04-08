import pytest
from pydantic import ValidationError

from automation.agent.mcp.schemas import ToolFilter, UserMcpServer, UserMcpServersConfig


class TestToolFilter:
    def test_allow_mode(self):
        tf = ToolFilter(mode="allow", items=["tool_a", "tool_b"])
        assert tf.mode == "allow"
        assert tf.items == ["tool_a", "tool_b"]

    def test_block_mode(self):
        tf = ToolFilter(mode="block", items=["tool_c"])
        assert tf.mode == "block"
        assert tf.items == ["tool_c"]

    def test_invalid_mode(self):
        with pytest.raises(ValidationError):
            ToolFilter(mode="invalid", items=["tool_a"])

    def test_alias_list(self):
        """Test that the 'list' alias works for the items field."""
        data = {"mode": "allow", "list": ["tool_a"]}
        tf = ToolFilter.model_validate(data)
        assert tf.items == ["tool_a"]


class TestUserMcpServer:
    def test_sse_server(self):
        server = UserMcpServer(type="sse", url="http://host:8080/sse")
        assert server.type == "sse"
        assert server.url == "http://host:8080/sse"
        assert server.headers is None

    def test_http_server_with_headers(self):
        server = UserMcpServer(type="http", url="http://host:9000/mcp", headers={"Authorization": "Bearer token"})
        assert server.type == "http"
        assert server.headers == {"Authorization": "Bearer token"}

    def test_invalid_type(self):
        with pytest.raises(ValidationError):
            UserMcpServer(type="stdio", url="http://host:8080/sse")


class TestUserMcpServersConfig:
    def test_empty_config(self):
        config = UserMcpServersConfig()
        assert config.mcp_servers == {}

    def test_config_with_servers(self):
        raw = {
            "mcpServers": {
                "my-api": {"type": "sse", "url": "http://host:8080/sse"},
                "another": {"type": "http", "url": "http://host:9000/mcp"},
            }
        }
        config = UserMcpServersConfig.model_validate(raw)
        assert len(config.mcp_servers) == 2
        assert "my-api" in config.mcp_servers
        assert config.mcp_servers["my-api"].type == "sse"
        assert config.mcp_servers["another"].type == "http"

    def test_config_with_alias(self):
        """Test that mcpServers alias is required in JSON input."""
        raw = {"mcpServers": {"test": {"type": "sse", "url": "http://host:8080/sse"}}}
        config = UserMcpServersConfig.model_validate(raw)
        assert "test" in config.mcp_servers

    def test_config_with_headers_and_env_vars(self):
        """Test that headers containing env var placeholders are preserved as-is."""
        raw = {
            "mcpServers": {
                "my-api": {
                    "type": "sse",
                    "url": "http://host:8080/sse",
                    "headers": {"Authorization": "Bearer ${MY_TOKEN}"},
                }
            }
        }
        config = UserMcpServersConfig.model_validate(raw)
        assert config.mcp_servers["my-api"].headers["Authorization"] == "Bearer ${MY_TOKEN}"
