# MCP Tools

[Model Context Protocol (MCP)](https://modelcontextprotocol.io/) tools extend DAIV's agent with access to external services. Each MCP server runs in its own isolated container via [supergateway](https://github.com/supercorp-ai/supergateway), ensuring that a compromised or misbehaving server cannot affect others.

## Built-in MCP servers

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
MCP_SENTRY_URL=http://mcp-sentry:8000/mcp   # Default; set to None to disable
```

`SENTRY_ACCESS_TOKEN` is consumed by the `mcp-sentry` container — add it to your secrets env file. `SENTRY_HOST` is set as a regular environment variable on the container.

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
MCP_CONTEXT7_URL=http://mcp-context7:8000/mcp  # Default; set to None to disable
```

Context7 credentials (`CONTEXT7_API_KEY`) are consumed by the `mcp-context7` container. Add them to your secrets env file.

## Configuring outbound MCP servers

MCP servers are managed via the admin UI at **Dashboard → MCP Servers**
(`/dashboard/mcp-servers/`). From there an administrator can:

- Add a new MCP server (HTTP or SSE transport).
- Provide headers either as **literal values** (encrypted at rest with the
  same key used elsewhere in DAIV — see `DAIV_ENCRYPTION_KEY`) or as
  **environment variable references** (entered by name, resolved at runtime).
- Configure a per-server tool filter (`allow` or `block` list). When the
  server is reachable, the filter UI shows the currently-exposed tools as
  checkboxes; otherwise it falls back to a free-text list.
- Test a connection before saving (a transient handshake is opened against
  the configured URL with a 5-second timeout).
- Enable or disable any server, including built-in ones (Sentry, Context7),
  without removing their code-defined configuration.

The DB is the source of truth — changes apply on the next agent turn; no
restart required.

### Legacy `MCP_SERVERS_CONFIG_FILE`

The `MCP_SERVERS_CONFIG_FILE` environment variable is **deprecated**. If
set, its contents are imported once into the new database table during the
`mcp_servers` 0002 migration. After that import the file is no longer
read; the env var should be unset.

Two compatibility notes for the import:

- `${VAR}` references are converted to environment-variable headers; the
  variable name is preserved.
- `${VAR:-default}` syntax is partially supported — the variable name is
  kept but the **fallback default value is dropped** (the new model does not
  support fallback defaults). The migration logs a warning naming any
  entries affected.
- Mixed-content header values like `"Bearer ${TOKEN}"` are imported as an
  environment-variable header pointing at `TOKEN` — the surrounding
  `Bearer ` prefix is **not** preserved. If your existing config relies on
  this pattern, review the imported `Authorization` headers in the UI after
  upgrade and adjust as needed.

## Running custom MCP servers

To add a custom stdio-based MCP server, wrap it with [supergateway](https://github.com/supercorp-ai/supergateway) in its own container. Each MCP server should run in a separate container for security isolation.

!!! important
    Always run supergateway in **stateful mode** (`--stateful`). In stateless mode (the default), every tool call spawns a fresh child process chain (`npx` → `npm` → `sh` → `node`), which can exhaust the system's thread limit (`kernel.threads-max`) under concurrent load. Stateful mode keeps a single long-lived child process and multiplexes all requests through it.

    Use `--sessionTimeout` to automatically clean up idle sessions (in milliseconds). A value of `300000` (5 minutes) works well for typical agent runs.

Example `docker-compose.yml` service:

```yaml
mcp-my-tool:
  image: supercorp/supergateway:latest
  restart: unless-stopped
  container_name: daiv-mcp-my-tool
  command:
    - --stdio
    - "npx my-mcp-server@latest"
    - --outputTransport
    - streamableHttp
    - --healthEndpoint
    - "/healthz"
    - --stateful
    - --sessionTimeout
    - "300000"
  environment:
    MY_TOOL_API_KEY: ${MY_TOOL_API_KEY:-}
  healthcheck:
    test: ["CMD", "wget", "--spider", "-q", "http://localhost:8000/healthz"]
    interval: 30s
    timeout: 5s
    retries: 3
    start_period: 30s
```

Then register it in the UI at **Dashboard → MCP Servers** (`/dashboard/mcp-servers/`) using transport type `http` and URL `http://mcp-my-tool:8000/mcp`.

## Security considerations

- **One container per MCP server** — each MCP runs in its own isolated container, so a vulnerability in one cannot compromise others
- **Environment variable references** — use environment-variable header references in the MCP Servers UI to inject secrets at runtime instead of storing them as plaintext
- **Store tokens securely** — use Docker secrets for sensitive values like access tokens
- **Network segmentation** — MCP containers only need to reach the services they interact with; consider restricting their network access
- **Review server permissions** — MCP servers may require network access to external services
