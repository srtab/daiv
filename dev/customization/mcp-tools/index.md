# MCP Tools

[Model Context Protocol (MCP)](https://modelcontextprotocol.io/) tools extend DAIV's agent with access to external services. Each MCP server runs in its own isolated container via [supergateway](https://github.com/supercorp-ai/supergateway), ensuring that a compromised or misbehaving server cannot affect others.

## Built-in MCP servers

### Sentry

The [Sentry MCP Server](https://www.npmjs.com/package/@sentry/mcp-server) gives DAIV access to your error tracking data.

**Available tools:**

| Tool                 | Description                                     |
| -------------------- | ----------------------------------------------- |
| `find_organizations` | Discover Sentry organizations                   |
| `find_projects`      | List projects in an organization                |
| `search_issues`      | Search for issues by query                      |
| `search_events`      | Search for events                               |
| `get_issue_details`  | Get detailed information about a specific issue |

**Use cases:** Analyzing error patterns when fixing bugs, correlating code changes with production errors, gathering debugging context.

**Configuration:**

```
MCP_SENTRY_URL=http://mcp-sentry:8000/sse   # Default; set to None to disable
```

`SENTRY_ACCESS_TOKEN` is consumed by the `mcp-sentry` container — add it to your secrets env file. `SENTRY_HOST` is set as a regular environment variable on the container.

### Context7

The [Context7 MCP Server](https://www.npmjs.com/package/@upstash/context7-mcp) provides up-to-date library documentation lookup.

**Available tools:**

| Tool                 | Description                                |
| -------------------- | ------------------------------------------ |
| `resolve-library-id` | Resolve a library name to its Context7 ID  |
| `query-docs`         | Query documentation for a specific library |

**Use cases:** Looking up current API documentation, finding code examples for libraries used in the project.

**Configuration:**

```
MCP_CONTEXT7_URL=http://mcp-context7:8000/sse  # Default; set to None to disable
```

Context7 credentials (`CONTEXT7_API_KEY`) are consumed by the `mcp-context7` container. Add them to your secrets env file.

## User-defined MCP servers

DAIV can connect to any MCP server that implements the [Model Context Protocol](https://modelcontextprotocol.io/). If you need to build a custom server, see the [MCP server documentation](https://modelcontextprotocol.io/docs/concepts/servers). This section covers how to connect an existing server to DAIV.

Provide a JSON config file following the [Claude Code `.mcp.json` standard](https://docs.anthropic.com/en/docs/claude-code/mcp). Set the file path via the `MCP_SERVERS_CONFIG_FILE` environment variable.

Only `sse` and `http` (streamable HTTP) transport types are supported, since user MCP servers must be network-accessible.

Note

When running in Docker, the config file must be accessible inside the container. Mount it as a volume and set `MCP_SERVERS_CONFIG_FILE` to the container path:

```
# docker-compose.yml
app:
  volumes:
    - ./mcp.json:/home/app/mcp.json:ro
  environment:
    MCP_SERVERS_CONFIG_FILE: /home/app/mcp.json
```

### Config file format

```
{
  "mcpServers": {
    "my-internal-api": {
      "type": "sse",
      "url": "http://my-mcp-host:8080/sse",
      "headers": {
        "Authorization": "Bearer ${MY_API_TOKEN}"
      }
    },
    "another-service": {
      "type": "http",
      "url": "http://another-host:9000/mcp",
      "headers": {
        "X-Api-Key": "${ANOTHER_API_KEY}"
      },
      "toolFilter": {
        "mode": "allow",
        "list": ["search", "get_document"]
      }
    }
  }
}
```

Each server entry supports:

| Field        | Required | Description                                       |
| ------------ | -------- | ------------------------------------------------- |
| `type`       | Yes      | Transport type: `sse` or `http` (streamable HTTP) |
| `url`        | Yes      | URL of the MCP server                             |
| `headers`    | No       | HTTP headers (supports env var expansion)         |
| `toolFilter` | No       | Restrict which tools are exposed (see below)      |

### Tool filtering

Use `toolFilter` to control which tools from a server are available to the agent:

- **`mode: "allow"`** — only tools in the `list` are exposed
- **`mode: "block"`** — all tools except those in the `list` are exposed

```
{
  "toolFilter": {
    "mode": "allow",
    "list": ["tool_a", "tool_b"]
  }
}
```

If `toolFilter` is omitted, all tools from the server are available.

### Environment variable expansion

The `url` and `headers` values support environment variable expansion:

- `${VAR}` — replaced with the value of `VAR`, kept as-is if unset
- `${VAR:-default}` — replaced with the value of `VAR`, or `default` if unset

This allows you to keep secrets out of the config file and inject them via environment variables.

## Running custom MCP servers

To add a custom stdio-based MCP server, wrap it with [supergateway](https://github.com/supercorp-ai/supergateway) in its own container. Each MCP server should run in a separate container for security isolation.

Example `docker-compose.yml` service:

```
mcp-my-tool:
  image: supercorp/supergateway:latest
  restart: unless-stopped
  container_name: daiv-mcp-my-tool
  command:
    - --stdio
    - "npx my-mcp-server@latest"
    - --healthEndpoint
    - "/healthz"
  environment:
    MY_TOOL_API_KEY: ${MY_TOOL_API_KEY:-}
  healthcheck:
    test: ["CMD", "wget", "--spider", "-q", "http://localhost:8000/healthz"]
    interval: 30s
    timeout: 5s
    retries: 3
    start_period: 30s
```

Then reference it in your config file:

```
{
  "mcpServers": {
    "my-tool": {
      "type": "sse",
      "url": "http://mcp-my-tool:8000/sse"
    }
  }
}
```

## Security considerations

- **One container per MCP server** — each MCP runs in its own isolated container, so a vulnerability in one cannot compromise others
- **Environment variable expansion** — use `${VAR}` syntax to inject secrets from the environment instead of hardcoding them in config files
- **Store tokens securely** — use Docker secrets for sensitive values like access tokens
- **Network segmentation** — MCP containers only need to reach the services they interact with; consider restricting their network access
- **Review server permissions** — MCP servers may require network access to external services
