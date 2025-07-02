# Environment Variables

DAIV provides a large number of environment variables that can be used to configure the application. This page lists all environment variables related to core, codebase, automation, and sandbox features.

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
| `DB_POOL_MAX_LIFETIME` | Maximum lifetime of a connection pool in seconds | `30` | `60` |

### Redis

| Variable           | Description                | Default | Example |
|--------------------|----------------------------|:---------:|---------|
| :material-asterisk: `DJANGO_REDIS_URL`  :material-lock: | Redis connection URL | *(none)* | `redis://redis:6379/0` |

### Celery / Broker

| Variable                | Description                        | Default        | Example         |
|-------------------------|------------------------------------|:--------------:|-----------------|
| :material-asterisk: `DJANGO_BROKER_URL` :material-lock:     | Celery broker URL                  | `memory:///`   | `redis://redis:6379/0` |
| `DJANGO_BROKER_USE_SSL` | Use SSL for broker connection      | `False`        | `True`          |
| `CELERY_LOGLEVEL`       | Celery log level                   | `INFO`         | `DEBUG`         |
| `CELERY_CONCURRENCY`    | Number of Celery workers           | `2`            | `4`             |

!!! note
    The `CELERY_CONCURRENCY` variable is used to specify the number of Celery workers to use. This is useful for scaling the application. The default value is `2` which is suitable for most use cases.

### Sentry

| Variable                | Description                        | Default        | Example         |
|-------------------------|------------------------------------|:--------------:|-----------------|
| `SENTRY_DSN` :material-lock:            | Sentry DSN                         | *(none)*       |                 |
| `SENTRY_DEBUG`          | Enable Sentry debug mode           | `False`        | `True`          |
| `SENTRY_ENABLE_TRACING` | Enable Sentry tracing              | `False`        | `True`          |
| `NODE_HOSTNAME`         | Node hostname for Sentry           | *(none)*       |                 |
| `SERVICE_NAME`          | Service name for Sentry            | *(none)*       |                 |

!!! note
    `NODE_HOSTNAME` and `SERVICE_NAME` are used to identify the node and service that is reporting the error.

### Logging

| Variable                | Description                        | Default        | Example         |
|-------------------------|------------------------------------|:--------------:|-----------------|
| `DJANGO_LOGGING_LEVEL`  | Django logging level               | `INFO`         | `DEBUG`         |

### Monitoring (LangSmith)

| Variable                | Description                        | Default        | Example         |
|-------------------------|------------------------------------|:--------------:|-----------------|
| `LANGSMITH_TRACING`     | Enable LangSmith tracing (alternative) | `False`    | `true`          |
| `LANGSMITH_PROJECT`     | LangSmith project name (alternative) | `default`    | `daiv-production` |
| `LANGSMITH_API_KEY` :material-lock:    | LangSmith API key (alternative)    | *(none)*       | `lsv2_pt_...`   |
| `LANGSMITH_API_KEY_FILE` | Path to LangSmith API key file    | *(none)*       | `/run/secrets/langsmith_api_key` |
| `LANGSMITH_ENDPOINT`    | LangSmith API endpoint             | `https://api.smith.langchain.com` | `https://eu.api.smith.langchain.com` |

!!! note
    LangSmith provides comprehensive monitoring and observability for AI agents. For detailed setup instructions, see [Monitoring Configuration](monitoring.md).

### Sandbox (client-side)

| Variable                | Description                        | Default        | Example         |
|-------------------------|------------------------------------|:--------------:|-----------------|
| `DAIV_SANDBOX_URL`     | URL of the sandbox service    | `http://sandbox:8000` | `http://sandbox:8000` |
| `DAIV_SANDBOX_TIMEOUT` | Timeout for sandbox requests in seconds        | `600`          | `600`           |
| `DAIV_SANDBOX_API_KEY` :material-lock: | API key for sandbox requests        | *(none)*          | `random-api-key`           |

!!! info
    Check the [daiv-sandbox](https://github.com/daiv/daiv-sandbox) repository for server-side configuration of the sandbox service.

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
| `CODEBASE_CLIENT`   | Client to use for codebase operations    | `gitlab`  | `gitlab`  |

### GitLab Integration

| Variable                        | Description                                 | Default   | Example              |
|---------------------------------|---------------------------------------------|:---------:|----------------------|
| :material-asterisk: `CODEBASE_GITLAB_URL`            | URL of the GitLab instance                  | *(none)*  | `https://gitlab.com` |
| :material-asterisk: `CODEBASE_GITLAB_AUTH_TOKEN`  :material-lock:    | Authentication token for GitLab             | *(none)*  | `glpat-xyz`          |
| `CODEBASE_GITLAB_WEBHOOK_SECRET` :material-lock:| Secret token for GitLab webhook validation  | *(none)*  | `random-webhook-secret` |

!!! note
    The `CODEBASE_GITLAB_AUTH_TOKEN` is used to authenticate with the GitLab instance using a personal access token with the `api` scope.

### Embeddings

| Variable                        | Description                                 | Default                        | Example                        |
|---------------------------------|---------------------------------------------|:------------------------------:|--------------------------------|
| :material-asterisk: `CODEBASE_EMBEDDINGS_API_KEY`  :material-lock:   | API key for the embeddings provider         | *(none)*                       | `openai-api-key`               |
| `CODEBASE_EMBEDDINGS_MODEL_NAME`| Name of the embeddings model                | `openai/text-embedding-3-large`| `voyageai/voyage-code-3` or `huggingface/Alibaba-NLP/gte-modernbert-base` |
| `CODEBASE_EMBEDDINGS_DIMENSIONS`| Number of dimensions for embeddings         | `1536`                         | `2000`                         |
| `CODEBASE_EMBEDDINGS_BATCH_SIZE`| Batch size for embeddings (OpenAI only)     | `500`                          | `500`                          |
| `CODEBASE_CHUNK_SIZE`           | Size of chunks to split documents into      | `1500`                         | `1500`                         |
| `CODEBASE_CHUNK_OVERLAP`        | Overlap between chunks (non-code languages) | `150`                          | `150`                          |

!!! note
    The `CODEBASE_EMBEDDINGS_MODEL_NAME` is the name of the embeddings model to use. OpenAI, HuggingFace or VoyageAI models are supported. For more embeddings models, check: [MTEB Leaderboard](https://mteb-leaderboard.hf.space/?benchmark_name=CoIR).

!!! danger
    If you change the `CODEBASE_EMBEDDINGS_DIMENSIONS` after the documents have been already indexed, you will need to recreate the `CodebaseDocument` table on the database and re-index all the documents.

---

## Automation

!!! note
    At least one of the [supported providers](supported-providers.md) should be configured to use the automation features.

### OpenRouter (*default*).

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

### Web Search

| Variable                        | Description                                                    | Default        | Example |
|---------------------------------|----------------------------------------------------------------|:--------------:|---------|
| `AUTOMATION_WEB_SEARCH_MAX_RESULTS` | Maximum number of results to return from web search      | `5`            |         |
| `AUTOMATION_WEB_SEARCH_ENGINE`  | Web search engine to use (`duckduckgo`, `tavily`)              | `duckduckgo`   | `tavily`|
| `AUTOMATION_WEB_SEARCH_API_KEY` :material-lock: | Web search API key (required if engine is `tavily`)            | *(none)*       |         |

### MCP Tools

MCP (Model Context Protocol) tools extend agent capabilities by providing access to external services and specialized functionality.

| Variable                        | Description                                                    | Default                        | Example |
|---------------------------------|----------------------------------------------------------------|:------------------------------:|---------|
| `MCP_PROXY_HOST`                | Host URL for the MCP proxy server                             | `http://mcp-proxy:9090`        | `http://localhost:9090` |
| `MCP_PROXY_ADDR`                | Address for the MCP proxy to listen on                        | `:9090`                        | `:9090` |
| `MCP_PROXY_AUTH_TOKEN` :material-lock: | Authentication token for MCP proxy                             | *(none)*                       | `secure-auth-token` |
| `MCP_FETCH_ENABLED`             | Enable/disable Fetch MCP server for web scraping              | `true`                         | `false` |
| `MCP_FETCH_VERSION`             | Version of the Fetch MCP server                               | `2025.4.7`                     | `2025.4.7` |
| `MCP_SENTRY_ENABLED`            | Enable/disable Sentry MCP server for error monitoring         | `true`                         | `false` |
| `MCP_SENTRY_VERSION`            | Version of the Sentry MCP server                              | `0.10.0`                       | `0.10.0` |
| `MCP_SENTRY_ACCESS_TOKEN` :material-lock: | Sentry API access token                                        | *(none)*                       | `sntryu_abc123...` |
| `MCP_SENTRY_HOST`               | Sentry instance hostname                                       | *(none)*                       | `your-org.sentry.io` |

!!! info
    MCP tools are currently available in the **Plan and Execute** agent. The Fetch server provides web scraping capabilities, while the Sentry server enables error monitoring integration. For detailed configuration, see [MCP Tools](../ai-agents/mcp-tools.md).

!!! note
    Sentry MCP server requires both `MCP_SENTRY_ACCESS_TOKEN` and `MCP_SENTRY_HOST` to be configured for functionality.

---

## Automation: AI Agents

This section documents the environment variables for each automation agent. Each agent uses a unique prefix for its variables.

All the default models where chosen to be the most effective models. You can change the models to use other models by setting the corresponding environment variables.

### Codebase Search

| Variable | Description | Default |
|---------------------------|----------------------------------------------------------|------------------------|
| `CODEBASE_SEARCH_NAME` | Name of the codebase search agent. | `CodebaseSearch` |
| `CODEBASE_SEARCH_TOP_N` | Number of results to return from the codebase search. | `10` |
| `CODEBASE_SEARCH_REPHRASE_MODEL_NAME` | Model to rephrase the codebase search query. | `openrouter:openai/gpt-4-1-mini` |
| `CODEBASE_SEARCH_RERANKING_MODEL_NAME`| Model for listwise reranking. | `openrouter:openai/gpt-4-1-mini` |

!!! note
    The `CODEBASE_SEARCH_TOP_N` variable is used to specify the number of code snippets that will be provided to the LLM.

### Code Describer

| Variable | Description | Default |
|-------------------------------|----------------------------------------------|--------------------|
| `CODE_DESCRIBER_NAME` | Name of the code describer agent. | `CodeDescriber` |
| `CODE_DESCRIBER_MODEL_NAME` | Model for code describer. | `openrouter:openai/gpt-4-1-mini` |

### Pull Request Describer

| Variable | Description | Default |
|-------------------------------|----------------------------------------------|------------------------|
| `PR_DESCRIBER_NAME` | Name of the PR describer agent. | `PullRequestDescriber` |
| `PR_DESCRIBER_MODEL_NAME` | Model for PR describer. | `openrouter:openai/gpt-4-1-mini` |

### Pipeline Fixer

| Variable | Description | Default |
|----------------------------------------|----------------------------------------------------------|--------------------|
| `PIPELINE_FIXER_NAME` | Name of the pipeline fixer agent. | `PipelineFixer` |
| `PIPELINE_FIXER_TROUBLESHOOTING_MODEL_NAME` | Model for troubleshooting. | `openrouter:openai/o4-mini` |
| `PIPELINE_FIXER_TROUBLESHOOTING_THINKING_LEVEL` | Thinking level for troubleshooting. | `high` |
| `PIPELINE_FIXER_COMMAND_OUTPUT_MODEL_NAME` | Model for command output evaluator. | `openrouter:openai/gpt-4-1-mini` |

### Plan and Execute

| Variable | Description | Default |
|----------------------------------------|----------------------------------------------------------|------------------------|
| `PLAN_AND_EXECUTE_NAME` | Name of the plan and execute agent. | `PlanAndExecute` |
| `PLAN_AND_EXECUTE_RECURSION_LIMIT` | Recursion limit for planning and execution steps each. | `100` |
| `PLAN_AND_EXECUTE_PLANNING_MODEL_NAME` | Model for planning tasks. | `openrouter:anthropic/claude-sonnet-4` |
| `PLAN_AND_EXECUTE_EXECUTION_MODEL_NAME`| Model for executing tasks. | `openrouter:anthropic/claude-sonnet-4` |
| `PLAN_AND_EXECUTE_HUMAN_APPROVAL_MODEL_NAME` | Model for plan approval evaluation. | `openrouter:openai/gpt-4-1-mini` |

### Issue Addressor

| Variable | Description | Default |
|----------------------------------------|----------------------------------------------------------|--------------------|
| `ISSUE_ADDRESSOR_NAME` | Name of the issue addressor agent. | `IssueAddressor` |
| `ISSUE_ADDRESSOR_ISSUE_EVALUATOR_MODEL_NAME` | Model for issue evaluation. | `openrouter:openai/gpt-4-1-mini` |

### Snippet Replacer

| Variable | Description | Default |
|----------------------------------------|----------------------------------------------------------|------------------------|
| `SNIPPET_REPLACER_NAME` | Name of the snippet replacer agent. | `SnippetReplacer` |
| `SNIPPET_REPLACER_MODEL_NAME` | Model for snippet replacer (LLM strategy). | `openrouter:anthropic/claude-3-5-haiku` |
| `SNIPPET_REPLACER_STRATEGY` | Strategy: `llm` or `find_and_replace`. | `find_and_replace` |

### Codebase Chat

| Variable | Description | Default |
|----------------------------------------|----------------------------------------------------------|--------------------|
| `CODEBASE_CHAT_NAME` | Name of the codebase chat agent. | `CodebaseChat` |
| `CODEBASE_CHAT_MODEL_NAME` | Model for codebase chat. | `openrouter:openai/gpt-4-1-mini` |
| `CODEBASE_CHAT_TEMPERATURE` | Temperature for codebase chat. | `0.2` |

### Review Addressor

| Variable | Description | Default |
|----------------------------------------|----------------------------------------------------------|--------------------|
| `REVIEW_ADDRESSOR_NAME` | Name of the review addressor agent. | `ReviewAddressor` |
| `REVIEW_ADDRESSOR_REVIEW_COMMENT_MODEL_NAME` | Model for review assessment. | `openrouter:openai/gpt-4-1-mini` |
| `REVIEW_ADDRESSOR_REPLY_MODEL_NAME` | Model for reply to comments/questions. | `openrouter:openai/gpt-4-1` |
| `REVIEW_ADDRESSOR_REPLY_TEMPERATURE` | Temperature for the reply model. | `0.2` |

### Image URL Extractor

| Variable | Description | Default |
|----------------------------------------|----------------------------------------------------------|------------------------|
| `IMAGE_URL_EXTRACTOR_NAME` | Name of the image URL extractor agent. | `ImageURLExtractor` |
| `IMAGE_URL_EXTRACTOR_MODEL_NAME` | Model for image URL extraction. | `openrouter:openai/gpt-4-1-nano` |
