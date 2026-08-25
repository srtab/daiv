# MCP Endpoint

DAIV exposes a [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server endpoint, allowing AI coding assistants to delegate tasks to the DAIV agent — directly from your editor or terminal.

The DAIV agent can read and modify code, run commands in a sandbox, create commits and branches, open merge requests or pull requests, and debug CI/CD pipelines. Through the MCP endpoint, your local assistant can offload these tasks to DAIV and get the results back.

## Authentication

The endpoint accepts two authentication methods:

- **OAuth 2.0** (default for interactive clients) — on first use a browser window opens for you to log in with your existing DAIV account. Your client manages tokens and refreshes them automatically. This is what the editor integrations below use out of the box.
- **API key** — pass an `Authorization: Bearer <api-key>` header. This is aimed at headless or non-interactive clients (CI jobs, scripts) that can't complete the browser flow. Create a key self-service in the dashboard at `/accounts/api-keys/` (see [Creating an API key](jobs-api.md#creating-an-api-key)); the same key also works against the HTTP [Jobs API](jobs-api.md).

!!! note
    API keys are scoped to your user, not to a specific surface — a key that authenticates against the MCP endpoint has the same access as it does on the REST Jobs/Chat API. Revoke a key from the dashboard to cut off both at once.

### Authenticating with an API key

Any MCP client that lets you set request headers can pass the key. For example, with Claude Code:

```bash
claude mcp add daiv --transport http https://daiv.example.com/mcp/ \
  --header "Authorization: Bearer <prefix.secret>"
```

Or in Cursor's `mcp.json`:

```json
{
  "mcpServers": {
    "daiv": {
      "type": "streamable-http",
      "url": "https://daiv.example.com/mcp/",
      "headers": { "Authorization": "Bearer <prefix.secret>" }
    }
  }
}
```

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
| `list_repositories` | Discover repositories accessible to DAIV, optionally filtered by `search` (partial name match) or `topics`. Served from DAIV's local repository catalog — a periodically synced mirror — not a live platform call. Returns `{repositories, next_cursor}` but does **not** cursor-paginate (`next_cursor` is always `null`). Results are capped at 40 — when a `warning` is present, narrow with `search` or `topics` rather than paging. |
| `list_environments` | List the sandbox environments visible to you (your own `USER` environments plus all `GLOBAL` ones), ordered by scope then name. Supports `limit` (default 20, max 50) and `cursor`; use a returned `name` or `id` as `submit_job`'s `environment` argument. |
| `get_environment` | Look up a single sandbox environment by name or UUID. Returns full details with secret env-var values masked, or nothing if it is not in your visible scopes. |
| `list_jobs` | List your recent agent runs (newest first), optionally filtered by `repo_id` and `status`. Supports `limit` (default 20, max 50) and `cursor`. Returns a lean summary per run plus `next_cursor` — use `get_job_status` for a single run's full result text. |
| `schedule_job` | Create a recurring or one-off scheduled run owned by you. Takes `name`, `prompt`, a 1–20 entry `repos` list, and a `frequency` (`hourly`/`daily`/`weekdays`/`weekly`/`custom`/`once`) with its companion field (`time` for daily/weekdays/weekly, `cron_expression` for custom, `run_at` for once). Optional `agent_model`, `agent_thinking_level`, `environment`, and `muted` mirror `submit_job`. |
| `list_scheduled_jobs` | List your scheduled jobs (newest first), optionally filtered by `enabled_only` or `repo_id`. Supports `limit` (default 20, max 50) and `cursor`. |

The paginated listing tools (`list_jobs`, `list_scheduled_jobs`, `list_environments`) share one pagination contract: pass an optional `limit` and `cursor`, and read back `{ "<items>": [...], "next_cursor": <token or null> }`. To page, call again with `cursor` set to the previous response's `next_cursor` until it comes back `null`; a cursor encodes only sort position, so reuse it with the **same** filters. `list_repositories` is also served from the database but never paginates — it returns the same shape for consistency (narrow with `search`/`topics` instead).

`submit_job` takes a `repos` list (1–20 entries) and a single `prompt` that runs as an independent job against each repository. Each entry is `{repo_id, ref}`, where `ref` is the starting branch or commit the agent reads from — it is optional and defaults to the repository's default branch. The response includes a `batch_id`, a `jobs` list (one entry per submitted job, each with its `job_id`, `repo_id`, `ref`, `thread_id`, and `status`), and a `failed` list for repositories that could not be enqueued.

`submit_job` also accepts these optional parameters:

- `agent_model` — override the default model as a `provider_slug:model_name` string (e.g. `openrouter:anthropic/claude-sonnet-4.6`); the provider slug must match an enabled provider. Omit to use the system default.
- `agent_thinking_level` — control reasoning effort: one of `minimal`, `low`, `medium`, `high`, or `xhigh`. Omit to inherit the system default.
- `muted` — mute this run's notifications; default false. `notify_on` is no longer accepted (removed); sending it returns an unknown-argument error.
- `environment` — the [sandbox environment](sandbox.md) to run every job in, given as its name or UUID (discover names via `list_environments`). Omit to auto-resolve a runtime per repository.
- `thread_id` — continue an existing thread by passing the UUID from a prior `submit_job` or `get_job_status` response. Continuation requires exactly one repository, whose latest activity must belong to you.
- `references` — a list of up to 20 external work-item references to link into the MR/PR DAIV creates. Each entry is an object with these fields:
    - `key` *(required)* — the issue or ticket identifier, 1–64 characters, matching `[A-Za-z0-9][A-Za-z0-9._/#-]*` (e.g. `"PROJ-123"`, `"42"`, `"DAIV-1V"`).
    - `url` *(optional)* — an `http(s)` URL for the work item, up to 500 characters. Used as the link target when DAIV renders the reference.
    - `provider` *(optional)* — identifies the ticketing system. Accepted values: `gitlab-issue`, `github-issue`, `sentry`, `jira`, or any lowercase alphanumeric-and-hyphen string up to 32 characters for other systems. Defaults to `generic`. DAIV emits platform-native closing syntax for the four named providers; any other provider value is accepted and renders as a plain reference link — no DAIV change is needed to add a new ticketing system.
    - `relation` *(optional)* — either `"relates"` (default) or `"closes"`. `"closes"` opts into auto-close on merge **where the provider supports it**: GitLab and GitHub close an issue when a merged MR description contains a closing keyword referencing it; Sentry resolves an issue whose short ID appears in a `Fixes …` commit line reaching the tracked branch (which also requires the Sentry–repository integration to be configured on Sentry's side — DAIV only emits the syntax). Auto-closing is always an explicit opt-in; the default `"relates"` only links.

  On thread continuation (when `thread_id` is set), newly supplied references are **merged** into the session's existing set, deduped by `(provider, key)` — the first-seen entry wins. A session accumulates at most 50 references in total across all turns.

  **Note (v1 limitation):** DAIV writes the MR description only when the MR is first created. References declared on a later turn reach that turn's commit trailers but do not rewrite an existing MR's description body.

For the full request/response schema, the batch `repos` contract, and the job lifecycle, see the [Jobs API](jobs-api.md).

`schedule_job` creates a [scheduled run](scheduled-jobs.md). Pick a `frequency` and supply its companion field: `time` (`"HH:MM"`, 24-hour) for `daily`, `weekdays`, and `weekly` (which fires on Mondays); a five-field `cron_expression` for `custom`; or an ISO-8601 `run_at` for a one-off `once` schedule. A `run_at` without a timezone offset is interpreted in the server timezone and must be in the future (a ~60-second grace into the past is tolerated). New schedules are always enabled. The response includes the schedule `id` and the computed `next_run_at`. Manage existing schedules (edit, disable, delete) from the DAIV dashboard.

## Usage examples

Once connected, you can interact with DAIV naturally from your AI coding assistant:

- *"Ask DAIV to refactor the authentication module in mygroup/myproject to use JWT tokens"*
- *"Submit a job to mygroup/myproject on the develop branch: fix the broken CI pipeline"*
- *"Check the status of my last DAIV job"*

!!! tip
    Be specific in your prompts — include file paths, function names, error messages, or branch names. The more context you give, the better the result.
