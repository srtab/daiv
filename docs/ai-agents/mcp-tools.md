# 🔧 MCP Tools

## What are MCP Tools?

MCP (Model Context Protocol) Tools are external services that extend the capabilities of DAIV agents by providing specialized functionality through a standardized protocol. These tools allow AI agents to interact with external systems, fetch data from various sources, and perform actions that go beyond basic code analysis and modification.

## Available MCP Tools

DAIV currently supports the following MCP tools:

| MCP Server | Tools | Use Cases |
|------------|--------------|-----------|
| [Sentry MCP Server](https://www.npmjs.com/package/@sentry/mcp-server) | • `find_organizations`: Discover Sentry organizations<br>• `get_issue_details`: Retrieve detailed information about specific issues | • Analyzing error patterns and crash reports<br>• Understanding issue context when fixing bugs<br>• Gathering debugging information from production systems<br>• Correlating code changes with error occurrences |

## Configuration

MCP tools are configured through environment variables. Here's how to set them up:

### Basic Configuration

```bash
# MCP Proxy Configuration
MCP_PROXY_HOST=http://mcp-proxy:9090         # Default: http://mcp-proxy:9090
MCP_PROXY_ADDR=:9090                         # Default: :9090
MCP_PROXY_AUTH_TOKEN=your-auth-token         # Optional authentication token

# Sentry MCP Server
MCP_SENTRY_ENABLED=true                      # Default: true
MCP_SENTRY_VERSION=0.17.1                    # Default: 0.17.1
MCP_SENTRY_ACCESS_TOKEN=your-sentry-token    # Required for Sentry functionality
MCP_SENTRY_HOST=your-sentry-host             # Your Sentry instance host
```

See [Environment Variables Reference](../configuration/env-config.md#mcp-tools) for more details.

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

DAIV uses [MCP Proxy](https://github.com/TBXark/mcp-proxy) to allow installing and running MCP servers on a containerized environment. This means that the MCP servers are not directly installed/running on your machine, but rather on the MCP Proxy docker container, improving security by isolating the MCP servers from your machine.

This doesn't mean that you're safe from attacks. You still need to be careful about the MCP servers you use [following the MCP protocol security best practices](https://modelcontextprotocol.io/specification/draft/basic/security_best_practices).

Here are some best practices to follow when using MCP servers in DAIV:

- **API Tokens**: Store sensitive tokens like `MCP_SENTRY_ACCESS_TOKEN` securely using Docker secrets
- **Network Access**: MCP servers may require network access to external services (e.g. Sentry)
- **Authentication**: Configure `MCP_PROXY_AUTH_TOKEN` for additional security in production environments

## Additional Resources

- [MCP Protocol Specification](https://spec.modelcontextprotocol.io/)
- [Fetch MCP Server Documentation](https://pypi.org/project/mcp-server-fetch/)
- [Sentry MCP Server Documentation](https://www.npmjs.com/package/@sentry/mcp-server)
- [Environment Variables Reference](../configuration/env-config.md)
