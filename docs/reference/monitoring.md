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
  "issue_id": 42,
  "labels": ["daiv", "bug"],
  "scope": "Issue"
}
```

**Comment addressing** (triggered by `@daiv` mentions on merge requests):

```json
{
  "author": "reviewer-username",
  "merge_request_id": 123,
  "scope": "Merge Request"
}
```

The `scope` field distinguishes how the agent was triggered: `Issue`, `Merge Request`, or `Global`.

### Dashboard tips

- **By trigger type** — filter by `scope` or by metadata key `issue_id` vs `merge_request_id`
- **By user** — filter or group by `author`
- **Performance** — monitor execution time and token usage per run

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
