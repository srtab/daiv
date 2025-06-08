# Monitoring Configuration

This guide walks you through configuring LangSmith monitoring for DAIV. LangSmith provides comprehensive observability for your AI agents, including tracing, logging, and performance monitoring.

---

## Prerequisites

Before configuring monitoring, ensure you have:

- **DAIV installed and running** - Follow the [installation guide](up-and-running.md) first
- **LangSmith account** - Create a free account at [smith.langchain.com](https://smith.langchain.com)
- **LangSmith API key** - Generated from your LangSmith dashboard

---

## Step 1: Create LangSmith API Key

1. **Sign in to LangSmith**:

   - Go to [smith.langchain.com](https://smith.langchain.com)
   - Sign in with your account or create a new one

2. **Generate API Key**:

   - Navigate to **Settings** → **API Keys**
   - Click **Create API Key**
   - **Name**: `DAIV Integration`
   - **Description**: `API key for DAIV monitoring`
   - Click **Create**

3. **Copy the API Key**:
   - **Important**: Copy and save the API key immediately - you won't see it again
   - The key format looks like: `lsv2_pt_xxxxxxxxxxxxxxxxxxxxxxxx_yyyyyyyyyyyy`

!!! warning "API Key Security"
    Store your API key securely. Never commit it to version control or share it publicly.

---

## Step 2: Configure Environment Variables

Add your LangSmith configuration to DAIV's environment settings.

### For Docker Compose Setup

Edit your `docker-compose.yml` file:

```yaml
x-app-defaults: &x_app_default
  # ...
  environment:
    LANGSMITH_TRACING: true
    LANGSMITH_PROJECT: daiv-default
    LANGSMITH_API_KEY: lsv2_pt_xxxxxxxxxxxxxxxxxxxxxxxx_yyyyyyyyyyyy
  # ...
```

### For Docker Swarm Setup

**Environment configuration**:
```bash
# LangSmith Monitoring
LANGSMITH_TRACING=true
LANGSMITH_PROJECT=daiv-production
LANGSMITH_API_KEY_FILE=/run/secrets/langsmith_api_key
```

**Create Docker secret**:
```bash
# Create secret for LangSmith API key
echo "lsv2_pt_xxxxxxxxxxxxxxxxxxxxxxxx_yyyyyyyyyyyy" | docker secret create langsmith_api_key -
```

!!! tip "Using EU Endpoint"
    If you're in Europe, you may want to use the EU endpoint (default is US):
    ```bash
    LANGSMITH_ENDPOINT=https://eu.api.smith.langchain.com
    ```

---

## Step 3: Configure Project Settings

Customize your LangSmith project settings for better organization.

### Project Names

Use descriptive project names to organize your traces:

```bash
# For different environments
LANGSMITH_PROJECT=daiv-production    # Production environment
LANGSMITH_PROJECT=daiv-staging       # Staging environment
LANGSMITH_PROJECT=daiv-development   # Development environment
```

---

## Step 4: Restart DAIV Services

Apply the new monitoring configuration by restarting DAIV.

### For Docker Compose

```bash
# Restart all services
docker compose restart

# Or restart specific services
docker compose restart app worker
```

### For Docker Swarm

```bash
# Update the stack with new configuration
docker stack deploy -c stack.yml daiv
```

---

## Step 5: Verify Monitoring Setup

Test that LangSmith monitoring is working correctly.

1. **Generate Some Activity**:

   - Create a test issue in your repository with the `daiv` label
   - Wait for DAIV to process the issue
   - Or trigger any AI agent activity

2. **Check LangSmith Dashboard**:

   - Go to [smith.langchain.com](https://smith.langchain.com)
   - Navigate to your project (e.g., `daiv-default`)
   - You should see traces appearing for agent executions

3. **Verify Trace Details**:

   - Click on any trace to see detailed execution steps
   - Check for proper agent names, model calls, and timing information

---

## Step 6: Dashboard and Analytics

Set up monitoring dashboards and alerts for your DAIV deployment.

### Agent Metadata and Tags

Each DAIV agent automatically includes standardized metadata and tags for LangSmith tracing, making it easy to create dashboards and analyze performance:

#### Standard Tags

All agents include these tags in their traces:

| Tag | Description | Example Values |
|-----|-------------|----------------|
| **Agent Name** | The specific agent type | `IssueAddressor`, `PipelineFixer`, `ReviewAddressor`, `CodebaseChat`, `PullRequestDescriber`, `CodeDescriber`, `SnippetReplacer`, `ImageURLExtractor`, `CodebaseSearch`, `PlanAndExecute` |
| **Client Slug** | The repository client identifier | `gitlab`, `github` |

#### Agent-Specific Metadata

Different agents include additional context-specific metadata:

**Issue Addressor** (`IssueAddressor`):
```json
{
  "author": "username",
  "thread_id": "unique-thread-id",
  "project_id": 123,
  "source_repo_id": "group/repo",
  "source_ref": "main",
  "issue_id": 456,
  "repo_client": "gitlab"
}
```

**Pipeline Fixer** (`PipelineFixer`):
```json
{
  "merge_request_id": 789,
  "job_id": 101112,
  "thread_id": "unique-thread-id",
  "source_repo_id": "group/repo",
  "source_ref": "feature-branch",
  "job_name": "build_and_test"
}
```

**Review Addressor** (`ReviewAddressor`):
```json
{
  "merge_request_id": 789,
  "discussion_id": "abc123",
  "author": "reviewer-username",
  "thread_id": "unique-thread-id",
  "source_repo_id": "group/repo",
  "source_ref": "feature-branch"
}
```

**Codebase Chat** (`CodebaseChat`):
```json
{
  "model_id": "DAIV",
  "chat_stream": true
}
```

**Pull Request Describer** (`PullRequestDescriber`):
```json
{
  "thread_id": "unique-thread-id"
}
```

#### Creating Custom Dashboards

Use these tags and metadata to create focused dashboards:

**By Agent Type:**
- Filter by tag: `IssueAddressor` to see all issue processing activity
- Filter by tag: `PipelineFixer` to monitor CI/CD troubleshooting
- Filter by tag: `ReviewAddressor` to track code review interactions

**By Repository:**
- Filter by metadata: `source_repo_id` = `"your-org/your-repo"`
- Group by `repo_client` to compare GitLab vs GitHub activity

**By User Activity:**
- Filter by metadata: `author` = `"username"` to see user-specific interactions
- Group by `author` to identify most active users

**By Performance:**
- Monitor execution time by agent type
- Track token usage patterns across different agents
- Analyze success/failure rates by agent and repository

### Setting Up Alerts

Configure alerts in LangSmith for:
- High error rates (> 5%)
- Slow response times (> 30 seconds)
- Excessive token usage
- Failed agent executions

---

## Troubleshooting

### Common Issues

**No traces appearing in LangSmith**:
- Verify API key is correct and has proper permissions
- Check that `LANGSMITH_TRACING=true` is set
- Ensure network connectivity to LangSmith endpoints
- Review application logs for authentication errors

**Incomplete or missing trace data**:
- Verify project name matches in all configurations
- Check that all required environment variables are set
- Ensure Docker secrets are properly mounted (for Swarm deployments)

**High costs or token usage**:
- Review trace filtering settings
- Consider disabling tracing for development environments
- Monitor token consumption patterns in LangSmith dashboard

---

## Advanced Configuration

### Sampling Configuration

Configure trace sampling to reduce costs while maintaining visibility:

```bash
# Sample 50% of traces (default: 100%)
LANGCHAIN_TRACING_SAMPLE_RATE=0.5
```

---

## ⏭️ Next Steps

For more detailed information about LangSmith features, visit the [LangSmith documentation](https://docs.smith.langchain.com/).
