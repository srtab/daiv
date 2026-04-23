# MCP Endpoint

DAIV exposes a [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server endpoint, allowing AI coding assistants to delegate tasks to the DAIV agent — directly from your editor or terminal.

The DAIV agent can read and modify code, run commands in a sandbox, create commits and branches, open merge requests or pull requests, and debug CI/CD pipelines. Through the MCP endpoint, your local assistant can offload these tasks to DAIV and get the results back.

Authentication is handled via OAuth 2.0 — on first use a browser window opens for you to log in with your existing DAIV account. Your client manages tokens and refreshes them automatically.

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

## Available tools

| Tool | Description |
|------|-------------|
| `submit_job` | Submit a prompt to the DAIV agent for a repository. Returns a `job_id` for polling, or set `wait=True` to block until the result is ready (up to 10 minutes). |
| `get_job_status` | Get the status and result of a previously submitted job. Also supports `wait=True` to block until completion. |

`submit_job` accepts an optional `ref` parameter to target a specific branch or commit. If omitted, the repository's default branch is used. Set `use_max=True` to use the more capable model with thinking set to high.

For the full request/response schema and job lifecycle, see the [Jobs API](jobs-api.md).

## Usage examples

Once connected, you can interact with DAIV naturally from your AI coding assistant:

- *"Ask DAIV to refactor the authentication module in mygroup/myproject to use JWT tokens"*
- *"Submit a job to mygroup/myproject on the develop branch: fix the broken CI pipeline"*
- *"Check the status of my last DAIV job"*

!!! tip
    Be specific in your prompts — include file paths, function names, error messages, or branch names. The more context you give, the better the result.
