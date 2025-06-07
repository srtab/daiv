# 🔧 MCP Tools

## What are MCP Tools?

MCP (Model Context Protocol) Tools are external services that extend the capabilities of DAIV agents by providing specialized functionality through a standardized protocol. These tools allow AI agents to interact with external systems, fetch data from various sources, and perform actions that go beyond basic code analysis and modification.

## Available MCP Tools

DAIV currently supports the following MCP tools:

### 1. Fetch MCP Server

The Fetch MCP server provides web scraping and HTTP request capabilities, allowing agents to retrieve content from web pages and APIs.

**Capabilities:**

- Fetch content from URLs
- Perform HTTP GET/POST requests
- Extract structured data from web pages
- Handle various content types (HTML, JSON, text)

**Use Cases:**

- Researching documentation and examples from the web
- Fetching configuration files or data from remote sources
- Analyzing external APIs and their responses
- Gathering context from online resources

### 2. Sentry MCP Server

The Sentry MCP server integrates with Sentry.io to provide error monitoring and issue tracking capabilities.

**Available Tools:**

- `find_organizations`: Discover Sentry organizations
- `get_issue_details`: Retrieve detailed information about specific issues

**Use Cases:**

- Analyzing error patterns and crash reports
- Understanding issue context when fixing bugs
- Gathering debugging information from production systems
- Correlating code changes with error occurrences

## Configuration

MCP tools are configured through environment variables. Here's how to set them up:

### Basic Configuration

```bash
# MCP Proxy Configuration
MCP_PROXY_HOST=http://mcp-proxy:9090         # Default: http://mcp-proxy:9090
MCP_PROXY_ADDR=:9090                         # Default: :9090
MCP_PROXY_AUTH_TOKEN=your-auth-token         # Optional authentication token

# Fetch MCP Server
MCP_FETCH_ENABLED=true                       # Default: true
MCP_FETCH_VERSION=2025.4.7                   # Default: 2025.4.7

# Sentry MCP Server
MCP_SENTRY_ENABLED=true                      # Default: true
MCP_SENTRY_VERSION=0.10.0                    # Default: 0.10.0
MCP_SENTRY_ACCESS_TOKEN=your-sentry-token    # Required for Sentry functionality
MCP_SENTRY_HOST=your-sentry-host             # Your Sentry instance host
```

See [Environment Variables Reference](../getting-started/environment-variables.md#mcp-tools) for more details.

## Agent Integration

### Which Agents Use MCP Tools?

Currently, MCP tools are available in the following agents:

#### Plan and Execute Agent

The Plan and Execute agent has access to all configured MCP tools through the `MCPToolkit`. This agent can:

- Use the Fetch server to research solutions online
- Access Sentry to understand error contexts when fixing issues
- Combine MCP tools with repository tools for comprehensive problem-solving

**Example Usage:**

When addressing an issue, the agent might:

1. Use Sentry tools to analyze error details
2. Use Fetch tools to research similar issues or documentation
3. Apply repository tools to implement the fix


## Advanced Configuration

### Creating Custom MCP Servers

!!! warning "Coming Soon"
    The ability to create custom MCP servers is currently under development. This feature will allow you to define custom MCP servers.

    Stay tuned for updates as we work on bringing this functionality to DAIV.

## Troubleshooting

### Common Issues

**MCP tools not available in agents:**

- Verify that the MCP proxy is running and accessible
- Check that required environment variables are set

**Sentry tools not working:**

- Verify `MCP_SENTRY_ACCESS_TOKEN` is set and valid
- Check that `MCP_SENTRY_HOST` points to your Sentry instance
- Ensure your Sentry token has the necessary permissions

**Fetch tools timing out:**

- Check network connectivity from the MCP proxy
- Verify target URLs are accessible

### Debugging

To debug MCP tool issues:

1. **Check MCP proxy logs:**
   ```bash
   docker logs mcp-proxy
   ```

2. **Verify configuration:**
   ```bash
   docker compose exec -it app django-admin mcp_proxy_config
   ```

## Security Considerations

- **API Tokens**: Store sensitive tokens like `MCP_SENTRY_ACCESS_TOKEN` securely using Docker secrets
- **Network Access**: MCP servers may require network access to external services
- **Authentication**: Configure `MCP_PROXY_AUTH_TOKEN` for additional security in production environments

## Additional Resources

- [MCP Protocol Specification](https://spec.modelcontextprotocol.io/)
- [Fetch MCP Server Documentation](https://pypi.org/project/mcp-server-fetch/)
- [Sentry MCP Server Documentation](https://www.npmjs.com/package/@sentry/mcp-server)
- [Environment Variables Reference](../getting-started/environment-variables.md)
