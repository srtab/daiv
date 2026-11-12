---
description: "Connect Claude Code, Cursor or Codex CLI to DAIV over the Model Context Protocol (MCP) and delegate coding tasks to the agent from your editor."
---

# MCP endpoint

DAIV runs a [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server endpoint at `/mcp/`, so an AI coding assistant can delegate tasks to the DAIV agent from your editor or terminal.

The DAIV agent reads and modifies code, runs commands in a sandbox, creates commits and branches, opens merge or pull requests, and debugs CI/CD pipelines. Through the endpoint, your local assistant offloads these tasks to DAIV and gets the results back.

## Connect your editor

You need:

- A DAIV account.
- An MCP client that supports the Streamable HTTP transport. Claude Code, Cursor and Codex CLI do.
- The address of your DAIV instance. The examples use `https://daiv.example.com`; replace it with yours.

On first use, an interactive client opens a browser window where you sign in with your DAIV account. The client stores the OAuth 2.0 token and refreshes it. To authenticate without a browser, for a CI job or script, see [API key authentication](#api-key-authentication).

### Claude Code

Run:

```bash
claude mcp add daiv --transport http https://daiv.example.com/mcp/
```

### Cursor

Add DAIV to `.cursor/mcp.json` for one project, or to `~/.cursor/mcp.json` for all projects:

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

Add DAIV to `.codex/config.toml` for one project, or to `~/.codex/config.toml` for all projects:

```toml
[mcp_servers.daiv]
url = "https://daiv.example.com/mcp/"
```

Any other MCP client that supports the Streamable HTTP transport connects to the same `/mcp/` URL.

## API key authentication

A client that cannot complete the browser sign-in sends an API key instead. Create the key as described in [Create an API key](jobs-api.md#create-an-api-key), then pass it in an `Authorization: Bearer` header. Any MCP client that lets you set request headers can do this.

An API key belongs to your user, not to one surface. A key that authenticates against the MCP endpoint has the same access on the [jobs API](jobs-api.md) and the chat API, and revoking it cuts off all of them at once.

In Claude Code, run:

```bash
claude mcp add daiv --transport http https://daiv.example.com/mcp/ \
  --header "Authorization: Bearer <prefix.secret>"
```

In Cursor's `mcp.json`, set the header:

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

Replace `<prefix.secret>` with your API key.

## Available tools

| Tool | Description |
|------|-------------|
| `submit_job` | Submit a prompt to the DAIV agent as a batch of jobs, one independent job per repository. Returns a `batch_id` and a `jobs` list. See [`submit_job` parameters](#submit_job-parameters). |
| `get_job_status` | Get the status and result of a submitted job, plus the `artifacts` it published or revised (reports and other files, with viewer and download URLs; see [Artifacts](sessions.md#artifacts)). A `WAITING_INPUT` status means the agent stopped to ask a question: `question` holds it and `result` its rendered text, and you answer with `submit_job` using the same `thread_id`. With `wait=True` it blocks until the job completes; a `WAITING_INPUT` result also ends the wait, since it is final. |
| `list_repositories` | Discover the repositories DAIV can access, optionally filtered by `search` (partial name match) or `topics`. DAIV serves them from its local repository catalog, a periodically synced mirror, not a live platform call. Ordered by slug, so a `search` that matches more repositories than `limit` is fully reachable by paging. |
| `list_environments` | List the sandbox environments you can see: your own `USER` environments plus all `GLOBAL` ones, ordered by scope then name, with no environment-variable values at all. Use a returned `name` or `id` as the `environment` argument of `submit_job`. |
| `get_environment` | Look up one sandbox environment by name or UUID. Returns the full details with secret environment-variable values masked, or nothing if the environment is not in your visible scopes. |
| `list_jobs` | List your recent agent runs, newest first, optionally filtered by `repo_id` and `status`. Returns a lean summary per run plus `next_cursor`; use `get_job_status` for one run's full result text. |
| `schedule_job` | Create a recurring or one-off [scheduled run](scheduled-jobs.md) that you own. See [`schedule_job` parameters](#schedule_job-parameters). |
| `list_scheduled_jobs` | List your scheduled jobs, newest first, optionally filtered by `enabled_only` or `repo_id`. |

The four listing tools (`list_jobs`, `list_scheduled_jobs`, `list_environments` and `list_repositories`) share one pagination contract. Pass an optional `limit` (default 20, at most 50) and `cursor`, and read back `{ "<items>": [...], "next_cursor": <token or null> }`. To page, call again with `cursor` set to the previous `next_cursor` until it is `null`. A cursor encodes only a sort position, so reuse it with the same filters.

### `submit_job` parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `repos` | yes | 1–20 entries of `{repo_id, ref, prompt}`. `ref` is the branch the agent starts from; it is optional, defaults to the repository's default branch, and must be a branch that exists on the remote (a commit SHA fails the clone). `prompt` is optional, must not be blank, and replaces the batch `prompt` for that repository only. A merge or pull request the job opens targets that branch, and DAIV assigns it to the token's owner when their DAIV account is OAuth-linked to the git platform. |
| `prompt` | yes | The instruction. It runs as an independent job against each repository, except one that gives its own `prompt`. |
| `wait` | no | Block until every job in the batch completes, for up to 10 minutes in total, and return each job's status with its `artifacts`. Default `false`. |
| `agent_model` | no | Override the model, as `provider_slug:model_name`, for example `openrouter:anthropic/claude-sonnet-5.5`. The provider slug must match an enabled provider. Without it, DAIV uses the repository's `models.agent` model, else the system default. |
| `agent_thinking_level` | no | Reasoning effort: `minimal`, `low`, `medium`, `high` or `xhigh`. Without it, DAIV uses the repository's `models.agent` thinking level, else the system default. |
| `muted` | no | Mute this run's notifications. Default `false`. DAIV rejects `notify_on` with an unknown-argument error. |
| `environment` | no | The [sandbox environment](sandbox-environments.md) to run every job in, by name or UUID; find names with `list_environments`. Without it, DAIV resolves a runtime for each repository. |
| `shared_session` | no | Run up to 5 `repos` in one session instead of one job each. The first repository is the main one. The response holds one job, and `get_job_status` and `list_jobs` add `merge_requests` and `publish_errors`. See [Multi-repository sessions](jobs-api.md#multi-repository-sessions); the dashboard chat starts them too, from its [Workspace card](sessions.md#start-a-chat-over-several-repositories). |
| `thread_id` | no | Continue an existing thread, by the UUID from an earlier `submit_job` or `get_job_status` response. `repos` must hold exactly one repository (a multi-repository session also accepts its full list, in order), whose latest activity is yours, in a session you have not archived. This is also how you answer a `WAITING_INPUT` job: submit the answer as `prompt` with the same `thread_id`. If the session is busy, the job joins its next run for the job's branch and comes back `QUEUED`; jobs that join the same next run share its `job_id` (see [Job lifecycle](jobs-api.md#job-lifecycle)). |
| `references` | no | Up to 20 external work-item references to link into the merge or pull request DAIV creates. See [References](#references). |
| `mcp_overrides` | no | MCP servers to switch on or off for every job in the batch, relative to the default set: `{"grafana": true}` adds an on-demand server, `{"sentry": false}` drops an active one. Names are those of the global servers and your own personal ones; DAIV returns an unknown name, or a value other than `true` or `false`, as an error. On a continued thread (`thread_id`) the overrides apply on top of the thread's own, so a server you do not name keeps its setting. DAIV returns a continuation that would change them while another run on the thread is unfinished in `failed`, since that run would pick them up too. |

The response holds a `batch_id`, a `jobs` list with one entry per submitted job (each with its `job_id`, `repo_id`, `ref`, `thread_id` and `status`), and a `failed` list for repositories DAIV could not enqueue. The tool shares the [jobs API](jobs-api.md#rate-limiting) rate limit. For the full response schema, see the [jobs API](jobs-api.md).

### References

Each entry of `references` is an object with these fields:

| Field | Required | Description |
|-------|----------|-------------|
| `key` | yes | The issue or ticket identifier, 1–64 characters, matching `[A-Za-z0-9][A-Za-z0-9._/#-]*`, for example `PROJ-123`, `42` or `DAIV-1V`. When `relation` is `closes`, the key must be a bare identifier, with no `/` and no `#`: DAIV emits it next to a closing keyword, where `namespace/project#7` would be a valid cross-project reference and would close an issue in someone else's project. |
| `url` | no | An `http` or `https` URL for the work item, up to 500 characters, used as the link target. DAIV renders it into a markdown link destination in the merge or pull request description, so percent-encode anything outside the RFC 3986 URL character set, parentheses included. A space, a newline or a `)` would end the link and let the rest of the value become body text the git platform parses. |
| `provider` | no | The ticketing system: `gitlab-issue`, `github-issue`, `sentry`, `jira`, or any other lowercase alphanumeric-and-hyphen string up to 32 characters. Default `generic`. |
| `relation` | no | `relates` (default) only links. `closes` opts into auto-close on merge where the provider supports it. |

How each provider renders:

- `gitlab-issue` and `github-issue` write the platform's own closing or relating line in the description. GitLab and GitHub close an issue when a merged description contains a closing keyword that references it.
- `sentry` with `relation: "closes"` writes a `Fixes <short-id>` line in the description and as a commit trailer. Sentry resolves an issue whose short ID appears in a `Fixes` commit line that reaches the tracked branch, which also needs the Sentry and repository integration set up on Sentry's side: DAIV only writes the syntax.
- `jira` adds a `Refs: <key>` commit trailer for dev-panel linking, a link and never a workflow transition. Its description entry is a plain reference link.
- Any other provider is accepted and renders as a plain reference link.

DAIV writes the merge or pull request description only when it first creates the request. On a continued thread (`thread_id` set), DAIV merges newly supplied references into the session's set when the run that takes the job starts, deduplicated by `(provider, key)` with the first-seen entry winning, and a session keeps at most 50 references across all turns. References from a later turn reach that turn's commit trailers, but do not rewrite the description of an existing request.

### `schedule_job` parameters

`schedule_job` takes `name`, `prompt`, a `repos` list of 1–20 `{repo_id, ref}` entries, and a `frequency` with its companion field. It refuses any other key in a `repos` entry, a per-repository `prompt` included. The optional `agent_model`, `agent_thinking_level`, `environment`, `mcp_overrides` and `muted` work as on `submit_job`.

| `frequency` | Companion field |
|-------------|-----------------|
| `daily`, `weekdays`, `weekly` | `time`, as `HH:MM` in 24-hour form. `weekly` runs on Mondays. |
| `custom` | `cron_expression`, a five-field cron expression. |
| `once` | `run_at`, an ISO 8601 date and time in the future. DAIV tolerates about 60 seconds in the past. A value without an offset is read in the server's [time zone](../reference/env-variables.md#time-zone). |
| `hourly` | None. |

New schedules are always enabled. The response includes the schedule `id` and the computed `next_run_at`. To edit, disable or delete a schedule, use the dashboard.

## Usage examples

Once connected, ask your assistant in plain language:

- "Ask DAIV to refactor the authentication module in mygroup/myproject to use JWT tokens."
- "Submit a job to mygroup/myproject on the develop branch: fix the broken CI pipeline."
- "Check the status of my last DAIV job."

!!! tip
    Be specific: include file paths, function names, error messages or branch names. The more context you give, the better the result.
