# MCP Endpoint

DAIV exposes a [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server endpoint, allowing AI coding assistants to delegate tasks to the DAIV agent — directly from your editor or terminal.

The DAIV agent can read and modify code, run commands in a sandbox, create commits and branches, open merge requests or pull requests, and debug CI/CD pipelines. Through the MCP endpoint, your local assistant can offload these tasks to DAIV and get the results back.

Authentication is handled via OAuth 2.0 — on first use a browser window opens for you to log in with your existing DAIV account. Your client manages tokens and refreshes them automatically.

!!! tip
    Prefer the HTTP [Jobs API](jobs-api.md) instead? It uses API key authentication, and you can create a key self-service in the dashboard at `/accounts/api-keys/` (see [Creating an API key](jobs-api.md#creating-an-api-key)).

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
| `submit_job` | Submit a prompt to the DAIV agent as a batch of jobs — one independent job per repository. Returns a `batch_id` and a `jobs` list, or set `wait=True` to block until every job in the batch completes (up to 10 minutes total). |
| `get_job_status` | Get the status and result of a previously submitted job. Also supports `wait=True` to block until completion. |
| `list_repositories` | Discover repositories accessible to DAIV, optionally filtered by `search` (partial name match) or `topics`. Results are truncated at 40 — narrow with `search` or `topics` if you hit the limit. |
| `list_environments` | List the sandbox environments visible to you (your own `USER` environments plus all `GLOBAL` ones). Use a returned `name` or `id` as `submit_job`'s `environment` argument. |
| `get_environment` | Look up a single sandbox environment by name or UUID. Returns full details with secret env-var values masked, or nothing if it is not in your visible scopes. |
| `list_jobs` | List your recent agent runs (newest first), optionally filtered by `repo_id`, `status`, and `limit` (default 20, max 50). Returns a lean summary per run plus a `truncated` flag — use `get_job_status` for a single run's full result text. |
| `schedule_job` | Create a recurring or one-off scheduled run owned by you. Takes `name`, `prompt`, a 1–20 entry `repos` list, and a `frequency` (`hourly`/`daily`/`weekdays`/`weekly`/`custom`/`once`) with its companion field (`time` for daily/weekdays/weekly, `cron_expression` for custom, `run_at` for once). Optional `agent_model`, `agent_thinking_level`, `environment`, and `notify_on` mirror `submit_job`. |
| `list_scheduled_jobs` | List your scheduled jobs (newest first), optionally filtered by `enabled_only` or `repo_id`. |

`submit_job` takes a `repos` list (1–20 entries) and a single `prompt` that runs as an independent job against each repository. Each entry is `{repo_id, ref}`, where `ref` is the starting branch or commit the agent reads from — it is optional and defaults to the repository's default branch. The response includes a `batch_id`, a `jobs` list (one entry per submitted job, each with its `job_id`, `repo_id`, `ref`, `thread_id`, and `status`), and a `failed` list for repositories that could not be enqueued.

`submit_job` also accepts these optional parameters:

- `agent_model` — override the default model as a `provider_slug:model_name` string (e.g. `openrouter:anthropic/claude-sonnet-4.6`); the provider slug must match an enabled provider. Omit to use the system default.
- `agent_thinking_level` — control reasoning effort: one of `minimal`, `low`, `medium`, `high`, or `xhigh`. Omit to inherit the system default.
- `notify_on` — when to be notified for each job: one of `never`, `always`, `on_success`, or `on_failure`. Omit to fall back to your default preference.
- `environment` — the [sandbox environment](sandbox.md) to run every job in, given as its name or UUID (discover names via `list_environments`). Omit to auto-resolve a runtime per repository.
- `thread_id` — continue an existing thread by passing the UUID from a prior `submit_job` or `get_job_status` response. Continuation requires exactly one repository, whose latest activity must belong to you.

For the full request/response schema, the batch `repos` contract, and the job lifecycle, see the [Jobs API](jobs-api.md).

`schedule_job` creates a [scheduled run](schedules.md). Pick a `frequency` and supply its companion field: `time` (`"HH:MM"`, 24-hour) for `daily`, `weekdays`, and `weekly` (which fires on Mondays); a five-field `cron_expression` for `custom`; or an ISO-8601 `run_at` for a one-off `once` schedule. A `run_at` without a timezone offset is interpreted in the server timezone and must be in the future. New schedules are always enabled. The response includes the schedule `id` and the computed `next_run_at`. Manage existing schedules (edit, disable, delete) from the DAIV dashboard.

## Usage examples

Once connected, you can interact with DAIV naturally from your AI coding assistant:

- *"Ask DAIV to refactor the authentication module in mygroup/myproject to use JWT tokens"*
- *"Submit a job to mygroup/myproject on the develop branch: fix the broken CI pipeline"*
- *"Check the status of my last DAIV job"*

!!! tip
    Be specific in your prompts — include file paths, function names, error messages, or branch names. The more context you give, the better the result.
