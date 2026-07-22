# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Common Changelog](https://common-changelog.org/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Changed

- **Breaking:** The `Activity` and `ChatThread` models are replaced by unified `Session` and `Run` models (new `sessions` app). All job execution — Jobs API, MCP, chat, schedules, and webhooks — now runs on the unified session model with a claim/heartbeat execution lock. Existing rows are migrated automatically.
- **Breaking:** Per-user max mode (`use_max` flag) is replaced by an explicit agent override pair (`agent_model` + `agent_thinking_level`) on runs, schedules, chat threads, and `submit_job`. The agent picker in the run/chat composers now lets you choose any catalog model and thinking level per run; the fallback model may carry its own thinking effort. A per-run override now requires an explicit model — the repo-config `Auto` fallback was dropped.
- **Breaking:** LLM providers are now configurable via a database-backed `Provider` table (admin UI), alongside site settings, with support for Docker secrets and explicit `provider:model_name` syntax in model settings.
- Repository access is now authorized per user, mirrored from the git platform. Members only see and act on repositories they can access on GitLab/GitHub: viewing (pickers, search, branch lists, memory, MCP `list_repositories`) requires platform read access, and triggering agent runs, chat, or schedules requires platform write access. Access levels are synced every 15 minutes (configurable via `CODEBASE_REPO_ACCESS_SYNC_CRON`); a verified platform login (OAuth) is required, so invited users who only used login-by-code have no repository access until they connect their GitLab/GitHub account. Admins are exempt. Previously, any authenticated user could enumerate and run the agent against every repository the DAIV bot credential could reach.
- Built-in MCP servers (Sentry, Context7) are now database rows seeded to the official remote endpoints (`mcp.sentry.dev`, `mcp.context7.com`) and fully editable from the dashboard — URL, headers, and tool filter included. The `mcp_sentry`/`mcp_context7` supergateway containers, and their `MCP_SENTRY_URL`/`MCP_CONTEXT7_URL` settings, were removed from `docker-compose.yml`. **Upgrade note:** upgrading from v2.0.0 or earlier seeds the remote defaults (Sentry disabled until an `Authorization: Sentry-Bearer <token>` header is configured; Context7 enabled keyless) — if you were running the optional supergateway bridge containers, edit the `sentry`/`context7` row URL to point at your bridge and re-enable it. For on-premise Sentry, run your own bridge and point the row at it (see the MCP tools docs).
- **Upgrade note — existing network-enabled sandbox environments become network-isolated.** The legacy `network_enabled` flag is replaced by the per-environment egress policy (network is now derived from the presence of a policy). There is no automatic conversion: after upgrade those environments run network-isolated. The repository's own git platform stays reachable (DAIV injects it at runtime, so runs still clone and publish), but any other outbound access — package registries, external APIs — is blocked until an operator sets **Network** to **On** and defines an egress allow-list. This is deliberate: the new model has no unrestricted-network mode, and auto-granting allow-all would fail closed on deployments without the egress proxy CA configured.
- Chat runs now survive page refreshes and connection drops. The agent run executes server-side detached from the browser connection, publishing its events to a short-lived Redis stream; the chat page (re)joins that stream with full replay, so refreshing mid-run resumes live streaming instead of killing the run. **Stop** now explicitly cancels the run server-side (previously it worked by disconnecting). API note: `POST /api/chat/completions` returns a JSON run handle unless the caller sends `Accept: text/event-stream`, which preserves the inline AG-UI SSE behavior.
- `job_id` in `submit_job` / `get_job_status` responses now corresponds to `Run.id` (was previously `DBTaskResult.id`). Capture the new id from `submit_job` responses going forward — old ids will no longer resolve.
- `submit_job` and `get_job_status` responses now include `thread_id` and a `QUEUED` status value.
- The agent workspace is now sandbox-authoritative: the agent commits and opens the MR/PR itself from inside the sandbox (deepagents `SandboxFileBackend`), instead of DAIV pulling changed files back and publishing them host-side.
- The `/code-review` skill now runs as a detector fan-out pipeline (correctness, security, performance, structure, schema/contract and N+1 checks) with per-repo custom rules from `.agents/review-rules.md`, inline question archetype, and a delivery mode that posts inline plus summary review comments.
- GitLab git clone/push now uses a short-lived (~48–72h), project-scoped access token (`write_repository`, Developer role) minted per repository, instead of embedding the configured PAT in the clone URL — so the credential persisted in the workspace's `.git/config` (which travels into the sandbox) can no longer reach the full API or other projects. Minting requires the PAT user to have at least the Maintainer role on the project; when token creation is unavailable (e.g. GitLab.com Free tier), DAIV logs a warning and falls back to the previous PAT behavior. Note: the token pushes at Developer level — branch protection rules that restrict pushes on DAIV's branches to Maintainers must now allow Developers.
- Sandbox runtime is now configured exclusively through `SandboxEnvironment` rows; the `sandbox:` block in `.daiv.yml` is no longer honored. Recreate per-repo runtime configuration by creating an environment and listing the repo under its repository assignments. The env picker gains an `Auto` mode (the new default): the environment claiming the repository wins (USER-scoped beats GLOBAL-scoped); otherwise the GLOBAL default applies.
- Sandbox sessions are now reused across the turns of a chat conversation: the `thread_id → session_id` mapping is cached (Redis, TTL 12h) and the session is warmed and reused on the next turn, skipping the repo re-seed. Requires the matching daiv-sandbox release (stop-on-close + reaper).
- Reworked sandbox file sync: `write_file` and `edit_file` now push changes to the sandbox eagerly per tool call, and `bash_tool` no longer tarballs the entire working tree on every invocation. **Breaking:** requires the matching daiv-sandbox release with the new mutation/seed wire protocol.
- Sandbox file tools (`ls`/`read_file`/`grep`/`glob`/`write_file`/`edit_file`) now surface structured, machine-branchable errors from the sandbox instead of free-form strings: a missing path reads as "does not exist", reading a directory or creating over an existing file is rejected with a hint pointing at the right tool, and deletes are idempotent. **Breaking:** requires the matching daiv-sandbox release with the structured `fs/*` error responses (`FsError`/`FsErrorCode`).
- Renamed several environment variables to use the `DAIV_` prefix consistently: `AUTOMATION_WEB_SEARCH_*` → `DAIV_WEB_SEARCH_*`, `AUTOMATION_WEB_FETCH_*` → `DAIV_WEB_FETCH_*`, `AUTOMATION_SUGGEST_CONTEXT_FILE_ENABLED` → `DAIV_SUGGEST_CONTEXT_FILE_ENABLED`, `DIFF_TO_METADATA_*` → `DAIV_DIFF_TO_METADATA_*`, `JOBS_THROTTLE_RATE` → `DAIV_JOBS_THROTTLE_RATE`. Provider API key env vars (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `OPENROUTER_API_KEY`) are unchanged.
- Changed diff-to-metadata default model from Claude Haiku 4.5 to GPT-5.4-mini (with Claude Haiku 4.5 as fallback), and improved its prompts to enforce repository conventions from memory, incorporate ticket identifiers and external references (Sentry/Jira URLs), and reduce vague language in generated PR titles, descriptions, and commit messages.
- Changed `setup_webhooks` command to only create new webhooks by default, skipping existing ones.
- Increased Claude max output tokens from 4,096 to 16,384.
- Upgraded `deepagents` from 0.4.12 to 0.5.1, adding prompt caching, large message eviction, CRLF normalization, and multimodal file support.
- OpenRouter's Anthropic branch now uses `reasoning.effort` (with a five-level taxonomy including a new `xhigh` thinking level) so OpenRouter derives the reasoning budget server-side.
- Activity result downloads are now owner-scoped (previously any authenticated user with a run UUID could download any user's result).
- Deactivated users are now rejected on the API-key and MCP OAuth bearer paths (previously outstanding keys/tokens kept working).
- MCP `submit_job` now enforces the same per-user rate limit as the REST jobs and chat endpoints.
- MCP `get_job_status` and job pollers are now scoped to the authenticated user.

### Added

- Added per-user (member-scoped) MCP servers: members manage their own servers from the dashboard alongside the admin-managed global ones. Global rows win on name collisions (the shadowed personal server is flagged in the list), and member server headers are literal-only (no env-var references).
- Added a live model catalog to the agent picker: models are fetched dynamically from each configured provider (OpenAI, Anthropic, Google GenAI, OpenRouter) with caching, and the picker falls back to free-text entry when a provider has no catalog.
- Added Claude 5 model support (`claude-sonnet-5`, `claude-fable-5`, including OpenRouter slugs) and enabled extended thinking for `claude-opus-4.7`/`4.8` and `gpt-5.4`–`5.6`. Claude 5's removed `temperature` parameter is handled automatically.
- Added agent "dreaming" memory: the agent captures observations from runs, a scheduled `consolidate_memory` task consolidates them into per-repository memory, and relevant memories are injected into future runs. Includes a memory registries dashboard.
- Added skills management to the dashboard: upload/download/delete global custom skills as zip packages (validated and size-limited), browse built-in skills, and per-skill usage tracking with invocation counts and a 30-day chart. A custom skill with the same name shadows the built-in.
- Added new MCP tools: `list_jobs`, `list_repositories`, `list_environments`, `get_environment`, `schedule_job`, and `list_scheduled_jobs` (all with cursor-based pagination), plus a `wait` parameter on `submit_job`/`get_job_status` for long-polling.
- Added `thread_id` continuation to `submit_job` (MCP and HTTP API): a job submitted on a thread with an in-flight run is queued and dispatched FIFO after it terminates.
- Added scheduled run envelopes: each finished scheduled run derives a `RunEnvelope` with an `actionable[]` summary, and schedules can declare their intent on jobs and templates.
- Added one-off schedules and schedule templates; schedules now have run history, subscribers with notification fan-out and self-unsubscribe, and a "Run now" action.
- Added repoless agent runs (no repository attached) and multi-repository batch jobs, with one shared generated title per batch.
- Added per-run sandbox environments with USER/GLOBAL scopes, and a per-environment **network egress policy**: restrict outbound traffic to an allow-list of hosts (glob patterns), attach per-host HTTP credentials (encrypted at rest, never rendered back), scope each host to all methods or read-only, and set the default (deny/allow) reachability for unlisted hosts. The egress proxy is mandatory for network-enabled sessions — a network-enabled run is rejected if no egress proxy is configured rather than falling back to unrestricted network.
- Sandbox environments now automatically allow and authenticate their repository's git platform for git-over-HTTPS in the sandbox: DAIV injects a short-lived token at the egress proxy, so `git fetch`/clone/push works without a per-environment host rule — even in **Network Off** environments, which are opened solely for the git platform. The credential is refreshed on warm session reuse, and the rule takes precedence over a user-defined rule for the same host.
- Added in-app notifications (bell tray, mark-as-read on open) with email subjects prefixed and structured job-result emails, plus Rocket.Chat notifications rendered as event-typed attachments.
- Added token and cost usage tracking per run, propagated through a ContextVar-based usage handler and included in notification context.
- Added a database-backed configuration interface at `/dashboard/configuration/` (admin-only) for global settings — agent models, thinking levels, web search/fetch options, sandbox defaults, feature flags, rate limits, and API keys — without redeployment. API keys are encrypted at rest using Fernet; environment variables still act as hard overrides.
- Added a dashboard Skills section, a "Start a run" page with a ChatGPT-style prompt box, a "Retry" button on terminal runs, collapsible prompts, copy-to-clipboard buttons, markdown download of run results, and merge-request links on results.
- Added per-domain auth headers for the `web_fetch` tool, configurable from the dashboard (domain → header name/value, encrypted at rest) or via `DAIV_WEB_FETCH_AUTH_HEADERS`.
- Added configurable models for the titling task (chat thread and run titles) via `DAIV_TITLING_MODEL_NAME` / `DAIV_TITLING_FALLBACK_MODEL_NAME` or the configuration UI. Defaults: `gpt-5.4-mini` (primary), `claude-haiku-4.5` (fallback).
- Added model fallback support to all subagents (general-purpose, explore, and custom), with a new `DAIV_AGENT_EXPLORE_FALLBACK_MODEL_NAME` setting (default: `gpt-5-4-mini`).
- Added web-based authentication via django-allauth with GitHub and GitLab social login, passwordless login-by-code, a toggle for open social signup, and first-admin bootstrapping on fresh installs.
- Added role-based user management (admin/member) with role-based data scoping across all views, distinguishable user avatars, and a landing page for anonymous users.
- Added dashboard activity counters (jobs processed, success rate, issues resolved, MRs assisted, active API keys) with temporal filtering and today's deltas, plus code merge analytics (lines added/removed, files changed, DAIV vs human attribution) under a "Code Velocity" section.
- Added MCP (Model Context Protocol) server endpoint at `/mcp/` with OAuth 2.0 + PKCE authentication, metadata discovery, and dynamic client registration — MCP clients like Claude Code can connect to DAIV remotely.
- Added async Jobs API (`POST /api/jobs`, `GET /api/jobs/{id}`) with configurable per-user rate limiting (`DAIV_JOBS_THROTTLE_RATE`).
- Added `release_orphan_queued_threads` management command to recover `QUEUED` runs left behind on threads with no active sibling.
- Added `setup_langsmith_dashboard` management command (27 monitoring charts) and `model`/`thinking_level` metadata on all LangSmith traces.
- Added `EnsureNonEmptyResponseMiddleware` to recover from empty LLM responses by injecting a no-op tool call that prompts the model to retry.
- Added `--update` and `--repo-id` options to the `setup_webhooks` command.
- Added `allowed_usernames` option to `.daiv.yml` to restrict which users can interact with DAIV per repository.
- Added automatic suggestion to create an `AGENTS.md` file when DAIV opens a merge request for a repository that lacks one. Disable globally via `DAIV_SUGGEST_CONTEXT_FILE_ENABLED=False` or per repository with `context_file_name: null` in `.daiv.yml`.
- Added support for custom subagents per repository (markdown files in `.agents/subagents/` with YAML frontmatter) and custom global skills mounted at `/home/daiv/data/skills/` (configurable via `DAIV_AGENT_CUSTOM_SKILLS_PATH`).
- Added deferred tool loading: the agent starts with a minimal tool set and loads additional tools on demand based on task context.
- Added a **Test connection** button to the MCP server form that probes the URL/headers and swaps the tool-filter free-text field for checkboxes of the discovered tools.
- Added SMTP email relay configuration and branded HTML email templates (welcome, sign-in code) with Portuguese translations.
- Added SEO assets: favicon, meta tags, sitemap, robots.txt, and llms.txt.

### Fixed

- Fixed the configured GitLab PAT being embedded in clone URLs, leaking the full-access credential into the workspace's `.git/config` and the sandbox: git clone/push now uses a short-lived, project-scoped token (see the GitLab token entry under Changed).
- Fixed the agent failing to publish after a non-fast-forward push rejection, and failing when the source branch is protected (it now opens a new MR).
- Fixed `web_fetch` to limit same-host redirects (max 5) and re-validate SSRF protection on each redirect, preventing redirect loops and DNS rebinding.
- Fixed `PullRequestMetadata.branch` validation accepting invalid branch names (e.g. with spaces or uppercase) through an incomplete regex.
- Fixed `/clear` not dropping the agent's in-memory conversation history.
- Fixed intermittent database reconnects on some deployments by adding libpq TCP keepalives.
- Fixed `__version__` in `daiv/daiv/__init__.py` reporting `1.1.0` instead of the released version.
- Fixed `set -eu pipefail` in shell scripts — `pipefail` is not valid in POSIX `#!/bin/sh`.

### Removed

- **Breaking:** Removed the `mcp-proxy` container, its API configuration endpoint, and the `MCP_PROXY_HOST`/`MCP_PROXY_ADDR`/`MCP_PROXY_AUTH_TOKEN`/`MCP_CONFIG_API_KEY` settings. MCP servers are managed as database rows from the dashboard; existing `docker-compose.yml` and stack files must be updated.
- Removed the `sandbox:` block from `.daiv.yml` — sandbox runtime is configured exclusively through Sandbox Environments in the dashboard.
- Removed the deployment-level `DAIV_SANDBOX_BASE_IMAGE`, `DAIV_SANDBOX_CPU`, and `DAIV_SANDBOX_MEMORY` settings — manage runtime values through the Sandbox Environments UI (`DAIV_SANDBOX_TIMEOUT` and `DAIV_SANDBOX_API_KEY` remain).
- Removed GPT-4.1-mini, GPT-4.1, and GPT-5.2 from the model catalog; added GPT-5.4, GPT-5.4-mini, Z-AI GLM-5-turbo, and MiniMax M2-7.

## [2.0.0] - 2026-03-14

### Added

- Added `create_api_key` management command to create API keys from the command line without opening a Django shell.
- Added `/agents` slash command to list all available sub-agents with their names and descriptions.
- Added `/clear` slash command to reset conversation context in issues and merge requests.
- Added code review, security audit, plan, and skill creator builtin skills for structured guidance during agent execution.
- Added a deduplicating Django Tasks backend to prevent duplicate async task execution.
- Added support for issue labels to configure agent behavior:
  - `daiv-auto`: Automatically approve the plan and proceed with implementation without manual approval.
  - `daiv-max`: Use high-performance mode with a more capable model and higher thinking level.
- Added `MAX_MODEL_NAME` and `MAX_THINKING_LEVEL` configuration settings for high-performance mode.
- Added `django-crontask` integration and scheduler service for periodic tasks, including automatic webhook setup for GitLab.
- Added GitHub CLI (`gh`) and GitLab specialized tools for git platform operations.
- Added support for `.agents/skills` directory as an additional location for project-specific skills.
- Added `web_fetch` tool with SSRF protection (multicast and IPv6 checks), replacing the MCP fetch tool.
- Added configurable command policy for the sandbox `bash` tool, allowing repositories to define allowed and disallowed commands via `.daiv.yml`.
- Added Context7 MCP server integration for up-to-date library documentation lookups.
- Added support for inline comments on merge request reviews.
- Added the ability for the Review Addressor to push code changes directly to the repository as a draft merge request.
- Added per-repository model configuration support through the `models` section in `.daiv.yml`.
- Added Sentry AI integrations for Anthropic, OpenAI, Google GenAI, LangChain, and LangGraph tracing.
- Added configurable Sentry settings: `SENTRY_TRACES_SAMPLE_RATE`, `SENTRY_PROFILES_SAMPLE_RATE`, and `SENTRY_SEND_DEFAULT_PII`.
- Added support for `claude-opus-4.6`, `claude-sonnet-4.6`, `gpt-5.2`, `gpt-5.3-codex`, `glm-5`, `minimax-m2.5`, and `kimi-k2.5` models.

### Changed

- Integrated the `deepagents` library to power the core agent framework, replacing the custom `PlanAndExecuteAgent`, `CodebaseChatAgent`, and `PullRequestDescriberAgent` implementations with a unified middleware-based architecture.
- Migrated agent checkpoints from Postgres to Redis to prevent sub-agent hangs during concurrent execution.
- Migrated background processing from Celery to Django Tasks using the `django-tasks` database backend.
- Migrated pre-commit tooling to prek.
- Migrated type checking from mypy to ty.
- Updated issue addressing to accept DAIV trigger labels (`daiv`, `daiv-auto`, `daiv-max`) to launch the agent, with `daiv-auto` enabling auto-approval mode. **BREAKING CHANGE**: Issue title prefix (`DAIV:`) is no longer supported as a trigger. Use labels instead.
- Completely rewrote the pull request metadata generation as the `diff_to_metadata` module with improved structure, including `AGENTS.md` context integration.
- Updated default models to `claude-sonnet-4.6` and `gpt-5.3-codex`.
- Deferred sandbox session creation until the first `bash` tool invocation.
- Changed base sandbox image to include the `git` command.
- Migrated Anthropic prompt caching to automatic cache control for OpenRouter.
- Updated Redis configuration to use persistence (`appendonly yes`) with memory limits.
- Increased default worker replicas to 2.
- Updated merge request creation to return full metadata, including web URLs, for GitHub and GitLab clients.
- Renamed `quick_actions` module to `slash_commands` and merged behavior with skills system.
- Improved documentation for Review Addressor, Slash Commands, and Issue Addressor with clearer examples and configuration guides.

### Fixed

- Fixed health check endpoints (`/-/alive/`) generating irrelevant Sentry transactions by adding a `traces_sampler` function.
- Fixed handling of empty GitHub repositories when reading config files; the client now gracefully returns `None` instead of raising an exception.

### Removed

- Removed builtin `maintaining-changelog` skill.
- Removed `PlanAndExecuteAgent`, `CodebaseChatAgent`, and `PullRequestDescriberAgent` classes, replaced by the `deepagents`-based agent framework.
- Removed `pull_request.branch_name_convention` from `.daiv.yml`. **BREAKING CHANGE**: Branch name convention must now be defined in the `AGENTS.md` file instead.
- Removed Celery worker configuration and bootstrap scripts.
- Removed the `quick_actions` Django app in favor of the `slash_commands` module.
- Removed support for `gpt-5.1`, `gpt-5.1-codex`, `deepseek-v3.1-terminus`, `gemini-2.5-pro`, `grok-code-fast-1`, `glm-4.6`, `qwen3-max`, `qwen3-coder-plus`, `kimi-k2-thinking`, and `minimax-m2` models.

## [1.1.0] - 2025-12-04

### Added

- Added Agent Skills system implementing Anthropic's progressive disclosure pattern.
- Added `creating-agents-md-file` skill to help generate `AGENTS.md` files for repositories, following the [AGENTS.md](https://agents.md/) format.
- Added `maintaining-changelog` skill to help maintain changelog files for pull requests, following existing format conventions or creating new files using Common Changelog format.
- Added `creating-daiv-yml-config` skill to help create `.daiv.yml` configuration files with sandbox settings (base_image and format_code commands) based on repository content.
- Added chunked reading capability to `read_tool` with `start_line` and `max_lines` parameters. The tool now supports reading files in segments rather than loading entire file contents, limiting output to a default maximum of 2000 lines. When content is truncated, a message indicates the range shown and total lines available, guiding further reads. This addresses the issue of costly full-file reads for large files.
- Added `SWERepoClient` to support SWE-bench style evaluations with public OSS repositories. This client clones repositories to temporary directories without requiring credentials and is designed for automated testing scenarios.
- Added support to `gpt-5.1`, `gpt-5.1-codex`, `gpt-5.1-codex-mini` models from OpenAI.
- Added OpenRouter support to Anthropic caching middleware, reducing costs.
- Added `FileNavigationMiddleware`, `FileEditingMiddleware`, `MergeRequestMiddleware` and `WebSearchMiddleware` in replacement of toolkits, leveraging LangChain v1 middlewares capabilities to inject the system prompt and tools into the model call.
- Added `EXECUTION_THINKING_LEVEL` configuration to `PlanAndExecuteAgent` to allow users to enable thinking for execution tasks.
- Added `/clone-to-topic` quick action to clone issues to all repositories matching specified topics, enabling bulk distribution of issues across multiple repositories.

### Changed

- Changed default model for `PlanAndExecuteAgent` to `gpt-5.1` and `gpt-5.1-codex-mini` for planning fallback and code review respectively.
- Improved `PlanAndExecuteAgent` planning output to be more structured and easier to human understand.
- Improved `PlanAndExecuteAgent` planning prompts with "Code minimalism" guidelines to prevent over-engineering and unnecessary changes.
- Migrated all prompt templates from Jinja2 to Mustache format to prevent code injection attacks.
- Replaced `plan_think_tool` with `TodoListMiddleware` to allow the agent to maintain a todo list of the tasks to be completed during the planning phase.

### Fixed

- Fixed `format_code_tool` to properly apply the patch to the repository even when the command fails.
- Fixed inclusion of `.git` directory in the sandbox archive, preventing the agent from accessing the repository and reducing archive size.
- Fixed `InvalidNamespaceError` when branch names contain periods (e.g., `fix/python-version-3.11`) by sanitizing namespace labels for LangGraph store.
- Fixed `PlanAndExecuteAgent` to use `ToolStrategy` for execution tasks instead of `AutoStrategy` to handle union types.

## [1.0.0] - 2025-11-17

### Added

- Added support to `github` client type to allow users to use GitHub as the client for the codebase.
- Added to `PlanAndExecuteAgent` the capability to:
  - load images from the user message to help the agent to visualize them (using `InjectImagesMiddleware`).
  - plan and execute commands using the DAIV Sandbox tools. This will allow the agent to perform actions on the codebase, such as installing/updating dependencies ensuring lock files are updated, generating translations, etc.
  - load the repository instructions from a `AGENTS.md` file, which is a markdown file that follows the [AGENTS.md](https://agents.md/) format.
  - fix pipelines by retrieving the pipeline status and job logs when planning using the new `pipeline` and `job_logs` tools.
  - review code changes against the plan tasks using the new `review_code_changes` tool ensuring the changes are correct and complete.
- Added `InjectImagesMiddleware` LangChain v1 middleware to automatically extract and process images from markdown/HTML syntax in user messages, supporting GitHub user-attachments, GitLab uploads, and external URLs.
- Added `AGENTS.md` file to the project.
- Added `omit_content_patterns` to DAIV configuration to allow users to omit files content, but visible for the agents (the agent will only be able to see that the file exists, but not its content).
- Added evaluation tests for `CodebaseChatAgent`, `PullRequestDescriberAgent` and `PlanAndExecuteAgent`.
- Added support to `gpt-5`, `gpt-5-nano`, `gpt-5-mini` and `gpt-5-codex` models from OpenAI.
- Added support to `grok-code-fast-1` model from Grok.
- Added support to `claude-sonnet-4.5`, `claude-opus-4.1` and `claude-haiku-4.5` models from Anthropic.
- Added support to `deepseek-v3.1-terminus` model from DeepSeek.
- Added support to `glm-4.6` model from Z-AI.
- Added support to `qwen3-max` and `qwen3-coder-plus` models from Qwen.
- Added support to `kimi-k2-thinking` model from MoonshotAI.
- Added `RECURSION_LIMIT` configuration to `CodebaseChatAgent` to allow users to change the limit of recursive calls to the agent.
- Added support to delete entire directories with the `delete` tool.

### Changed

- Fixed async Celery tasks to properly handle Django connection pooling by implementing `ThreadSensitiveContext` wrapper and worker process signal handlers, preventing connection pool exhaustion.
- Migrated LangChain and LangGraph to v1.x with updated imports and API patterns.
- Improved planning prompt of `PlanAndExecuteAgent` to deal better with asking for clarification, ensuring the agent will ask questions contextualized to the current state of the codebase.
- Changed `CodebaseChatAgent` to only be able to answer questions about a repository at a time by passing the repository id as a header. This is direct consequence of removing codebase indexation, making it difficult to answer questions about multiple repositories at the same time. **BREAKING CHANGE**
- Changed `PullRequestDescriberAgent` to use diffs to describe the changes instead of commit messages, making it more accurate and concise.
- Replaced repository read tools `search_code_snippets`, `retrieve_file_content`, and `repository_structure` with the new `glob`, `grep`, `ls`, and `read` tools.
- Replaced repository write tools `create_new_repository_file`, `replace_snippet_in_file`, `rename_repository_file` and `delete_repository_file` with the new `write`, `edit`, `delete` and `rename` tools.
- Replaced sandbox tools `run_sandbox_commands` and `run_sandbox_code` with the new `bash` tool.
- Replaced LLM-based image extraction in `PlanAndExecuteAgent` with regex-based utility function for improved performance and reduced costs.
- Migrated in-memory store based file changes to actual filesystem based file changes and commits tracking using GitPython.
- Migrated default database from `pgvector/pgvector:pg17` to `postgres:17.6`.
- Migrated project from Python 3.13 to Python 3.14.
- Refactored repository configuration file schema to be more flexible and easier to use. **BREAKING CHANGE**
- Moved tools from `daiv/automation/tools` to `daiv/automation/agents/tools`.
- Moved quick actions from `daiv/automation/quick_actions` to `daiv/quick_actions`.
- Migrated quick action `help` to activate as `@daiv /help` instead of `@daiv help`. **BREAKING CHANGE**
- Migrated quick action `plan execute` to activate as `@daiv /approve-plan` instead of `@daiv plan execute`. **BREAKING CHANGE**
- Migrated quick action `plan revise` to activate as `@daiv /revise-plan` instead of `@daiv plan revise`. **BREAKING CHANGE**
- Updated project dependencies.
- Updated documentation.

### Fixed

- Current date time format is now excluded hours and minutes, making prompts cacheable.
- Blocked GitLab and GitHub callbacks if client type is not set to the corresponding client.
- Fixed `PlanAndExecuteAgent` to avoid reading the same files twice before executing the planned changes.
- Fixed `write` and `rename` tools to create parent directories automatically when they don't exist, preventing `FileNotFoundError`.
- Fixed sandbox session management to properly reuse sessions across multiple `bash` tool invocations by replacing ContextVar-based storage with LangGraph store-based persistence.
- Fixed DuckDuckGo search tool to use the new `ddgs` package instead of the deprecated `duckduckgo-search` package.

### Removed

- Removed codebase indexation feature in favor of the new navigation tools.
- Removed `CodeDescriberAgent`.
- Removed `CodebaseSearchAgent`.
- Removed `ImageURLExtractorAgent`.
- Removed `SnippetReplacerAgent`.
- Removed `RunSandboxCodeTool`.
- Removed `IssueAddressorAgent` (replaced by `PlanAndExecuteAgent`).
- Removed `PipelineFixerAgent` (replaced by `ReviewAddressorAgent` + `pipeline` and `job_logs` tools).
- Removed all notebooks from the project.
- Removed support to `claude-sonnet-4` and `claude-opus-4` models from Anthropic.
- Removed support to `deepseek-chat-v3.1` model from DeepSeek.
- Removed support to `o4-mini` model from OpenAI.

## [0.3.0] - 2025-07-25

### Added

- Added quick actions feature to allow users perform actions by commenting on the merge request or issue.
- Added quick actions to allow users to trigger plan revision by commenting `@daiv plan revise` on the issue.

### Changed

- Migrated `RunSandboxCommandsTool` and `RunSandboxCodeTool` to be async only.
- Migrated `PipelineFixerAgent` to be triggered by a quick action instead of a webhook, allowing users to request a repair plan to fix pipelines by commenting `@daiv pipeline repair` on the merge request.
- Migrated `IssueAddressorAgent` plan approval to be triggered by a quick action, allowing users to request a plan approval by commenting `@daiv plan execute` on the issue.

### Fixed

- `ReviewAddressorAgent` was not handling single line notes without line range, leading to empty diff content.
- `IssueAddressorAgent` was not handling correctly issues with the bot label on the title, leading to errors. Now it will remove the bot label from the title. #435
- Mentions to the bot on the review comments were not being handled correctly, leading the agent to ask for clarification about who is being mentioned in this context. #436

## [0.2.1] - 2025-06-17

### Added

- Added support to `o3` model from OpenAI.
- Added build and push docker image to `main` branch to allow testing edge versions of the project.

### Changed

- Changed default model for `CodebaseChatAgent` to `gpt-4.1`.
- Changed `PlanAndExecuteAgent` planning phase to use `medium` thinking level by default.
- Updated deployment documentation to include information about the MCP proxy.
- Improved `PipelineFixerAgent` to ensure the `troubleshoot_analysis_result` (renamed to `complete_task`) tool is called exactly once at the end of the workflow.
- Improved `PipelineFixerAgent` troubleshooting details to include more context about the issue.
- `ReviewAddressorAgent` now will only accept reviews for merge requests that have DAIV mentions on the discussion thread.
- Updated MCP `@sentry/mcp-server` to `0.12.0` version.
- Updated base python image to `3.13.5`.

### Fixed

- `MCPServer.get_connection` now attaches an `Authorization: Bearer` header when `MCP_PROXY_AUTH_TOKEN` secret is configured, ensuring authenticated requests to the MCP proxy. (#419)

### Removed

- Support to `claude-3-7-sonnet` model from Anthropic.

## [0.2.0] - 2025-06-09

### Added

- Added `author` to metadata on `ReviewAddressorAgent` and `IssueAddressorAgent` to track the agent executions on the `langsmith` platform.
- Added MCP tools support to allow the agent to use external tools through MCP servers: #274.

### Changed

- Improved `PlanAndExecuteAgent`:
  - Completely rewrote planning system prompt to be more structured and concise with clear workflow steps and rules of thumb.
  - Enhanced execution system prompt with better organization and clearer instructions for applying change plans.
  - Simplified plan template format for better readability and reduced verbosity.
  - Enhanced tools schema docstrings with more detailed field descriptions and usage guidelines.
  - These improvements affect all agents that use `PlanAndExecuteAgent`: `ReviewAddressorAgent`, `IssueAddressorAgent`, and `PipelineFixerAgent`.
- Improved `ReviewAddressorAgent`:
  - Completely rewrote reviewer response prompt with structured workflow steps, better context handling, and improved reasoning with the `think` tool.
  - Enhanced review planning prompt with clear workflow steps, better diff handling guidance, and structured reasoning process.
  - Improved prompt organization with visual separators and clearer section headers for better readability.
- Improved `PipelineFixerAgent`:
  - Completely rewrote troubleshooting system prompt to be more structured and concise with clear workflow steps and rules of thumb.
  - Enhanced troubleshooting human prompt with better context handling.
  - Simplified troubleshooting template format for better readability and reduced verbosity.
- Migrated project to be async by default.
- Updated project dependencies.

### Fixed

- When changing the state of an Issue (from `closed` to `opened`), the webhook was being ignored by the GitLab callback.
- Planning questions on Issue Addressor Agent were not being handled correctly.
- Recursion limit was not being correctly passed to `PlanAndExecuteAgent`, limiting the agent to only 25 calls.

## [0.1.5] - 2025-05-26

### Added

- Added `cleanup_indexes` command to clean up outdated indexes and inaccessible repositories.

### Fixed

- Fixed connection closed or lost on `ConnectionPool` by using `check_connection` to verify if the connection is still working.
- Fixed chunks length check to use the correct number of tokens instead of the number of characters.

## [0.1.4] - 2025-05-22

### Added

- Added support to `claude-sonnet-4` and `claude-opus-4` models from Anthropic.

### Changed

- Optimized `PullRequestDescriberAgent` prompt to improve the quality of the responses for a 0-shot agent.
- Optimized `CodebaseChatAgent` prompts to improve the quality of the responses, reduce hallucinations, gatekeeping first and improve the reasoning capabilities of the agent.
- Updated `PlanAndExecuteAgent` to use `claude-sonnet-4` as the default model for planning and execution.

### Fixed

- `reply_reviewer` node of `ReviewAddressorAgent` was not using the correct tool to reply to the reviewer comments. We completely refactored the agent to turn it more reliable and robust.
- `SearchCodeSnippetsTool` was being called with `repository` parameter even when `repo_id` was being provided, leading to errors. Now we support conditionally add the `repository` parameter to the signature of the tool.
- Sometimes `Document.id` was being defined as an uuid when retrieving the document from the database, leading to errors..

## [0.1.3] - 2025-05-20

### Added

- Added mapping for `yaml` language for `.yaml` and `.yml` extensions.

### Fixed

- Temperature is being sent on `o4-mini` model, which is not supported.
- Large chunks were being indexed, causing errors on the embedding process. Now it will skip chunks that are too large (more than 2x the chunk size). #378

## [0.1.2] - 2025-05-15

### Added

- Added support to `04-mini` model from OpenAI.
- Added support to define `LANGSMITH_API_KEY` as docker secrets.

### Changed

- Improved plan comment template readability by adding a separator between the each step of the plan.
- Normalized `WEB_SEARCH_API_KEY` to be `AUTOMATION_WEB_SEARCH_API_KEY` and followed the same pattern for other keys.

### Fixed

- Fixed `ImportError` when `LanguageParser` try to parse a files with `tree-sitter-languages`, which is not installed.
- Fixed system checks to verify if required environment variables or docker secrets are set up.
- Fixed `PushCallback` to consider only merge requests created by DAIV to avoid indexing every merge request on the project.

## [0.1.1] - 2025-05-14

### Fixed

- Fixed `start-app` script passing iligal option `-o`.

## [0.1.0] - 2025-05-13

### Added

- Added security check to the GitLab callback to validate the `X-Gitlab-Token` header: #93.
- Added posibility to configure `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `WEB_SEARCH_API_KEY` and `EMBEDDINGS_API_KEY` using docker secrets.

### Changed

- Improved how to set up a test project on local GitLab in the README.md file.
- Replaced `gpt-4o` and `gpt-4o-mini` with the new OpenAI models `gpt-4.1` and `gpt-4.1-mini`.
- Replaced `o3-mini` with the new reasoning OpenAI model `o4-mini`.
- Replaced `gemini-2.0-flash` and `gemini-2.0-flash-lite-001` with `gpt-4.1-mini` and `gpt-4.1-nano` respectively.
- Simplified `CodebaseChatAgent` and `PullRequestDescriberAgent` prompts to make the agent job—and the prompt reader's job—simpler and less error‑prone.
- Migrated all evaluators/assessments logics to standalone agents to allow testing and customizing them independently from the main agents.
- Parallelized `update_index` process to improve performance.
- Improved codebase chunking process by replacing `RecursiveCharacterTextSplitter` and integrating more specialized splitters for Markdown and all languages supported by tree-sitter-language-pack using Chonkie package. `RecursiveCharacterTextSplitter` is now used as a fallback splitter.
- Added Roadmap section to the README.md.
- Updated project urls declared in `pyproject.toml` to use standard labels.
- Updated sensible `pydantic` settings to use `SecretStr` to avoid exposing sensitive information.

### Fixed

- Turned Sandbox tools more resilient and prevent failing the whole agent execution when the sandbox is unavailable.
- Empty repositories case was not being considered on the repository structure tool, causing an not found error.
- Repository index was not updating the `sha` field on the `CodebaseIndex` model, causing the index to be considered as outdated even when it's not.

### Removed

- Removed dependency on `gunicorn` and used `uvicorn` as the default server.


[Unreleased]: https://github.com/srtab/daiv/compare/v2.0.0...HEAD
[2.0.0]: https://github.com/srtab/daiv/compare/v1.1.0...v2.0.0
[1.1.0]: https://github.com/srtab/daiv/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/srtab/daiv/compare/v0.3.0...v1.0.0
[0.3.0]: https://github.com/srtab/daiv/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/srtab/daiv/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/srtab/daiv/compare/v0.1.5...v0.2.0
[0.1.5]: https://github.com/srtab/daiv/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/srtab/daiv/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/srtab/daiv/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/srtab/daiv/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/srtab/daiv/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/srtab/daiv/releases/tag/v0.1.0
