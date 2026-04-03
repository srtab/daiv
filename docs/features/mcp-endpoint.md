# MCP Endpoint

DAIV exposes a [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server endpoint, allowing AI coding assistants to submit jobs and check results — all from within your editor or terminal.

No API keys needed — on first use, a browser window opens for you to log in with your existing DAIV account.

## Getting started

### Claude Code

```bash
claude mcp add daiv --transport http https://daiv.example.com/mcp/
```

### Cursor

Add to `.cursor/mcp.json` (project) or `~/.cursor/mcp.json` (global):

```json
{
  "mcpServers": {
    "daiv": {
      "type": "streamable-http",
      "url": "https://daiv.example.com/mcp/"
    }
  }
}
```

### Codex CLI

Add to `.codex/config.toml` (project) or `~/.codex/config.toml` (global):

```toml
[mcp_servers.daiv]
url = "https://daiv.example.com/mcp/"
```

!!! tip
    Any MCP client that supports Streamable HTTP transport can connect to DAIV using the same `/mcp/` URL.

## How it works

1. **Add the server** — configure your MCP client with the DAIV URL (see above)
2. **Log in** — on first use, a browser window opens for you to authorize access
3. **Use DAIV tools** — submit jobs, check status, and list repositories directly from your editor

Authentication is handled automatically via OAuth 2.0 — your client manages tokens and refreshes them as needed.

## Available tools

The MCP endpoint exposes the same capabilities as the [Jobs API](jobs-api.md):

| Tool | Description |
|------|-------------|
| `submit_job` | Submit a prompt to the DAIV agent for a repository |
| `get_job_status` | Poll the status and result of a submitted job |

## Usage examples

Once connected, you can interact with DAIV naturally from your AI coding assistant:

- *"Submit a job to mygroup/myproject: refactor the authentication module to use JWT tokens"*
- *"Check the status of my last job"*
- *"Ask DAIV to fix the broken CI pipeline in mygroup/myproject on the develop branch"*
