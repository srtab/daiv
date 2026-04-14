# Monitoring

DAIV supports [LangSmith](https://smith.langchain.com) for tracing agent executions. This page covers how to set it up and what data is available.

---

## Setup

### 1. Get an API key

1. Sign in at [smith.langchain.com](https://smith.langchain.com)
2. Go to **Settings** → **API Keys** → **Create API Key**
3. Copy the key (format: `lsv2_pt_...`)

### 2. Configure environment variables

#### Docker Compose

```yaml
x-app-defaults: &x_app_default
  # ...
  environment:
    LANGSMITH_TRACING: true
    LANGSMITH_PROJECT: daiv-production
    LANGSMITH_API_KEY: lsv2_pt_xxxxxxxxxxxxxxxxxxxxxxxx_yyyyyyyyyyyy
  # ...
```

#### Docker Swarm

```bash
# Environment
LANGSMITH_TRACING=true
LANGSMITH_PROJECT=daiv-production
LANGSMITH_API_KEY_FILE=/run/secrets/langsmith_api_key
```

```bash
# Create secret
echo "lsv2_pt_xxxxxxxxxxxxxxxxxxxxxxxx_yyyyyyyyyyyy" | docker secret create langsmith_api_key -
```

!!! tip "EU endpoint"
    If you're in Europe, set `LANGSMITH_ENDPOINT=https://eu.api.smith.langchain.com`.

### 3. Restart services

```bash
# Docker Compose
docker compose restart

# Docker Swarm
docker stack deploy -c stack.yml daiv
```

### 4. Verify

1. Create a test issue with the `daiv` label
2. Open your LangSmith project — traces should appear within a few minutes

See [Environment Variables](env-variables.md#monitoring-langsmith) for all LangSmith-related variables.

---

## Trace tags and metadata

Every agent run includes tags and metadata that make it easy to filter and build dashboards in LangSmith.

### Tags

All runs include two tags:

| Tag | Description | Example |
|-----|-------------|---------|
| Agent name | Always `DAIV Agent` | `DAIV Agent` |
| Git platform | The platform handling the request | `gitlab`, `github` |

### Metadata by trigger

**Issue addressing** (triggered by labelled issues):

```json
{
  "author": "username",
  "triggered_by": "username",
  "trigger": "label",
  "repository": "group/project",
  "git_platform": "gitlab",
  "issue_id": 42,
  "labels": ["daiv", "bug"],
  "scope": "Issue",
  "model": "claude-sonnet-4-6",
  "thinking_level": "medium"
}
```

**Comment addressing** (triggered by `@daiv` mentions on merge requests):

```json
{
  "author": "reviewer-username",
  "triggered_by": "commenter-username",
  "trigger": "mention",
  "repository": "group/project",
  "git_platform": "gitlab",
  "merge_request_id": 123,
  "scope": "Merge Request",
  "model": "claude-sonnet-4-6",
  "thinking_level": "medium"
}
```

**Job execution** (triggered via the Jobs API):

```json
{
  "repo_id": "group/project",
  "ref": "main",
  "trigger": "job",
  "model": "claude-sonnet-4-6",
  "thinking_level": "medium"
}
```

The `scope` field distinguishes how the agent was triggered: `Issue`, `Merge Request`, or `Global`.

### Metadata fields reference

| Field | Type | Description |
|-------|------|-------------|
| `author` | string | User who created the issue or merge request |
| `triggered_by` | string | User who triggered the agent (may differ from author for mentions) |
| `trigger` | string | How the agent was triggered: `label`, `mention`, or `job` |
| `repository` | string | Repository slug (e.g. `group/project`) |
| `git_platform` | string | Git platform: `gitlab` or `github` |
| `scope` | string | Conversation scope: `Issue`, `Merge Request`, or `Global` |
| `model` | string | Primary model used for the agent run |
| `thinking_level` | string or null | Thinking level: `low`, `medium`, `high`, or `null` if disabled |
| `issue_id` | int | Issue IID (issue triggers only) |
| `merge_request_id` | int | Merge request ID (MR triggers only) |
| `labels` | list | Issue labels, lowercased (issue triggers only) |

---

## Custom dashboard

DAIV includes a management command to create a pre-configured LangSmith custom dashboard with charts covering all key metrics.

### Setup

```bash
python manage.py setup_langsmith_dashboard --project <project-name>
```

The command creates a single dashboard named **DAIV Monitoring** with 27 charts organized into 8 groups:

| Group | Charts | What it tracks |
|-------|--------|----------------|
| **Overview** | Trace Volume, Error Rate, Trigger Breakdown | High-level activity and reliability |
| **Latency** | P50/P99 by Scope, by Repository | Response times |
| **Cost** | Total/Prompt/Completion Cost, Token Usage, P99 Cost | Spend tracking |
| **Platform** | Volume and Error Rate by Platform, Top Repos | Platform and repository breakdown |
| **Tools** | Subagent Usage, Tool Calls, Tool Errors, MCP Tools | Agent internals |
| **LLM** | Call Count, Latency, Token Usage | Model-level metrics |
| **Model** | Volume, Latency, Cost, Error Rate by Model | Per-model comparison |
| **DiffToMetadata** | Volume, Latency, Error Rate | Diff-to-metadata pipeline health |

### Options

| Flag | Description |
|------|-------------|
| `--project` | LangSmith project name (default: `LANGCHAIN_PROJECT` or `LANGSMITH_PROJECT` env var) |
| `--recreate` | Delete the existing dashboard and recreate it from scratch |

### Recreating the dashboard

To update the dashboard after upgrading DAIV (which may add new charts):

```bash
python manage.py setup_langsmith_dashboard --recreate
```

### Dashboard tips

- **By trigger type** — filter by `scope` or by metadata key `issue_id` vs `merge_request_id`
- **By user** — filter or group by `author`
- **By model** — group by `model` to compare performance across models (e.g. `claude-sonnet-4-6` vs `claude-opus-4-6`)
- **Performance** — monitor execution time and token usage per run

---

## Internal usage and cost tracking

DAIV tracks token usage and estimated cost for every agent execution internally — independent of LangSmith.

### How it works

1. A `UsageMetadataCallbackHandler` (from LangChain) is attached to the `RunnableConfig` before each agent invocation.
2. The handler captures `usage_metadata` from every LLM call during graph execution — including fallback model invocations and tool-internal model calls.
3. After the run, token counts are aggregated per model and cost is calculated using [genai-prices](https://github.com/pydantic/genai-prices) (maintained by Pydantic).
4. The usage summary is stored in the `AgentResult` and denormalized onto the `Activity` record for long-term retention.

### What's tracked

| Field | Description |
|-------|-------------|
| `input_tokens` | Total input (prompt) tokens across all model calls |
| `output_tokens` | Total output (completion) tokens |
| `total_tokens` | Sum of input + output |
| `cost_usd` | Estimated USD cost from genai-prices |
| `usage_by_model` | Per-model breakdown of tokens and cost |

Token detail buckets (when available from the provider):

- **Cache creation / cache read** — Anthropic prompt caching tokens
- **Reasoning tokens** — thinking/chain-of-thought tokens (Anthropic extended thinking, OpenAI reasoning)

### Where to see it

- **Activity list** — cost or token count shown per row
- **Activity detail** — full breakdown with per-model details
- **Markdown export** — usage metadata included in YAML frontmatter

### Known limitations

- **Subagent calls** are not tracked — the `SubAgentMiddleware` creates a separate execution context that does not inherit the parent's callbacks.
- **Summarization middleware calls** may not be tracked if the middleware overrides the config.
- If a model is **not in the genai-prices database**, token usage is still recorded but cost is stored as `null`. A warning is logged.
- Cost estimates are **approximations** based on published list prices. Actual billing may differ based on your provider agreement.
- **Chat API flows** attach the tracker for callback propagation but do not persist usage to Activity records (chat does not create Activity records).

### Relationship to LangSmith

LangSmith remains the recommended tool for detailed **trace-level** observability (latency, tool calls, intermediate steps, debugging). Internal cost tracking provides **run-level** usage summaries that persist on Activity records and do not require a LangSmith account.

---

## Bash command policy logs

DAIV emits structured warning logs whenever the sandbox bash tool blocks a command due to policy evaluation or a parse failure. These logs use the `daiv.tools` logger.

### Log events

**`bash_policy_denied`** — a command segment matched a disallow rule:

```
[bash] bash_policy_denied: command denied (id=<call_id>, reason=default_disallow, rule='git push', segment='git push origin main')
```

**`bash_policy_parse_failed`** — the command string could not be parsed (fail-closed):

```
[bash] bash_policy_parse_failed: command could not be parsed (id=<call_id>, reason='Parse error: ...')
```

### Log fields

| Field | Description |
|-------|-------------|
| `event` | `bash_policy_denied` or `bash_policy_parse_failed` |
| `reason_category` | `default_disallow`, `repo_disallow`, or `parse_failure` |
| `matched_rule` | The rule prefix that triggered denial (e.g. `"git push"`) |
| `denied_segment` | The specific argv segment that was blocked |
| `tool_call_id` | The agent tool-call ID for correlation with LangSmith traces |

### Monitoring recommendations

- **Alert on high denial rates** — a spike in `bash_policy_denied` may indicate the agent is repeatedly attempting prohibited operations.
- **Tune policy rules** — use `reason_category` to distinguish built-in denials from custom repo-level denials and adjust `.daiv.yml` accordingly.
- **Correlate with LangSmith** — the `tool_call_id` links each denial to the full agent trace for deeper investigation.

---

## Troubleshooting

**No traces appearing**:

- Verify `LANGSMITH_TRACING=true` is set
- Check the API key is correct
- Ensure network connectivity to LangSmith endpoints
- Review application logs for authentication errors

**Incomplete trace data**:

- Verify the project name matches across all services
- Ensure Docker secrets are properly mounted (Swarm deployments)
