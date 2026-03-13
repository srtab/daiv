# MCP Tools

[Model Context Protocol (MCP)](https://modelcontextprotocol.io/) tools extend DAIV's agent with access to external services. MCP servers run in an isolated container via [MCP Proxy](https://github.com/TBXark/mcp-proxy), keeping them separate from your application.

## Available MCP servers

### Sentry

The [Sentry MCP Server](https://www.npmjs.com/package/@sentry/mcp-server) gives DAIV access to your error tracking data.

**Available tools:**

| Tool | Description |
|------|-------------|
| `find_organizations` | Discover Sentry organizations |
| `find_projects` | List projects in an organization |
| `search_issues` | Search for issues by query |
| `search_events` | Search for events |
| `get_issue_details` | Get detailed information about a specific issue |

**Use cases:** Analyzing error patterns when fixing bugs, correlating code changes with production errors, gathering debugging context.

**Configuration:**

```bash
MCP_SENTRY_ENABLED=true                     # Default: true
MCP_SENTRY_ACCESS_TOKEN=your-sentry-token   # Required
MCP_SENTRY_HOST=your-sentry-host            # Your Sentry instance URL
MCP_SENTRY_VERSION=0.20.0                   # Default: 0.20.0
```

### Context7

The [Context7 MCP Server](https://www.npmjs.com/package/@upstash/context7-mcp) provides up-to-date library documentation lookup.

**Available tools:**

| Tool | Description |
|------|-------------|
| `resolve-library-id` | Resolve a library name to its Context7 ID |
| `query-docs` | Query documentation for a specific library |

**Use cases:** Looking up current API documentation, finding code examples for libraries used in the project.

**Configuration:**

```bash
MCP_CONTEXT7_ENABLED=true                   # Default: true
MCP_CONTEXT7_API_KEY=your-api-key           # Optional
MCP_CONTEXT7_VERSION=latest                 # Default: latest
```

## Proxy configuration

All MCP servers run through a shared proxy:

```bash
MCP_PROXY_HOST=http://mcp-proxy:9090       # Default
MCP_PROXY_ADDR=:9090                        # Address the proxy listens on
MCP_PROXY_AUTH_TOKEN=your-auth-token        # Optional authentication
```

## Custom MCP servers

!!! note "Coming soon"
    Custom MCP server support is on the roadmap. You'll be able to register your own MCP servers to give DAIV access to internal tools and services.

## Security considerations

MCP servers run in an isolated Docker container, but you should still follow [MCP security best practices](https://modelcontextprotocol.io/specification/draft/basic/security_best_practices):

- **Store tokens securely** — use Docker secrets for sensitive values like `MCP_SENTRY_ACCESS_TOKEN`
- **Configure authentication** — set `MCP_PROXY_AUTH_TOKEN` in production
- **Review server permissions** — MCP servers may require network access to external services
