# Environment Variables

DAIV provides a large number of environment variables to configure its behavior. This page lists all supported variables.

Variables marked with:

 * :material-lock: are sensitive (such as API keys, passwords, and tokens) and should be declared using Docker secrets or a secure credential manager.
 * :material-asterisk: are required and should be declared.

---

## Core

### General

| Variable                | Description                                         | Default                | Example                        |
|-------------------------|-----------------------------------------------------|:----------------------:|--------------------------------|
| `DJANGO_DEBUG`          | Toggle Django debug mode                            | `False`                | `True`                         |
| :material-asterisk: `DJANGO_SECRET_KEY`  :material-lock:     | Secret key for Django                              | *(none)*               | `super-secret-key`             |
| `DJANGO_ALLOWED_HOSTS`  | Comma-separated list of allowed hosts               | `*`                    | `example.com,localhost`        |

!!! danger
    Do not turn on `DJANGO_DEBUG` in production. It will **expose sensitive information** and **break the security of the application**.

!!! info
    The `DJANGO_ALLOWED_HOSTS` variable is used to specify the hosts that are allowed to access the application. Make sure to include the host where the application is running to increase security.

### Uvicorn

| Variable                | Description                                         | Default                | Example                        |
|-------------------------|-----------------------------------------------------|:----------------------:|--------------------------------|
| `UVICORN_HOST`          | Host to bind the Uvicorn server                     | `0.0.0.0`              | `0.0.0.0`                      |
| `UVICORN_PORT`          | Port to bind the Uvicorn server                     | `8000`                 | `8000`                         |


### Database

| Variable        | Description                                 | Default      | Example         |
|-----------------|---------------------------------------------|:------------:|-----------------|
| :material-asterisk: `DB_NAME`       | Database name                              | *(none)*     | `daiv`          |
| :material-asterisk: `DB_USER`        | Database user                              | *(none)*     | `daiv_admin`    |
| :material-asterisk: `DB_PASSWORD`  :material-lock:   | Database password                          | *(none)*     |                 |
| `DB_HOST`       | Database host                              | `localhost`  | `db`            |
| `DB_PORT`       | Database port                              | `5432`       | `5432`          |
| `DB_SSLMODE`    | PostgreSQL SSL mode                        | `require`    | `prefer`        |
| `DB_POOL_MAX_SIZE` | Maximum size of a connection pool | `15` | `30` |

### Redis

| Variable           | Description                | Default | Example |
|--------------------|----------------------------|:---------:|---------|
| :material-asterisk: `DJANGO_REDIS_URL`  :material-lock: | Redis connection URL for cache (DB 0) | *(none)* | `redis://redis:6379/0` |
| `DJANGO_REDIS_SESSION_URL`  :material-lock: | Redis connection URL for sessions (DB 1) | Value of `DJANGO_REDIS_URL` | `redis://redis:6379/1` |
| `DJANGO_REDIS_CHECKPOINT_URL`  :material-lock: | Redis connection URL for LangGraph checkpoints (DB 2) | Value of `DJANGO_REDIS_URL` | `redis://redis:6379/2` |
| `DJANGO_REDIS_CHECKPOINT_TTL_MINUTES` | TTL in minutes for LangGraph checkpoint data | `10080` (7 days) | `1440` |


### Sentry

| Variable                | Description                        | Default        | Example         |
|-------------------------|------------------------------------|:--------------:|-----------------|
| `SENTRY_DSN` :material-lock:            | Sentry DSN                         | *(none)*       |                 |
| `SENTRY_DEBUG`          | Enable Sentry debug mode           | `False`        | `True`          |
| `SENTRY_ENABLE_LOGS`    | Enable Sentry logs                 | `False`        | `True`          |
| `SENTRY_TRACES_SAMPLE_RATE` | Sentry traces sample rate          | `0.0`          | `1.0`           |
| `SENTRY_PROFILES_SAMPLE_RATE` | Sentry profiles sample rate        | `0.0`          | `1.0`           |
| `SENTRY_SEND_DEFAULT_PII` | Enable Sentry default PII           | `False`        | `True`          |
| `NODE_HOSTNAME`         | Node hostname for Sentry           | *(none)*       |                 |
| `SERVICE_NAME`          | Service name for Sentry            | *(none)*       |                 |

!!! note
    `NODE_HOSTNAME` and `SERVICE_NAME` are used to identify the node and service that is reporting the error.

### Logging

| Variable                | Description                        | Default        | Example         |
|-------------------------|------------------------------------|:--------------:|-----------------|
| `DJANGO_LOGGING_LEVEL`  | Django logging level               | `WARNING`      | `DEBUG`         |

### Monitoring (LangSmith)

| Variable                | Description                        | Default        | Example         |
|-------------------------|------------------------------------|:--------------:|-----------------|
| `LANGSMITH_TRACING`     | Enable LangSmith tracing           | `false`        | `true`          |
| `LANGSMITH_PROJECT`     | LangSmith project name             | `default`      | `daiv-production` |
| `LANGSMITH_API_KEY` :material-lock:    | LangSmith API key         | *(none)*       | `lsv2_pt_...`   |
| `LANGSMITH_ENDPOINT`    | LangSmith API endpoint             | `https://api.smith.langchain.com` | `https://eu.api.smith.langchain.com` |

!!! note
    LangSmith variables can also use `LANGCHAIN_` prefix (e.g., `LANGCHAIN_TRACING_V2`, `LANGCHAIN_API_KEY`). The `LANGSMITH_API_KEY` supports Docker secrets via the `_FILE` suffix. For setup details, see [Monitoring](monitoring.md).

### Sandbox (client-side)

| Variable                | Description                        | Default        | Example         |
|-------------------------|------------------------------------|:--------------:|-----------------|
| `DAIV_SANDBOX_URL`     | URL of the sandbox service    | `http://sandbox:8000` | `http://sandbox:8000` |
| `DAIV_SANDBOX_TIMEOUT` | Timeout for sandbox requests in seconds        | `600`          | `600`           |
| `DAIV_SANDBOX_API_KEY` :material-lock: | API key for sandbox requests        | *(none)*          | `random-api-key`           |
| `DAIV_SANDBOX_BASE_IMAGE` | Default base image for sandbox sessions | `python:3.12-bookworm` | `node:18-alpine` |
| `DAIV_SANDBOX_EPHEMERAL` | Default ephemeral setting for sandbox sessions | `False` | `True` |
| `DAIV_SANDBOX_NETWORK_ENABLED` | Default network setting for sandbox sessions | `False` | `True` |
| `DAIV_SANDBOX_CPU` | Default CPU limit for sandbox sessions (CPUs) | `None` | `1.0` |
| `DAIV_SANDBOX_MEMORY` | Default memory limit for sandbox sessions (bytes) | `None` | `1073741824` |
| `DAIV_SANDBOX_COMMAND_POLICY_DISALLOW` | Space-separated list of additional bash command prefixes to block globally (e.g. `curl wget`) | `""` (none) | `"curl wget npm publish"` |
| `DAIV_SANDBOX_COMMAND_POLICY_ALLOW` | Space-separated list of bash command prefixes to globally permit, overriding the default policy | `""` (none) | `"my-safe-tool"` |

!!! info
    Check the [daiv-sandbox](https://github.com/srtab/daiv-sandbox) repository for server-side configuration of the sandbox service.

!!! note "Global policy vs. repository policy"
    `DAIV_SANDBOX_COMMAND_POLICY_DISALLOW` and `DAIV_SANDBOX_COMMAND_POLICY_ALLOW` set global defaults. Per-repository overrides are defined in the `.daiv.yml` `sandbox.command_policy` section and are merged at evaluation time. Built-in safety rules (blocking `git commit`, `git push`, etc.) cannot be overridden by either mechanism.

### Other

| Variable                | Description                        | Default        | Example         |
|-------------------------|------------------------------------|:--------------:|-----------------|
| `DAIV_EXTERNAL_URL`     | External URL of the application.   | `https://app:8000` | `https://daiv.example.com` |

!!! note
    The `DAIV_EXTERNAL_URL` variable is used to define webhooks on Git platform. Make sure that the URL is accessible from the Git platform.

---

## Codebase

### General

| Variable            | Description                              | Default   | Example   |
|---------------------|------------------------------------------|:---------:|-----------|
| `CODEBASE_CLIENT`   | Client to use for codebase operations    | `gitlab`  | `gitlab`, `github`, or `swe`  |
| `CODEBASE_WEBHOOK_SETUP_CRON` | Cron expression for periodic webhook setup (GitLab only) | `*/5 * * * *` | `*/10 * * * *` |

!!! note
    Set `CODEBASE_CLIENT` to either `gitlab`, `github`, or `swe` depending on which platform you want to use. Only one platform can be active at a time.

    The `swe` client type is designed for SWE-bench style evaluations and clones public OSS repositories to temporary directories without requiring credentials. It uses ephemeral temporary clones per run and does not cache repositories across runs. Repository identifiers should be in the format `owner/name` (e.g., `psf/requests`).

### GitLab Integration

| Variable                        | Description                                 | Default   | Example              |
|---------------------------------|---------------------------------------------|:---------:|----------------------|
| :material-asterisk: `CODEBASE_GITLAB_URL`            | URL of the GitLab instance                  | *(none)*  | `https://gitlab.com` |
| :material-asterisk: `CODEBASE_GITLAB_AUTH_TOKEN`  :material-lock:    | Authentication token for GitLab             | *(none)*  | `glpat-xyz`          |
| `CODEBASE_GITLAB_WEBHOOK_SECRET` :material-lock:| Secret token for GitLab webhook validation  | *(none)*  | `random-webhook-secret` |

!!! note
    The `CODEBASE_GITLAB_AUTH_TOKEN` is used to authenticate with the GitLab instance using a personal access token with the `api` scope.

### GitHub Integration

| Variable                        | Description                                 | Default   | Example              |
|---------------------------------|---------------------------------------------|:---------:|----------------------|
| `CODEBASE_GITHUB_URL`           | URL of the GitHub instance                  | *(none)*  | `https://github.com` |
| :material-asterisk: `CODEBASE_GITHUB_APP_ID`               | GitHub App ID                               | *(none)*  | `123456`             |
| :material-asterisk: `CODEBASE_GITHUB_INSTALLATION_ID`      | GitHub App Installation ID                  | *(none)*  | `789012`             |
| :material-asterisk: `CODEBASE_GITHUB_PRIVATE_KEY` :material-lock:         | GitHub App private key (PEM format)         | *(none)*  |                      |
| `CODEBASE_GITHUB_WEBHOOK_SECRET` :material-lock:| Secret token for GitHub webhook validation  | *(none)*  | `random-webhook-secret` |

!!! note
    GitHub uses GitHub App authentication. You must create a GitHub App in your account or organization settings. The private key is a multi-line PEM file that should be stored securely using Docker secrets.

!!! info
    For GitHub Enterprise Server, set `CODEBASE_GITHUB_URL` to your GitHub Enterprise URL (e.g., `https://github.your-company.com`). For GitHub.com, this variable can be omitted.

---

## Automation: LLM Providers

This section documents the environment variables for each LLM provider.

!!! note
    At least one of the [supported providers](../getting-started/llm-providers.md) should be configured to use the automation features.

### OpenRouter (*default*)

| Variable                        | Description                | Default                        | Example |
|---------------------------------|----------------------------|:------------------------------:|---------|
| `OPENROUTER_API_KEY` :material-lock: | OpenRouter API key         | *(none)*                       |         |
| `OPENROUTER_API_BASE`| OpenRouter API base URL    | `https://openrouter.ai/api/v1` |         |

### Anthropic

| Variable                        | Description                | Default    | Example |
|---------------------------------|----------------------------|:----------:|---------|
| `ANTHROPIC_API_KEY` :material-lock:  | Anthropic API key          | *(none)*   |         |

### OpenAI

| Variable                        | Description                | Default    | Example |
|---------------------------------|----------------------------|:----------:|---------|
| `OPENAI_API_KEY` :material-lock:     | OpenAI API key             | *(none)*   |         |

### Google

| Variable                        | Description                | Default    | Example |
|---------------------------------|----------------------------|:----------:|---------|
| `GOOGLE_API_KEY` :material-lock:     | Google API key             | *(none)*   |         |

## Automation: Tools

This section documents the environment variables for each tool configuration used by AI agents.

### Context File Suggestion

| Variable                        | Description                                                    | Default        | Example |
|---------------------------------|----------------------------------------------------------------|:--------------:|---------|
| `AUTOMATION_SUGGEST_CONTEXT_FILE_ENABLED` | Enable/disable suggesting an `AGENTS.md` file on new merge requests | `true` | `false` |

### Web Search

| Variable                        | Description                                                    | Default        | Example |
|---------------------------------|----------------------------------------------------------------|:--------------:|---------|
| `AUTOMATION_WEB_SEARCH_ENABLED`     | Enable/disable the `web_search` tool                     | `true`         | `false` |
| `AUTOMATION_WEB_SEARCH_MAX_RESULTS` | Maximum number of results to return from web search      | `5`            |         |
| `AUTOMATION_WEB_SEARCH_ENGINE`  | Web search engine to use (`duckduckgo`, `tavily`)              | `duckduckgo`   | `tavily`|
| `AUTOMATION_WEB_SEARCH_API_KEY` :material-lock: | Web search API key (required if engine is `tavily`)            | *(none)*       |         |

### Web Fetch

The native `web_fetch` tool fetches a URL, converts HTML to markdown, then uses a small/fast model to answer a prompt about the page content.

| Variable                        | Description                                                    | Default        | Example |
|---------------------------------|----------------------------------------------------------------|:--------------:|---------|
| `AUTOMATION_WEB_FETCH_ENABLED`  | Enable/disable the native `web_fetch` tool                     | `true`         | `false` |
| `AUTOMATION_WEB_FETCH_MODEL_NAME` | Model used by `web_fetch` to process page content with the prompt | `claude-haiku-4-5` | `openrouter:openai/gpt-4.1-mini` |
| `AUTOMATION_WEB_FETCH_CACHE_TTL_SECONDS` | Cache TTL (seconds) for repeated fetches                | `900`          | `1800` |
| `AUTOMATION_WEB_FETCH_TIMEOUT_SECONDS` | HTTP timeout for fetching (seconds)                      | `15`           | `30` |
| `AUTOMATION_WEB_FETCH_PROXY_URL` | Optional proxy URL for web fetch HTTP requests                 | *(none)*       | `http://proxy:8080` |
| `AUTOMATION_WEB_FETCH_MAX_CONTENT_CHARS` | Max page content size (characters) to analyze in one pass | `50000` | `80000` |
| `AUTOMATION_WEB_FETCH_AUTH_HEADERS` | Domain-to-headers mapping for authenticated fetches (JSON) | `{}` | `{"example.com": {"X-API-Key": "sk-abc"}}` |

### MCP Tools

MCP (Model Context Protocol) tools extend agent capabilities by providing access to external services and specialized functionality. Each MCP server runs in its own isolated container.

| Variable                        | Description                                                    | Default                        | Example |
|---------------------------------|----------------------------------------------------------------|:------------------------------:|---------|
| `MCP_SERVERS_CONFIG_FILE`       | Path to user-defined MCP servers JSON config file              | *(none)*                       | `/path/to/mcp.json` |
| `MCP_SENTRY_URL`                | SSE URL for the Sentry supergateway container (set to `None` to disable) | `http://mcp-sentry:8000/sse`   | `http://localhost:8001/sse` |
| `MCP_CONTEXT7_URL`              | SSE URL for the Context7 supergateway container (set to `None` to disable) | `http://mcp-context7:8000/sse` | `http://localhost:8002/sse` |

!!! info
    For detailed MCP server configuration including user-defined servers, see [MCP Tools](../customization/mcp-tools.md).

---

## Automation: Agents

These variables control the models and behavior of DAIV's agents. You can also override models per repository via `.daiv.yml` — see [Repository Config](../customization/repository-config.md#model-overrides).

### DAIV Agent

The main agent used for issue addressing, pull request assistance, and all interactive tasks. Variables use the `DAIV_AGENT_` prefix.

| Variable | Description | Default |
|----------------------------------------|----------------------------------------------------------|------------------------|
| `DAIV_AGENT_RECURSION_LIMIT` | Maximum recursion depth for agent execution | `500` |
| `DAIV_AGENT_MODEL_NAME` | Primary model for agent tasks | `claude-sonnet-4-6` |
| `DAIV_AGENT_FALLBACK_MODEL_NAME` | Fallback model if the primary model fails | `gpt-5-3-codex` |
| `DAIV_AGENT_THINKING_LEVEL` | Extended thinking level (`low`, `medium`, `high`, or `None` to disable) | `medium` |
| `DAIV_AGENT_MAX_MODEL_NAME` | Model used when the `daiv-max` label is present | `claude-opus-4-6` |
| `DAIV_AGENT_MAX_THINKING_LEVEL` | Thinking level for `daiv-max` tasks | `high` |
| `DAIV_AGENT_EXPLORE_MODEL_NAME` | Model for the explore subagent (fast, read-only) | `claude-haiku-4-5` |
| `DAIV_AGENT_CUSTOM_SKILLS_PATH` | Path to custom global skills directory. Set to `None` to disable. | `~/data/skills` |

### Jobs API

The [Jobs API](../features/jobs-api.md) allows programmatic agent execution. Variables use the `JOBS_` prefix.

| Variable | Description | Default |
|-------------------------------|----------------------------------------------|------------------------|
| `JOBS_THROTTLE_RATE` | Rate limit for job submissions per authenticated user. Format: `N/second`, `N/minute`, `N/hour`, or `N/day` | `20/hour` |

### Diff to Metadata

Generates pull request titles, descriptions, and commit messages from diffs. Variables use the `DIFF_TO_METADATA_` prefix.

| Variable | Description | Default |
|-------------------------------|----------------------------------------------|------------------------|
| `DIFF_TO_METADATA_MODEL_NAME` | Primary model for diff-to-metadata generation | `claude-haiku-4-5` |
| `DIFF_TO_METADATA_FALLBACK_MODEL_NAME` | Fallback model if the primary model fails | `gpt-4-1-mini` |
