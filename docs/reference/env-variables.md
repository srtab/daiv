---
description: "Reference for every environment variable that configures DAIV, including which ones are sensitive and should be provided as Docker secrets."
---

# Environment variables

This page lists the environment variables DAIV reads, with the default and an example for each. Use it to find the variable for a setting, or to check a default you rely on.

In the tables, :material-lock: marks a sensitive variable (an API key, password or token) that you should provide as a Docker secret or from a secure credential manager, and :material-asterisk: marks a variable you must set.

[Site Configuration](site-configuration.md) can change many of these settings at runtime. A variable that has a Site Configuration field overrides the stored value and locks the field; [How environment variables and the UI interact](site-configuration.md#how-environment-variables-and-the-ui-interact) gives the order. Variables marked *env-only* have no field there. The provider API key variables are a different case: DAIV reads them once, as [LLM providers](#llm-providers) explains.

## Core

### Django

| Variable                | Description                                         | Default                | Example                        |
|-------------------------|-----------------------------------------------------|:----------------------:|--------------------------------|
| `DJANGO_DEBUG`          | Toggle Django debug mode                            | `False`                | `True`                         |
| :material-asterisk: `DJANGO_SECRET_KEY`  :material-lock:     | Secret key for Django                              | *(none)*               | `super-secret-key`             |
| `DJANGO_ALLOWED_HOSTS`  | Comma-separated list of allowed hosts               | `*`                    | `example.com,localhost`        |
| `DJANGO_SESSION_COOKIE_NAME` | Name of the session cookie; give each instance on one host its own | `__Secure-sessionid` | `__Secure-sessionid-1` |

`DJANGO_ALLOWED_HOSTS` limits the hosts that can reach DAIV. The default `*` allows any host, so list the host DAIV runs on to restrict it.

!!! warning
    Do not turn on `DJANGO_DEBUG` in production. It exposes sensitive information and breaks the security of the application.

!!! warning
    Keep `DJANGO_SECRET_KEY` stable and backed up. Unless you set `DAIV_ENCRYPTION_KEY`, DAIV derives the key that encrypts stored secrets from it, so a lost or changed `DJANGO_SECRET_KEY` makes every stored secret unreadable. See [External URL and encryption key](#external-url-and-encryption-key).

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
| `DB_KEEPALIVES_IDLE` | Seconds a pooled connection may sit idle before a TCP keepalive probe is sent | `60` | `120` |
| `DB_KEEPALIVES_INTERVAL` | Seconds between TCP keepalive probes on a pooled connection | `10` | `10` |
| `DB_KEEPALIVES_COUNT` | Unanswered keepalive probes before a pooled connection is considered dead | `5` | `5` |

### Redis

| Variable           | Description                | Default | Example |
|--------------------|----------------------------|:---------:|---------|
| :material-asterisk: `DJANGO_REDIS_URL`  :material-lock: | Redis connection URL for cache (DB 0) | *(none)* | `redis://redis:6379/0` |
| `DJANGO_REDIS_SESSION_URL`  :material-lock: | Redis connection URL for sessions (DB 1) | Value of `DJANGO_REDIS_URL` | `redis://redis:6379/1` |
| `DJANGO_REDIS_CHECKPOINT_URL`  :material-lock: | Redis connection URL for LangGraph checkpoints. Needs Redis 8 or later and database `0`, the only one Redis search can index | Value of `DJANGO_REDIS_URL` | `redis://redis:6379/0` |
| `DJANGO_REDIS_CHECKPOINT_TTL_MINUTES` | TTL in minutes for LangGraph checkpoint data, the agent's working copy; an expired session resumes from its database archive. Each write expires on its own, so the agent no longer sees the oldest messages of a thread active for longer than this; the archive keeps them | `10080` (7 days) | `1440` |


### Sentry

| Variable                | Description                        | Default        | Example         |
|-------------------------|------------------------------------|:--------------:|-----------------|
| `SENTRY_DSN` :material-lock:            | Sentry DSN                         | *(none)*       |                 |
| `ENVIRONMENT`           | Deployment environment name, reported to Sentry as the event environment | *(none)* | `production` |
| `SENTRY_DEBUG`          | Enable Sentry debug mode           | `False`        | `True`          |
| `SENTRY_ENABLE_LOGS`    | Enable Sentry logs                 | `False`        | `True`          |
| `SENTRY_TRACES_SAMPLE_RATE` | Sentry traces sample rate          | `0.0`          | `1.0`           |
| `SENTRY_PROFILES_SAMPLE_RATE` | Sentry profiles sample rate        | `0.0`          | `1.0`           |
| `SENTRY_SEND_DEFAULT_PII` | Enable Sentry default PII           | `False`        | `True`          |
| `NODE_HOSTNAME`         | Node hostname, reported to Sentry as the event server name | *(none)*       |                 |
| `SERVICE_NAME`          | Service name, reported to Sentry as the `service_name` tag | *(none)*       |                 |

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

The LangSmith variables also accept the `LANGCHAIN_` prefix, for example `LANGCHAIN_TRACING_V2` and `LANGCHAIN_API_KEY`. `LANGSMITH_API_KEY` also accepts a Docker secret through the `_FILE` suffix. For setup, see [Monitoring](monitoring.md).

### Time zone

DAIV runs in the `Europe/Lisbon` time zone. It is fixed in the settings and is not a variable. A time you give without an offset, such as a schedule's run time or the value of `create_api_key --expires-at`, is read in that zone. Add an offset, for example `+00:00`, to give a time in another zone.

### Sandbox (client-side)

| Variable                | Description                        | Default        | Example         |
|-------------------------|------------------------------------|:--------------:|-----------------|
| `DAIV_SANDBOX_URL`     | URL of the sandbox service    | `http://sandbox:8000` | `http://sandbox:8000` |
| `DAIV_SANDBOX_TIMEOUT` | Timeout for sandbox requests in seconds, and the longest timeout the agent can give a bash command, whose request waits up to 60 seconds longer | `600`          | `600`           |
| `DAIV_SANDBOX_API_KEY` :material-lock: | API key for sandbox requests        | *(none)*          | `random-api-key`           |
| `DAIV_SANDBOX_COMMAND_POLICY_DISALLOW` | JSON array of extra bash command rules to block globally | *(none)* | `'["curl", "npm publish"]'` |
| `DAIV_SANDBOX_COMMAND_POLICY_ALLOW` | JSON array of bash command rules to permit; has no effect (see [Precedence](../features/sandbox.md#precedence)) | *(none)* | `'["my-safe-tool"]'` |

Both command policy settings take a JSON array. A plain string such as `curl wget`, or an empty value, fails to parse and DAIV does not start. The outer single quotes in the examples are YAML or shell quoting, not part of the value. For how entries are matched and which rules are built in, see [Sandbox](../features/sandbox.md#global-rules).

The sandbox service has its own settings; see [Deployment files](#deployment-files) and the [daiv-sandbox](https://github.com/srtab/daiv-sandbox) repository.

### Authentication

DAIV uses [django-allauth](https://docs.allauth.org/) for web authentication. Users sign in with a Git platform account (GitHub or GitLab) or with a passwordless login code sent by email. Email signup is off. For how accounts are created and who becomes the first admin, see [Accounts and roles](../getting-started/accounts.md#sign-in).

The OAuth variables below seed the **Authentication** group of Site Configuration, which also holds an **Enable OAuth login** toggle. To create the OAuth app that gives you the client ID and secret, see [Create the OAuth app](../getting-started/accounts.md#create-the-oauth-app).

| Variable                | Description                        | Default        | Example         |
|-------------------------|------------------------------------|:--------------:|-----------------|
| `DAIV_AUTH_LOGIN_ENABLED` | Show the social provider button on the login page | `false` | `true` |
| `DAIV_AUTH_SIGNUP_OPEN` | Allow anyone authenticating via the configured Git platform to self-create an account. When off, only admin-pre-created users can sign in | `false` | `true` |
| `ALLAUTH_CLIENT_ID` :material-lock: | OAuth client ID for the configured Git platform | *(none)* | `Iv1.abc123` |
| `ALLAUTH_CLIENT_SECRET` :material-lock: | OAuth client secret for the configured Git platform | *(none)* | |
| `ALLAUTH_GITLAB_URL` | GitLab instance URL (for OAuth redirects to the user's browser) | `https://gitlab.com` | `https://gitlab.example.com` |
| `ALLAUTH_GITLAB_SERVER_URL` | GitLab server URL (for server-to-server API calls, if different from `ALLAUTH_GITLAB_URL`) | *(none)* | `http://gitlab:8929` |
| `EMAIL_BACKEND` | Django email backend for login-by-code emails | `django.core.mail.backends.smtp.EmailBackend` | `django.core.mail.backends.console.EmailBackend` |
| `EMAIL_HOST` | SMTP server hostname | `localhost` | `smtp.example.com` |
| `EMAIL_PORT` | SMTP server port | `25` | `587` |
| `EMAIL_HOST_USER` | SMTP authentication username | *(empty)* | `user@example.com` |
| `EMAIL_HOST_PASSWORD` :material-lock: | SMTP authentication password | *(empty)* | |
| `EMAIL_USE_TLS` | Use TLS for SMTP connection | `False` | `True` |
| `DEFAULT_FROM_EMAIL` | Sender address for login-by-code emails | `noreply@daiv.dev` | `noreply@example.com` |

OAuth login needs all three of the `DAIV_AUTH_LOGIN_ENABLED` toggle, a client ID and a client secret. If you set only one of the client ID and the secret, DAIV logs a warning and does not show the provider button on the login page. The `CODEBASE_CLIENT` setting decides which provider, GitHub or GitLab, DAIV offers.

With `EMAIL_BACKEND` set to `django.core.mail.backends.console.EmailBackend`, DAIV writes login-code emails to the app's output instead of sending them, which is enough to sign in without an SMTP server.

### Rocket.Chat

Enabling the **Rocket Chat** group offers Rocket.Chat as a notification channel that users can opt into.

| Variable                | Description                        | Default        | Example         |
|-------------------------|------------------------------------|:--------------:|-----------------|
| `DAIV_ROCKETCHAT_ENABLED` | Offer Rocket Chat as a notification channel for users | `false` | `true` |
| `DAIV_ROCKETCHAT_URL`   | Base URL of your Rocket Chat instance | *(none)* | `https://rc.example.com` |
| `DAIV_ROCKETCHAT_USER_ID` | The bot user's `_id`, sent as the `X-User-Id` header | *(none)* | `abc123userid` |
| `DAIV_ROCKETCHAT_AUTH_TOKEN` :material-lock: | The bot user's auth token, sent as the `X-Auth-Token` header | *(none)* | |

### Telegram

Enabling Telegram offers it as a notification channel that users can opt into. DAIV derives the bot username and generates the webhook secret itself; see [Telegram derived values](site-configuration.md#telegram-derived-values).

| Variable                | Description                        | Default        | Example         |
|-------------------------|------------------------------------|:--------------:|-----------------|
| `DAIV_TELEGRAM_ENABLED` | Offer Telegram as a notification channel for users | `false` | `true` |
| `DAIV_TELEGRAM_BOT_TOKEN` :material-lock: | Bot token issued by @BotFather, in the form `123456:ABC-DEF` | *(none)* | |

Two more variables exist, but leave them unset: each pins a value that DAIV otherwise derives.

| Variable                | Description                        | Default        | Example         |
|-------------------------|------------------------------------|:--------------:|-----------------|
| `DAIV_TELEGRAM_BOT_USERNAME` | Pins the bot username. A value that disagrees with the token breaks the connect link, and nothing corrects it. | *(derived)* | `daiv_bot` |
| `DAIV_TELEGRAM_WEBHOOK_SECRET` :material-lock: | Pins the webhook secret. It must not be empty: a blank value makes the webhook reject every update. | *(generated)* | |

### External URL and encryption key

| Variable                | Description                        | Default        | Example         |
|-------------------------|------------------------------------|:--------------:|-----------------|
| `DAIV_EXTERNAL_URL`     | External URL of the application.   | `https://127.0.0.1:8000` | `https://daiv.example.com` |
| `DAIV_ENCRYPTION_KEY` :material-lock: | Fernet encryption key for secrets stored in the database. If not set, a key is derived from `DJANGO_SECRET_KEY` via HKDF. | *(derived)* | |

DAIV uses `DAIV_EXTERNAL_URL` to define webhooks on the Git platform and as the site domain in authentication emails, so the Git platform must be able to reach it.

`DAIV_ENCRYPTION_KEY` protects the secrets DAIV stores in its database: provider API keys, the Tavily web search API key, the OAuth client secret, the sandbox API key, the Rocket.Chat token, the Telegram bot token and webhook secret, web fetch header values, MCP server headers, and sandbox environment variables and egress secrets. DAIV uses a raw Fernet key directly. Any other value is a passphrase, from which DAIV derives a key with HKDF-SHA256. When the variable is unset, DAIV derives the key from `DJANGO_SECRET_KEY`.

!!! warning
    Back up `DAIV_ENCRYPTION_KEY`, or `DJANGO_SECRET_KEY` when you leave it unset. If the key is lost or changed, DAIV cannot decrypt the stored secrets, and you must enter them again.

### Analytics

| Variable                | Description                        | Default        | Example         |
|-------------------------|------------------------------------|:--------------:|-----------------|
| `DAIV_UMAMI_WEBSITE_ID` | [Umami](https://umami.is/) website ID. When set, the public homepage and `/compare/` pages load Umami's tracker for signed-out visitors. | *(none)* | `0b6bd0c4-5d6c-4f2e-9a43-1d2e3f4a5b6c` |
| `DAIV_UMAMI_SCRIPT_URL` | URL of Umami's tracker script; set it to use a self-hosted Umami. | `https://cloud.umami.is/script.js` | `https://stats.example.com/script.js` |

Analytics is off unless you set `DAIV_UMAMI_WEBSITE_ID`, so DAIV sends no visitor data by default. When it is set, the tracker uses no cookies, honours the browser's Do Not Track setting, and never loads for signed-in users or on dashboard pages.

---

## Codebase

### Git platform

| Variable            | Description                              | Default   | Example   |
|---------------------|------------------------------------------|:---------:|-----------|
| `CODEBASE_CLIENT`   | Client to use for codebase operations    | `gitlab`  | `gitlab` or `github`  |
| `CODEBASE_WEBHOOK_SETUP_CRON` | Cron expression for periodic webhook setup (GitLab only) | `*/5 * * * *` | `*/10 * * * *` |
| `CODEBASE_REPO_ACCESS_SYNC_CRON` | Cron expression for the periodic repository access sync | `*/15 * * * *` | `*/10 * * * *` |
| `CODEBASE_REPO_ACCESS_HARD_TTL_HOURS` | Hours a repository's synced access data stays trusted before it is denied (fails closed); tracked per repository | `24` | `12` |

Set `CODEBASE_CLIENT` to `gitlab` or `github`. Only one platform is active at a time.

`CODEBASE_REPO_ACCESS_SYNC_CRON` sets how often DAIV copies each user's repository membership from the Git platform into its database, with one member-list call per repository. Admins are exempt from repository authorization and always have full access. DAIV tracks freshness per repository: if a repository's sync stops succeeding, for example because the platform token loses access to it, DAIV keeps using the last known access data until `CODEBASE_REPO_ACCESS_HARD_TTL_HOURS` elapse. After that the repository denies access, while the others keep syncing. See [Accounts and roles](../getting-started/accounts.md#repository-access) for the access model.

### GitLab

| Variable                        | Description                                 | Default   | Example              |
|---------------------------------|---------------------------------------------|:---------:|----------------------|
| :material-asterisk: `CODEBASE_GITLAB_URL`            | URL of the GitLab instance                  | *(none)*  | `https://gitlab.com` |
| :material-asterisk: `CODEBASE_GITLAB_AUTH_TOKEN`  :material-lock:    | Authentication token for GitLab             | *(none)*  | `glpat-xyz`          |
| :material-asterisk: `CODEBASE_GITLAB_WEBHOOK_SECRET` :material-lock:| Secret token for GitLab webhook validation. Without it, every GitLab webhook request is rejected with 401 | *(none)*  | `random-webhook-secret` |

`CODEBASE_GITLAB_AUTH_TOKEN` is a personal access token with the `api` scope.

For `git clone` and `git push`, DAIV does not put this token in the repository workspace. It creates a short-lived project access token for each repository instead: named `daiv-clone`, with the `write_repository` scope and the Developer access level, and valid for 48 to 72 hours. DAIV reuses a repository's token for up to 24 hours, so expect a few `daiv-clone` bot members on a project at a time. GitLab removes expired ones.

Creating these tokens needs the token's user to hold at least the Maintainer role on each project and, on GitLab.com, the Premium or Ultimate tier. Self-managed instances accept any tier. When DAIV cannot create a token, it logs a warning and falls back to the personal access token.

The project access token pushes at the Developer level. If a branch protection rule matches the branches DAIV pushes to, for example a wildcard pattern that limits pushes to Maintainers, allow Developers to push on that pattern, or pushes fail.

!!! warning
    Revoking a `daiv-clone` token early makes the next clone of that project fail authentication until DAIV drops the cached token and creates a new one, which happens after about 15 seconds of retries. The cache entry is `codebase:gitlab:clone-token:<project-id>`.

### GitHub

| Variable                        | Description                                 | Default   | Example              |
|---------------------------------|---------------------------------------------|:---------:|----------------------|
| `CODEBASE_GITHUB_URL`           | URL of the GitHub instance                  | *(none)*  | `https://github.com` |
| :material-asterisk: `CODEBASE_GITHUB_APP_ID`               | GitHub App ID                               | *(none)*  | `123456`             |
| :material-asterisk: `CODEBASE_GITHUB_INSTALLATION_ID`      | GitHub App Installation ID                  | *(none)*  | `789012`             |
| :material-asterisk: `CODEBASE_GITHUB_PRIVATE_KEY` :material-lock:         | GitHub App private key (PEM format)         | *(none)*  |                      |
| :material-asterisk: `CODEBASE_GITHUB_WEBHOOK_SECRET` :material-lock:| Secret token for GitHub webhook validation. Without it, every GitHub webhook request is rejected with 401 | *(none)*  | `random-webhook-secret` |

GitHub uses GitHub App authentication, so you create a GitHub App in your account or organization settings. The private key is a multi-line PEM file; store it as a Docker secret. For GitHub Enterprise Server, set `CODEBASE_GITHUB_URL` to its URL, for example `https://github.example.com`. For GitHub.com, you can omit it.

---

## LLM providers

Configure at least one provider to use the agent. The [LLM providers](../getting-started/llm-providers.md) page explains how model specs pick a provider.

!!! warning "Provider keys are seed-only"
    DAIV reads the provider API key variables once, when it first creates the provider records, and never again. They are not part of the environment, database and default order that other settings follow. Changing or adding a `*_API_KEY` variable or secret after the provider record exists has no effect: edit the provider under **Configuration → Providers** instead, which holds the key and the base URL from then on. A provider created without a key starts disabled.

DAIV reads each key from a Docker secret named after the variable in lowercase, for example `openrouter_api_key` mounted at `/run/secrets/openrouter_api_key`, or else from the environment variable. The secret wins when both are set. The OpenRouter base URL is a **Base URL** field of the provider and defaults to `https://openrouter.ai/api/v1`. [Providers](site-configuration.md#providers) describes the fields.

### OpenRouter (default)

| Variable                        | Description                | Default                        | Example |
|---------------------------------|----------------------------|:------------------------------:|---------|
| `OPENROUTER_API_KEY` :material-lock: | OpenRouter API key (seeds the OpenRouter provider record on first run) | *(none)*                       |         |

### Anthropic

| Variable                        | Description                | Default    | Example |
|---------------------------------|----------------------------|:----------:|---------|
| `ANTHROPIC_API_KEY` :material-lock:  | Anthropic API key (seeds the Anthropic provider record on first run) | *(none)*   |         |

### OpenAI

| Variable                        | Description                | Default    | Example |
|---------------------------------|----------------------------|:----------:|---------|
| `OPENAI_API_KEY` :material-lock:     | OpenAI API key (seeds the OpenAI provider record on first run) | *(none)*   |         |

### Google

| Variable                        | Description                | Default    | Example |
|---------------------------------|----------------------------|:----------:|---------|
| `GOOGLE_API_KEY` :material-lock:     | Google API key (seeds the Google provider record on first run) | *(none)*   |         |

## Agent tools

These variables configure the tools the agent calls.

### Context file suggestion

| Variable                        | Description                                                    | Default        | Example |
|---------------------------------|----------------------------------------------------------------|:--------------:|---------|
| `DAIV_SUGGEST_CONTEXT_FILE_ENABLED` | Enable/disable suggesting an `AGENTS.md` file on new merge requests | `true` | `false` |

### Web search

| Variable                        | Description                                                    | Default        | Example |
|---------------------------------|----------------------------------------------------------------|:--------------:|---------|
| `DAIV_WEB_SEARCH_ENABLED`     | Enable/disable the `web_search` tool                     | `true`         | `false` |
| `DAIV_WEB_SEARCH_MAX_RESULTS` | Maximum number of results to return from web search      | `5`            |         |
| `DAIV_WEB_SEARCH_ENGINE`  | Web search engine to use (`duckduckgo`, `tavily`, `searxng`)   | `duckduckgo`   | `searxng`|
| `DAIV_WEB_SEARCH_API_KEY` :material-lock: | Web search API key (required if engine is `tavily`)            | *(none)*       |         |
| `DAIV_WEB_SEARCH_SEARXNG_URL` | Complete SearXNG search endpoint, including `/search` and any deployment prefix | *(none)* | `https://search.example.com/search` |
| `DAIV_WEB_SEARCH_SEARXNG_TIMEOUT_SECONDS` | SearXNG HTTP timeout in seconds (1–120); does not change other engines | `15` | `30` |

To use SearXNG, see [Set up SearXNG](site-configuration.md#set-up-searxng).

### Web fetch

The native `web_fetch` tool fetches a URL, converts HTML to markdown, and has a small model answer a prompt about the page. A `#fragment` on an HTML page's URL limits it to the section that anchor names. A page over the maximum content size is rejected with a list of its section anchors.

| Variable                        | Description                                                    | Default        | Example |
|---------------------------------|----------------------------------------------------------------|:--------------:|---------|
| `DAIV_WEB_FETCH_ENABLED`  | Enable/disable the native `web_fetch` tool                     | `true`         | `false` |
| `DAIV_WEB_FETCH_MODEL_NAME` | Model used by `web_fetch` to process page content with the prompt | `openrouter:anthropic/claude-haiku-5.5` | `openrouter:openai/gpt-4.1-mini` |
| `DAIV_WEB_FETCH_CACHE_TTL_SECONDS` | Cache TTL (seconds) for repeated fetches                | `900`          | `1800` |
| `DAIV_WEB_FETCH_TIMEOUT_SECONDS` | HTTP timeout for fetching (seconds)                      | `15`           | `30` |
| `AUTOMATION_WEB_FETCH_PROXY_URL` | Optional proxy URL for web fetch HTTP requests (env-only)      | *(none)*       | `http://proxy:8080` |
| `DAIV_WEB_FETCH_MAX_CONTENT_CHARS` | Max page content size (characters) to analyze in one pass | `50000` | `80000` |
| `DAIV_WEB_FETCH_AUTH_HEADERS` | Domain-to-headers mapping for authenticated fetches (JSON, env-only) | `{}` | `{"example.com": {"X-API-Key": "sk-abc"}}` |

### Artifacts

The `publish_artifact` tool stores files the agent generates (reports, datasets, charts) as run [artifacts](../features/sessions.md#artifacts), served from the dashboard. Files are written to Django's default file storage (`MEDIA_ROOT`, `~/data/media` in the containers), which the web and worker containers must share.

| Variable                        | Description                                                    | Default        | Example |
|---------------------------------|----------------------------------------------------------------|:--------------:|---------|
| `DAIV_ARTIFACT_MAX_BYTES`       | Maximum size of one published file, in bytes (at most 64 MiB, the sandbox download limit) | `10485760` (10 MiB) | `52428800` |
| `DAIV_ARTIFACTS_PER_RUN_MAX`    | Maximum number of files one run may publish                    | `20`           | `50` |
| `DAIV_TOOL_MEDIA_MAX_BYTES`     | Maximum size of one image or file kept from a tool result for the transcript, in bytes | `10485760` (10 MiB) | `20971520` |
| `DAIV_TOOL_MEDIA_PER_RUN_MAX`   | Maximum number of images and files kept from one run's tool results | `100`  | `200` |

### Session transcript

| Variable                        | Description                                                    | Default        | Example |
|---------------------------------|----------------------------------------------------------------|:--------------:|---------|
| `DAIV_SHOW_RAW_TOOL_DATA`       | Show each tool call's raw arguments and result in a closed **Raw** section of the call's details in the session transcript (env-only) | *(follows `DJANGO_DEBUG`)* | `true` |

### MCP tools

MCP (Model Context Protocol) tools give the agent access to external services. DAIV stores MCP servers as database rows, which you manage at `/dashboard/mcp-servers/`, and connects to them directly, with no sidecar containers. For user-defined servers, see [MCP Tools](../customization/mcp-tools.md).

| Variable                        | Description                                                    | Default                        | Example |
|---------------------------------|----------------------------------------------------------------|:------------------------------:|---------|
| `MCP_TOOL_LOAD_TIMEOUT`         | Max seconds to wait for a single MCP server to return its tools before skipping it (keeps a broken/slow server from freezing chats and runs) | `30` | `10` |

### Deferred tools

With deferred tools on, DAIV loads tools outside the always-loaded set on demand through a `tool_search` tool, instead of binding them to the model up front. Most deployments do not need to change these variables.

| Variable                        | Description                                                    | Default        | Example |
|---------------------------------|----------------------------------------------------------------|:--------------:|---------|
| `DEFERRED_TOOLS_ENABLED`        | Defer non-essential tools behind `tool_search` instead of binding them eagerly | `true` | `false` |
| `DEFERRED_TOOLS_TOP_K_DEFAULT`  | Default number of results returned by a `tool_search` call     | `3`            | `5` |
| `DEFERRED_TOOLS_TOP_K_MAX`      | Maximum number of results `tool_search` will return per call   | `10`           | `20` |
| `DEFERRED_TOOLS_FROZEN_TOOLS_MODELS` | Prefix-matched model names whose bound tools array is kept frozen (loaded schemas reach them via `tool_search` results only), preserving the provider prompt cache. Empty list disables freezing. | `claude-,anthropic/claude-,qwen/qwen3.8-max,minimax/minimax-m3,google/gemini-3.7-flash` | `claude-` |
| `DEFERRED_TOOLS_INLINE_TOOLS_MODELS` | Prefix-matched model names that get each loaded tool declared mid-conversation, right after the `tool_search` result that loaded it, through the provider's native mechanism (Anthropic `tool_addition`, OpenAI Responses `additional_tools`). The tools array stays frozen, so loading a tool keeps the prompt cache, and the model calls a declared tool. Applies only to models reached through the Anthropic API, or through an OpenAI-typed provider over the Responses API (the provider's Responses API setting is on, or the model is GPT 6, which langchain-openai sends there whenever tools are bound); on the Anthropic API, only to models langchain-anthropic keeps mid-conversation system messages in place for (not Claude Sonnet 5 or Haiku 5.5, for example). Empty list disables it. | `claude-,gpt-6-sol,gpt-6-luna,gpt-5.6-luna` | `claude-opus-5` |
| `DEFERRED_TOOLS_EMBED_SCHEMAS_IN_RESULTS` | Embed each loaded tool's full schema in the `tool_search` result. Emergency valve: when `false`, results carry summaries only **and freezing and inline definitions are forced off**, so every model falls back to the array-append + summary pre-change behaviour. | `true` | `false` |

---

## Agents

These variables set the models and behavior of DAIV's agents. A repository can override the models in `.daiv.yml`; see [Model overrides](../customization/repository-config.md#model-overrides).

### DAIV agent

The main agent handles issue addressing, pull request assistance and all interactive tasks. Its variables use the `DAIV_AGENT_` prefix.

| Variable | Description | Default |
|----------------------------------------|----------------------------------------------------------|------------------------|
| `DAIV_AGENT_RECURSION_LIMIT` | Maximum recursion depth for agent execution | `500` |
| `DAIV_AGENT_MODEL_NAME` | Primary model for agent tasks | `openrouter:anthropic/claude-sonnet-5.5` |
| `DAIV_AGENT_FALLBACK_MODEL_NAME` | Fallback model if the primary model fails | `openrouter:openai/gpt-6.1-sol` |
| `DAIV_AGENT_THINKING_LEVEL` | Extended thinking level (`minimal`, `low`, `medium`, `high`, `xhigh`, in any case; empty or `off` to disable) | `medium` |
| `DAIV_AGENT_FALLBACK_THINKING_LEVEL` | Thinking level applied when the primary model fails over to the fallback model (same values as `DAIV_AGENT_THINKING_LEVEL`, set independently of it) | `medium` |
| `DAIV_AGENT_MAX_MODEL_NAME` | Model used when the `daiv-max` label is present | `openrouter:anthropic/claude-opus-5.5` |
| `DAIV_AGENT_MAX_THINKING_LEVEL` | Thinking level for `daiv-max` tasks (same values as `DAIV_AGENT_THINKING_LEVEL`) | `high` |
| `DAIV_AGENT_SKILL_MODELS` | Model per skill name, as a JSON object (`{"code-review": "openrouter:anthropic/claude-sonnet-5.5"}`); see [Agent Skills](../customization/agent-skills.md#running-a-skill-on-another-model). A null or empty entry is unset; a malformed value is logged and ignored. | `{}` |
| `DAIV_AGENT_SUBAGENT_MODELS` | Model per subagent name, as a JSON object (`{"explore": "openrouter:anthropic/claude-haiku-5.5"}`). See [Subagents](../features/subagents.md#subagent-model-precedence). A null or empty entry is unset; a malformed value is logged and ignored. The deprecated `DAIV_AGENT_EXPLORE_MODEL_NAME` is read as the `explore` entry when there is none, and DAIV logs an error. | `{}` |
| `DAIV_AGENT_CUSTOM_SKILLS_PATH` | Path to custom global skills directory. Set to `None` to disable. | `~/data/skills` |
| `DAIV_SESSION_LINK_ENABLED` | Link back to the DAIV sessions that produced the changes, from the descriptions of merge requests DAIV opens and from a DAIV-Session trailer on every commit. Disable to keep the DAIV URL off the git platform | `true` |

### Model requests

These bound every LLM API call, whatever the provider: the main agent, subagents, and the model-backed tasks (titling, memory, commit/PR writer, web fetch). Without them a stalled provider can block a run indefinitely.

| Variable | Description | Default |
|--------------------------------------|---------------------------------------------------------------------------------------|---------|
| `DAIV_MODEL_REQUEST_TIMEOUT_SECONDS` | Per-request timeout for LLM API calls, in seconds. Minimum `1`. | `120` |
| `DAIV_MODEL_MAX_RETRIES` | Retries for a failed LLM API call before the error propagates (`0` disables retries) | `2` |

### Jobs API

The [Jobs API](../features/jobs-api.md) runs the agent from your own code.

| Variable | Description | Default |
|-------------------------------|----------------------------------------------|------------------------|
| `DAIV_JOBS_THROTTLE_RATE` | Rate limit for job submissions per authenticated user. Format: `N/sec`, `N/min`, `N/hour`, or `N/day`, or the short forms `N/s`, `N/m`, `N/h` and `N/d` | `20/hour` |

### Diff to metadata

Generates pull request titles, descriptions and commit messages from diffs.

| Variable | Description | Default |
|-------------------------------|----------------------------------------------|------------------------|
| `DAIV_DIFF_TO_METADATA_MODEL_NAME` | Primary model for diff-to-metadata generation | `openrouter:google/gemini-3.8-flash` |
| `DAIV_DIFF_TO_METADATA_FALLBACK_MODEL_NAME` | Fallback model if the primary model fails | `openrouter:deepseek/deepseek-v4.1-flash` |

### Titling

Generates short titles for sessions and runs from the user's initial prompt.

| Variable | Description | Default |
|-------------------------------|----------------------------------------------|------------------------|
| `DAIV_TITLING_MODEL_NAME` | Primary model for title generation | `openrouter:openai/gpt-6-luna` |
| `DAIV_TITLING_FALLBACK_MODEL_NAME` | Fallback model if the primary model fails | `openrouter:anthropic/claude-haiku-5.5` |

### Memory

Learns from finished runs, consolidates what they teach into a per-repository document, and injects it into future runs.

| Variable | Description | Default |
|----------------------------------------|----------------------------------------------------------|------------------------|
| `DAIV_MEMORY_ENABLED` | Capture learnings from finished runs, consolidate them, and inject them into future runs. A per-repository `.daiv.yml` flag can still opt an individual repository out | `true` |
| `DAIV_MEMORY_EXTRACTION_MODEL_NAME` | Cheap model that extracts candidate observations from each finished run's transcript | `openrouter:openai/gpt-6-luna` |
| `DAIV_MEMORY_EXTRACTION_FALLBACK_MODEL_NAME` | Fallback model when the extraction model fails | `openrouter:anthropic/claude-haiku-5.5` |
| `DAIV_MEMORY_CONSOLIDATION_MODEL_NAME` | Model that rewrites the memory document. Leave empty to reuse the repository's agent model | *(none)* |
| `DAIV_MEMORY_CONSOLIDATION_MIN_PENDING` | Consolidate once at least this many pending observations have accumulated (subject to the consolidation interval below) | `10` |
| `DAIV_MEMORY_CONSOLIDATION_MAX_PENDING_AGE_DAYS` | Consolidate a repository that is still below the threshold above once its oldest pending observation reaches this age, so low-volume repositories still form memory | `7` |
| `DAIV_MEMORY_CONSOLIDATION_MIN_INTERVAL_HOURS` | Minimum hours between consolidations for the same repository | `24` |
| `DAIV_MEMORY_MAX_LINES` | Cap on the memory document length in lines; entries beyond it are evicted | `200` |
| `DAIV_MEMORY_MAX_BYTES` | Cap on the memory document size in bytes; entries beyond it are evicted | `10240` |

### Run classifier

Classifies the report of each finished scheduled run into an envelope.

| Variable | Description | Default |
|----------------------------------------|----------------------------------------------------------|------------------------|
| `DAIV_RUN_CLASSIFIER_MODEL_NAME` | Model that classifies each finished scheduled run's report into an envelope | `openrouter:openai/gpt-6-luna` |
| `DAIV_RUN_CLASSIFIER_FALLBACK_MODEL_NAME` | Fallback model when the classifier model fails | `openrouter:anthropic/claude-haiku-5.5` |

### Pipeline watch

Watches the CI of merge requests DAIV publishes and spends a bounded number of agent runs fixing failures; see [Pipeline Watch](../features/pipeline-watch.md).

| Variable | Description | Default |
|----------------------------------------|----------------------------------------------------------|------------------------|
| `DAIV_PIPELINE_WATCH_ENABLED` | Watch CI on merge requests DAIV publishes and try to fix failures | `true` |
| `DAIV_PIPELINE_WATCH_MAX_ATTEMPTS` | How many fix attempts to spend on one merge request before handing back (at least 1) | `3` |

---

## Deployment files

The sample deployment files set the variables below. The `DAIV_SANDBOX_*` variables configure the [daiv-sandbox](https://github.com/srtab/daiv-sandbox) container, not the DAIV app, and Docker reads `DAIV_IMAGE_TAG` when it starts the stack.

| Variable | Read by | Description | Default | Example |
|----------|---------|-------------|:-------:|---------|
| `DAIV_SANDBOX_REDIS_URL` | Sandbox service | Redis URL for per-session locking and for removing running sessions that stop receiving requests. Without it, the sandbox disables both and logs a warning | *(none)* | `redis://redis:6379/3` |
| `DAIV_SANDBOX_EGRESS_CA_CERT_FILE` | Sandbox service | Path to the egress CA certificate that the sandbox's egress proxy signs with. Set it together with the key file: the sandbox does not start with only one | *(none)* | `/run/secrets/daiv_sandbox_egress_ca_cert` |
| `DAIV_SANDBOX_EGRESS_CA_KEY_FILE` | Sandbox service | Path to the egress CA private key. Without both files, the sandbox rejects sessions that need the egress proxy and the run fails | *(none)* | `/run/secrets/daiv_sandbox_egress_ca_key` |
| `DAIV_IMAGE_TAG` | Docker | Tag of the DAIV image that the sample services pull; pin it to roll back to a build | `main` | `sha-5e4f0d0` |

To create the egress CA and store it as secrets, see [Deployment](../getting-started/deployment.md).
