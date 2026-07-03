# MCP Tools

[Model Context Protocol (MCP)](https://modelcontextprotocol.io/) tools extend DAIV's agent with
access to external services. MCP servers are managed from the dashboard at
**Settings → MCP Servers** (`/dashboard/mcp-servers/`): each server is a row with a URL, a
transport (`http` or `sse`), optional HTTP headers (stored encrypted, or referenced from an env
var), and an optional tool filter. Connections are opened by the DAIV app itself at runtime — no
sidecar containers are required.

## Built-in servers

DAIV seeds two built-in servers pointing at the official remote MCP endpoints. Built-in rows are
fully editable (URL, headers, tool filter) but cannot be renamed or deleted — only disabled.

### Sentry

Seeded with `https://mcp.sentry.dev/mcp?disable-skills=seer,docs,project-management` and
**disabled** by default, because the endpoint requires authentication:

1. Create a [Sentry User Auth Token](https://docs.sentry.io/account/auth-tokens/) with read scopes
   (`org:read`, `project:read`, `team:read`, `event:read`).
2. Edit the `sentry` server and add a header: name `Authorization`, value
   `Sentry-Bearer <your token>` (literal values are encrypted at rest; alternatively use an
   `env_ref` to a variable holding that full string).
3. Click **Test connection**, adjust the tool filter if needed, save, and enable the server.

The seeded tool filter allows only read-only tools. `Sentry-Bearer` (not `Bearer`) is Sentry's
scheme for passing an API token directly to their hosted MCP.

### Context7

Seeded with `https://mcp.context7.com/mcp` and **enabled** by default — it works without
credentials at low rate limits. To raise them, add a header: name `CONTEXT7_API_KEY`, value your
API key from [context7.com/dashboard](https://context7.com/dashboard).

## On-premise Sentry

Sentry's hosted MCP only serves sentry.io. The official
[`@sentry/mcp-server`](https://www.npmjs.com/package/@sentry/mcp-server) package is stdio-only, so
for a self-hosted Sentry you expose it over HTTP yourself and point the `sentry` row's URL at it.
Two options:

**Option A — stdio bridge container** (any stdio→HTTP bridge works; this example uses
[supergateway](https://github.com/supercorp-ai/supergateway)):

```yaml
mcp-sentry:
  image: supercorp/supergateway:latest
  restart: unless-stopped
  command:
    - --stdio
    - "npx @sentry/mcp-server@latest --host=sentry.example.com"
    - --outputTransport
    - streamableHttp
    - --healthEndpoint
    - "/healthz"
    - --stateful
    - --sessionTimeout
    - "300000"
  environment:
    SENTRY_ACCESS_TOKEN: ${SENTRY_ACCESS_TOKEN}
  healthcheck:
    test: ["CMD", "wget", "--spider", "-q", "http://localhost:8000/healthz"]
    interval: 30s
    timeout: 5s
    retries: 3
    start_period: 30s
```

Then edit the `sentry` server row: set the URL to `http://mcp-sentry:8000/mcp` and remove the
`Authorization` header (the bridge authenticates via its own `SENTRY_ACCESS_TOKEN`). Add
`--insecure-http` to the `npx` command for plain-HTTP Sentry installs.

**Option B — self-deploy Sentry's worker.** The
[sentry-mcp](https://github.com/getsentry/sentry-mcp) repository ships the open-source Cloudflare
worker behind mcp.sentry.dev. Deploying your own instance gives you a bridge-free HTTP endpoint;
keep the `Authorization: Sentry-Bearer <token>` header in the row.

!!! important
    When running a stdio bridge, always use **stateful mode** (`--stateful`). In stateless mode
    every tool call spawns a fresh child process chain, which can exhaust the system's thread
    limit under concurrent load. Use `--sessionTimeout` (milliseconds; `300000` works well) to
    clean up idle sessions.

## Custom servers

Add any HTTP- or SSE-reachable MCP server from **MCP Servers → New server**:

| Field | Description |
|-------|-------------|
| Name | Unique slug (lowercase, dashes) |
| Transport | `http` (streamable HTTP) or `sse` |
| URL | The MCP endpoint URL |
| Headers | Per-header: literal value (encrypted at rest) or `env_ref` (resolved from the environment at runtime) |
| Tool filter | `allow`/`block` a list of tool names |

**Test connection** probes the server and, on success, turns the tool-filter field into
checkboxes listing the discovered tools. Tool discovery also runs on the server's detail and edit
pages once it is saved.

### Tool filtering

- **`allow`** — only the listed tools are exposed.
- **`block`** — all tools except the listed ones are exposed.

Unknown names in an allow-list fail closed: a tool that disappears upstream simply stops being
exposed. If no filter is set, all tools from the server are available.

### Stdio-only servers

DAIV connects over HTTP/SSE only. To use a stdio-based MCP server, wrap it with a stdio→HTTP
bridge in its own container (see the on-premise Sentry example above) and add the bridge URL as a
custom server. Run one bridge container per MCP server for isolation.

## Security considerations

- **Secrets in headers** are encrypted at rest and never rendered back into the form; `env_ref`
  headers keep the value out of the database entirely.
- **Read-only filters** — prefer allow-lists that exclude mutating tools, as the seeded Sentry
  filter does.
- **Network** — MCP connections originate from the DAIV app/worker containers; the sandbox egress
  proxy does not apply to them. Restrict outbound access at your network layer if needed.
