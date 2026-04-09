# Environment Variables

DAIV provides a large number of environment variables to configure its behavior. This page lists all supported variables.

Variables marked with:

 * :material-lock: are sensitive (such as API keys, passwords, and tokens) and should be declared using Docker secrets or a secure credential manager.
 * :material-asterisk: are required and should be declared.

!!! info "Configuration UI"
    Many settings listed under [Automation: LLM Providers](#automation-llm-providers), [Automation: Tools](#automation-tools), and [Automation: Agents](#automation-agents) can also be managed through the **Configuration UI** at `/dashboard/configuration/`. These settings use a three-tier priority chain: **environment variable** (highest) > **database value** (set via UI) > **hardcoded default** (lowest). When a setting is overridden by an environment variable, the corresponding field in the UI is shown as locked. Settings marked *env-only* are not available in the UI.

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

### Authentication

DAIV uses [django-allauth](https://docs.allauth.org/) for web authentication. Users are created by admins and sign in via social providers (GitHub, GitLab) or passwordless login-by-code. Social signup is restricted to pre-existing accounts — users must be created by an admin first via the user management interface at `/accounts/users/`. On a fresh install, the first social login is allowed to bootstrap the initial admin account (see [Deployment](../getting-started/deployment.md)). Configure at least one social provider.

| Variable                | Description                        | Default        | Example         |
|-------------------------|------------------------------------|:--------------:|-----------------|
| `ALLAUTH_GITHUB_CLIENT_ID` :material-lock: | GitHub OAuth App client ID | *(none)* | `Iv1.abc123` |
| `ALLAUTH_GITHUB_SECRET` :material-lock: | GitHub OAuth App secret | *(none)* | |
| `ALLAUTH_GITLAB_CLIENT_ID` :material-lock: | GitLab OAuth Application ID | *(none)* | |
| `ALLAUTH_GITLAB_SECRET` :material-lock: | GitLab OAuth Application secret | *(none)* | |
| `ALLAUTH_GITLAB_URL` | GitLab instance URL (for OAuth redirects to the user's browser) | `https://gitlab.com` | `https://gitlab.example.com` |
| `ALLAUTH_GITLAB_SERVER_URL` | GitLab server URL (for server-to-server API calls, if different from `ALLAUTH_GITLAB_URL`) | *(none)* | `http://gitlab:8929` |
| `EMAIL_BACKEND` | Django email backend for login-by-code emails | `django.core.mail.backends.smtp.EmailBackend` | `django.core.mail.backends.console.EmailBackend` |
| `EMAIL_HOST` | SMTP server hostname | `localhost` | `smtp.example.com` |
| `EMAIL_PORT` | SMTP server port | `25` | `587` |
| `EMAIL_HOST_USER` | SMTP authentication username | *(empty)* | `user@example.com` |
| `EMAIL_HOST_PASSWORD` :material-lock: | SMTP authentication password | *(empty)* | |
| `EMAIL_USE_TLS` | Use TLS for SMTP connection | `False` | `True` |
| `DEFAULT_FROM_EMAIL` | Sender address for login-by-code emails | `noreply@daiv.dev` | `noreply@example.com` |

!!! info "Setting up social providers"
    **GitHub**: Create an OAuth App at [github.com/settings/developers](https://github.com/settings/developers). Set the callback URL to `https://<your-domain>/accounts/github/login/callback/`.

    **GitLab**: Create an Application in your GitLab instance under **Admin Area → Applications** or **User Settings → Applications**. Set the redirect URI to `https://<your-domain>/accounts/gitlab/login/callback/` with the `read_user` scope.

!!! note
    Social providers are only registered when **both** client ID and secret are set. If only one is configured, a warning is logged and the provider button is not shown on the login page.

### Other

| Variable                | Description                        | Default        | Example         |
|-------------------------|------------------------------------|:--------------:|-----------------|
| `DAIV_EXTERNAL_URL`     | External URL of the application.   | `https://app:8000` | `https://daiv.example.com` |
| `DAIV_ENCRYPTION_KEY` :material-lock: | Fernet encryption key for secrets stored in the database. If not set, a key is derived from `DJANGO_SECRET_KEY` via HKDF. | *(derived)* | |

!!! note
    The `DAIV_EXTERNAL_URL` variable is used to define webhooks on Git platform and as the site domain for authentication emails. Make sure that the URL is accessible from the Git platform.

!!! note
    `DAIV_ENCRYPTION_KEY` protects API keys and other secrets stored in the configuration database. If you provide a raw Fernet key it is used directly; otherwise the value is treated as a passphrase and a key is derived via HKDF-SHA256. When omitted, the key is derived from `DJANGO_SECRET_KEY` — changing the Django secret key in that case will make existing encrypted values unreadable.

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
| `DAIV_OPENROUTER_API_BASE`| OpenRouter API base URL    | `https://openrouter.ai/api/v1` |         |

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
| `DAIV_SUGGEST_CONTEXT_FILE_ENABLED` | Enable/disable suggesting an `AGENTS.md` file on new merge requests | `true` | `false` |

### Web Search

| Variable                        | Description                                                    | Default        | Example |
|---------------------------------|----------------------------------------------------------------|:--------------:|---------|
| `DAIV_WEB_SEARCH_ENABLED`     | Enable/disable the `web_search` tool                     | `true`         | `false` |
| `DAIV_WEB_SEARCH_MAX_RESULTS` | Maximum number of results to return from web search      | `5`            |         |
| `DAIV_WEB_SEARCH_ENGINE`  | Web search engine to use (`duckduckgo`, `tavily`)              | `duckduckgo`   | `tavily`|
| `DAIV_WEB_SEARCH_API_KEY` :material-lock: | Web search API key (required if engine is `tavily`)            | *(none)*       |         |

### Web Fetch

The native `web_fetch` tool fetches a URL, converts HTML to markdown, then uses a small/fast model to answer a prompt about the page content.

| Variable                        | Description                                                    | Default        | Example |
|---------------------------------|----------------------------------------------------------------|:--------------:|---------|
| `DAIV_WEB_FETCH_ENABLED`  | Enable/disable the native `web_fetch` tool                     | `true`         | `false` |
| `DAIV_WEB_FETCH_MODEL_NAME` | Model used by `web_fetch` to process page content with the prompt | `claude-haiku-4.5` | `openrouter:openai/gpt-4.1-mini` |
| `DAIV_WEB_FETCH_CACHE_TTL_SECONDS` | Cache TTL (seconds) for repeated fetches                | `900`          | `1800` |
| `DAIV_WEB_FETCH_TIMEOUT_SECONDS` | HTTP timeout for fetching (seconds)                      | `15`           | `30` |
| `AUTOMATION_WEB_FETCH_PROXY_URL` | Optional proxy URL for web fetch HTTP requests (env-only)      | *(none)*       | `http://proxy:8080` |
| `DAIV_WEB_FETCH_MAX_CONTENT_CHARS` | Max page content size (characters) to analyze in one pass | `50000` | `80000` |
| `AUTOMATION_WEB_FETCH_AUTH_HEADERS` | Domain-to-headers mapping for authenticated fetches (JSON, env-only) | `{}` | `{"example.com": {"X-API-Key": "sk-abc"}}` |

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
| `DAIV_AGENT_THINKING_LEVEL` | Extended thinking level (`minimal`, `low`, `medium`, `high`, or `None` to disable) | `medium` |
| `DAIV_AGENT_MAX_MODEL_NAME` | Model used when the `daiv-max` label is present | `claude-opus-4-6` |
| `DAIV_AGENT_MAX_THINKING_LEVEL` | Thinking level for `daiv-max` tasks | `high` |
| `DAIV_AGENT_EXPLORE_MODEL_NAME` | Model for the explore subagent (fast, read-only) | `claude-haiku-4-5` |
| `DAIV_AGENT_EXPLORE_FALLBACK_MODEL_NAME` | Fallback model if the explore model fails | `gpt-5-4-mini` |
| `DAIV_AGENT_CUSTOM_SKILLS_PATH` | Path to custom global skills directory. Set to `None` to disable. | `~/data/skills` |

### Jobs API

The [Jobs API](../features/jobs-api.md) allows programmatic agent execution.

| Variable | Description | Default |
|-------------------------------|----------------------------------------------|------------------------|
| `DAIV_JOBS_THROTTLE_RATE` | Rate limit for job submissions per authenticated user. Format: `N/second`, `N/minute`, `N/hour`, or `N/day` | `20/hour` |

### Diff to Metadata

Generates pull request titles, descriptions, and commit messages from diffs.

| Variable | Description | Default |
|-------------------------------|----------------------------------------------|------------------------|
| `DAIV_DIFF_TO_METADATA_MODEL_NAME` | Primary model for diff-to-metadata generation | `gpt-5.4-mini` |
| `DAIV_DIFF_TO_METADATA_FALLBACK_MODEL_NAME` | Fallback model if the primary model fails | `claude-haiku-4.5` |
