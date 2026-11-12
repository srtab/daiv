# Monitoring

DAIV reports on its agent runs in two places. [LangSmith](https://smith.langchain.com) holds the trace of each run: latency, tool calls and intermediate steps. DAIV's own database holds each run's token usage and estimated cost, and a copy of each session's conversation. This page covers both, and the log events DAIV writes when something needs an operator.

## Trace runs with LangSmith

DAIV sends a trace of every agent run to LangSmith once you set three variables. You need a LangSmith account.

1. In [LangSmith](https://smith.langchain.com), select **Settings** → **API Keys** → **Create API Key**.

1. Copy the key.

1. Set `LANGSMITH_TRACING` to `true`, `LANGSMITH_PROJECT` to the project to write traces to, and `LANGSMITH_API_KEY` to the key. [Monitoring (LangSmith)](https://docs.daivagent.com/dev/reference/env-variables/#monitoring-langsmith) describes each variable, including the Docker secret form of the key and the EU endpoint.

   ```
   x-app-defaults: &x_app_default
     environment:
       LANGSMITH_TRACING: true
       LANGSMITH_PROJECT: daiv-production
       LANGSMITH_API_KEY: <langsmith-api-key>
   ```

   Replace `<langsmith-api-key>` with your LangSmith API key.

1. Recreate the services so they read the variables. On Docker Compose, run:

   ```
   docker compose up -d
   ```

   On Docker Swarm, run:

   ```
   docker stack deploy -c stack.yml daiv
   ```

1. Start a run, for example a chat in the dashboard. A trace appears in your LangSmith project.

## Trace tags and metadata

Every agent run carries tags and metadata that you can filter and group by in LangSmith. The title and commit message generation run has its own, described below.

An agent run has these tags:

| Tag             | Description                    | Example                            |
| --------------- | ------------------------------ | ---------------------------------- |
| Git platform    | The platform of the repository | `gitlab`, `github`                 |
| Repository slug | The repository the run targets | `group/project`                    |
| Scope           | The conversation scope         | `Issue`, `Merge Request`, `Global` |
| Agent name      | The agent that ran             | `DAIV Agent`                       |

The scope tag is absent when the run has no scope. A run on a labelled GitLab issue carries `gitlab`, `group/project`, `Issue` and `DAIV Agent`.

Its metadata holds these fields:

| Field              | Type           | Description                                                                                         |
| ------------------ | -------------- | --------------------------------------------------------------------------------------------------- |
| `repository`       | string         | Repository slug, for example `group/project`                                                        |
| `git_platform`     | string         | `gitlab` or `github`                                                                                |
| `scope`            | string         | `Issue`, `Merge Request` or `Global`                                                                |
| `trigger`          | string         | What started the run: `label`, `mention`, `job`, `chat` or `diff_to_metadata`                       |
| `model`            | string         | Primary model of the run, as a full model spec such as `openrouter:anthropic/claude-sonnet-5.5`     |
| `thinking_level`   | string         | Thinking level of the run; absent when the run has none                                             |
| `lc_agent_name`    | string         | The agent name, `DAIV Agent`                                                                        |
| `author`           | string         | User who created the issue or merge request (issue and merge request runs)                          |
| `triggered_by`     | string         | User who triggered the run, who differs from the author on a mention (issue and merge request runs) |
| `issue_id`         | int            | Issue IID (issue runs)                                                                              |
| `labels`           | list           | The issue's labels, lowercased (issue runs)                                                         |
| `merge_request_id` | int            | Merge request ID (merge request runs)                                                               |
| `ref`              | string         | The branch the run was asked to use (job runs)                                                      |
| `override_source`  | string or null | `explicit` when the request picked the model, otherwise `null` (job and chat runs)                  |

A run on a labelled issue has `trigger` set to `label`, and a run started by an `@daiv` mention on an issue, or on a merge request no session owns, has it set to `mention`. A chat message that starts a run on an idle session has `chat` and the `Global` scope. Every run DAIV takes from a session's inbox has `job` and the `Global` scope, as do runs from the [jobs API](https://docs.daivagent.com/dev/features/jobs-api/index.md), a schedule or the MCP endpoint. The inbox holds pipeline watch fixes, chat messages queued while the session was busy, delegation resumes and leg follow-ups, and `@daiv` mentions on a merge request the session owns.

The pull request title and commit message generation runs as a separate run named and tagged `DiffToMetadata`, with `trigger` set to `diff_to_metadata`. It carries the git platform, repository and scope tags and no agent name tag. It nests under the agent trace when DAIV publishes at the end of a run, and stands alone when DAIV recovers a draft.

## Create the LangSmith dashboard

DAIV creates a LangSmith dashboard named **DAIV Monitoring** with 27 charts in 8 groups, built on the tags and metadata above. You need the LangSmith variables from [Trace runs with LangSmith](#trace-runs-with-langsmith) set in the container.

Warning

Recreating the dashboard with `--recreate` deletes the existing **DAIV Monitoring** dashboard, including any chart you added by hand. [`setup_langsmith_dashboard`](https://docs.daivagent.com/dev/reference/management-commands/#setup_langsmith_dashboard) describes it.

1. Run the command, with your project name:

   ```
   docker compose exec app django-admin setup_langsmith_dashboard --project <project-name>
   ```

   Replace `<project-name>` with your LangSmith project.

1. In LangSmith, open the **DAIV Monitoring** dashboard of your project.

After you upgrade DAIV, run the command with `--recreate` to add the charts the upgrade brings.

| Group              | Charts                                                                                                      | What it tracks                      |
| ------------------ | ----------------------------------------------------------------------------------------------------------- | ----------------------------------- |
| **Overview**       | Trace volume and error rate by scope, trigger breakdown                                                     | Activity and reliability            |
| **Latency**        | Median and P99 latency by scope, latency by repository                                                      | Response times                      |
| **Cost**           | Total cost by scope, prompt against completion cost, token usage by repository, P99 cost per run by trigger | Spend                               |
| **Platform**       | Volume and error rate by git platform, top repositories by volume                                           | Platform and repository breakdown   |
| **Tools**          | Subagent usage, tool call volume, tool error rate, MCP server tool usage                                    | What the agent calls                |
| **LLM**            | Call count, latency, prompt against completion tokens                                                       | Model calls                         |
| **Model**          | Trace volume, latency, cost and error rate by model                                                         | Per-model comparison                |
| **DiffToMetadata** | Volume, latency, error rate                                                                                 | Title and commit message generation |

To compare models, group a chart by the `model` metadata field. To see who starts the runs, group by `author` or `triggered_by`.

## Usage and cost

DAIV records the token usage and an estimated cost for every agent run in its own database, without a LangSmith account.

DAIV counts the tokens of every model call in the run, including fallback models, models that tools call, and subagents, and prices each call on its own. For a call through OpenRouter, the cost is the charge OpenRouter reports, which depends on the provider it routed the call to. For any other call, DAIV estimates it from the model's list price in [genai-prices](https://github.com/pydantic/genai-prices). DAIV then totals the tokens and cost per model, and stores the totals on the run.

A run records:

| Field            | Description                                                                                |
| ---------------- | ------------------------------------------------------------------------------------------ |
| `input_tokens`   | Input tokens across all model calls                                                        |
| `output_tokens`  | Output tokens across all model calls                                                       |
| `total_tokens`   | Input plus output tokens                                                                   |
| `cost_usd`       | The run's cost in US dollars                                                               |
| `usage_by_model` | The same figures per model, with the cache and reasoning token counts the provider reports |

Two limits apply:

- For a call DAIV prices from list prices, the cost is empty when genai-prices does not list the model, and a run's cost is empty when any of its calls has no price. The tokens are still recorded, and DAIV logs a warning naming the model.
- A cost priced from list prices is an estimate. Your provider's bill can differ.

You see the figures in three places. A row in the sessions list shows the session's cost, the **Spend** section of a session's **Overview** shows what the session cost, and a run's downloaded Markdown result holds `total_tokens` and `cost_usd` in its front matter. [Sessions](https://docs.daivagent.com/dev/features/sessions/index.md) describes the pages.

## Export session archives

DAIV writes a copy of each session's conversation to its database when each run ends. This session archive is the saved copy, not a session's **Archived** state. The copy outlives the checkpointer's time to live and LangSmith's retention, and you can export it for offline analysis.

Warning

An export holds full conversations and run prompts. Store the output directory as you would the repositories' contents, and delete it when you finish.

1. Run [`export_session_archives`](https://docs.daivagent.com/dev/reference/management-commands/#export_session_archives) with an output directory, and with `--since` and `--repo` to narrow the sessions. The command's page holds the commands that write the files inside the container, copy them to the host and remove them.
1. Open `<thread_id>.json` for each session.

Each file holds the session's repository, origin and title, and its contexts in order, where a `/clear` closes one context and starts the next and `closed_at` says when. Each context holds its messages in LangChain's `messages_to_dict` format. The file also holds each run's status, model, tokens and cost. The archive holds the session's messages, not each model request: system prompts, tool schemas, summaries and subagent internals stay in LangSmith. When a session fails to export, the command logs and names it, writes the rest, then exits with an error.

## Log events

DAIV writes these events to its logs. Alert on them or search for them to find a problem early.

### Archive events

The `daiv.sessions` logger reports archive trouble. Errors reach Sentry.

| Level   | Message                                                                     | Meaning                                                                                                          |
| ------- | --------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| ERROR   | `executor: failed to archive thread_id=…`                                   | A run's end was not archived, and the traceback says why, a timeout included. The next run's archive catches up. |
| ERROR   | `Thread … holds N messages, fewer than its archived M; keeping the archive` | The live thread lost messages the archive holds. DAIV leaves the archive as it was.                              |
| ERROR   | `Failed to load archived context N of thread …; leaving it out`             | An archive row no longer decodes, and the transcript shows without it.                                           |
| WARNING | `No session … to archive its thread under`                                  | A thread has no session, so DAIV archives nothing.                                                               |
| INFO    | `Restored thread … from archived context N`                                 | An expired thread was seeded from its archive before a run.                                                      |

A context that exists but fails to restore fails the run with `ArchiveRestoreError` and is not logged as one of these events.

### Bash policy events

The `daiv.tools` logger writes a warning when the [sandbox](https://docs.daivagent.com/dev/features/sandbox/index.md) `bash` tool blocks a command, or cannot parse one and so blocks it.

```
[bash] bash_policy_denied: command denied (id=<call_id>, reason=default_disallow, rule='git push', segment='git push origin main')
```

```
[bash] bash_policy_parse_failed: command could not be parsed (id=<call_id>, reason='Parse error: ...')
```

Each record carries these fields:

| Field             | Description                                                                             |
| ----------------- | --------------------------------------------------------------------------------------- |
| `event`           | `bash_policy_denied` or `bash_policy_parse_failed`                                      |
| `reason_category` | `default_disallow`, `global_disallow` or `parse_failure`                                |
| `matched_rule`    | The rule that blocked the command, for example `git push`; on `bash_policy_denied` only |
| `denied_segment`  | The part of the command that was blocked; on `bash_policy_denied` only                  |
| `tool_call_id`    | The agent's tool call ID, which matches the tool call in the LangSmith trace            |

`default_disallow` is a built-in rule, and `global_disallow` is a rule you set in `DAIV_SANDBOX_COMMAND_POLICY_DISALLOW`. A spike in `bash_policy_denied` means the agent keeps attempting a blocked command.

## Troubleshooting

### No traces appear in LangSmith

Check that `LANGSMITH_TRACING` is `true` and that the API key is correct. Check that the containers can reach the LangSmith endpoint, and read the application logs for authentication errors.

### A trace is incomplete

Check that the project name is the same in every service. On Docker Swarm, check that the secret holding the API key is mounted.
