# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Common Changelog](https://common-changelog.org/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

_If you are upgrading from 2.0.0: please see [`UPGRADING.md`](UPGRADING.md)._

### Changed

- **Breaking:** Replace the OpenAI-compatible chat API with the AG-UI protocol of the dashboard chat: `POST /api/chat/completions` takes an AG-UI `RunAgentInput` and returns a run handle, or AG-UI events with `Accept: text/event-stream` ([#859](https://github.com/srtab/daiv/pull/859), [#1382](https://github.com/srtab/daiv/pull/1382))
- **Breaking:** Reject GitLab and GitHub webhooks with `401` while `CODEBASE_GITLAB_WEBHOOK_SECRET` or `CODEBASE_GITHUB_WEBHOOK_SECRET` is unset, and fail `django-admin check` until the active platform's secret is set ([#1675](https://github.com/srtab/daiv/pull/1675))
- **Breaking:** Manage LLM providers, their API keys and base URLs under **Configuration → Providers**; the environment variables and Docker secrets seed them once, on upgrade ([#1195](https://github.com/srtab/daiv/pull/1195), [#1829](https://github.com/srtab/daiv/pull/1829))
- **Breaking:** Run the agent's workspace inside the sandbox, where the agent commits and opens merge requests itself; this needs daiv-sandbox 0.5.0 or later, with an egress CA ([#1279](https://github.com/srtab/daiv/pull/1279))
- **Breaking:** Require the scheduler, which now also syncs repository access and runs schedules, sweeps and memory consolidation ([#1365](https://github.com/srtab/daiv/pull/1365), [#1030](https://github.com/srtab/daiv/pull/1030))
- **Breaking:** Close sandbox networking except to the repository's git platform, unless the run's sandbox environment allows more hosts through the egress proxy ([#1331](https://github.com/srtab/daiv/pull/1331), [#1340](https://github.com/srtab/daiv/pull/1340))
- **Breaking:** Rename `AUTOMATION_WEB_SEARCH_*`, `AUTOMATION_WEB_FETCH_*` and `DIFF_TO_METADATA_*` to `DAIV_WEB_SEARCH_*`, `DAIV_WEB_FETCH_*` and `DAIV_DIFF_TO_METADATA_*` ([#1022](https://github.com/srtab/daiv/pull/1022))
- **Breaking:** Match `git` rules of the sandbox command policy on the git subcommand, and block commands that switch branches or commit behind DAIV's back, such as `git checkout <branch>`, `git pull` and `git cherry-pick` ([#1615](https://github.com/srtab/daiv/pull/1615))
- Change the default models to Claude Sonnet 5.5 with GPT 6.1 Sol as fallback for the agent, Claude Opus 5.5 for `daiv-max`, and Gemini 3.8 Flash with DeepSeek V4.1 Flash for commit messages and merge request text ([#1637](https://github.com/srtab/daiv/pull/1637))
- Post one summary report per `/code-review` instead of inline comments, from detector subagents chosen by what the change touches and rules in `.agents/review-rules.md` ([#1432](https://github.com/srtab/daiv/pull/1432), [#1711](https://github.com/srtab/daiv/pull/1711))
- Stop the sandbox container when a run ends and reuse it on the session's next turn, instead of removing it ([#1279](https://github.com/srtab/daiv/pull/1279), [#1588](https://github.com/srtab/daiv/pull/1588))
- Change the default sandbox image from `python:3.12-bookworm` to `ghcr.io/astral-sh/uv:python3.14-bookworm`, about 1.1 GB ([#1791](https://github.com/srtab/daiv/pull/1791))
- Clone and push GitLab repositories with short-lived project access tokens instead of the configured personal access token, and keep git credentials out of the workspace so they never reach the sandbox ([#1300](https://github.com/srtab/daiv/pull/1300), [#1366](https://github.com/srtab/daiv/pull/1366))
- Open DAIV's merge requests against the branch the run started from, and assign them to the issue's assignee or author, or to the user who started the run ([#1547](https://github.com/srtab/daiv/pull/1547), [#1467](https://github.com/srtab/daiv/pull/1467))
- Write merge request descriptions as short prose, with **Key Changes** and **Notes** sections only when they are needed ([#1538](https://github.com/srtab/daiv/pull/1538))
- Start issue runs with the issue's title, labels and description in the conversation, so the agent no longer fetches them first ([#1618](https://github.com/srtab/daiv/pull/1618))
- Stop the agent's bash commands after 5 minutes by default, or after up to `DAIV_SANDBOX_TIMEOUT` when the agent asks for longer, instead of failing the call when the sandbox request times out ([#1759](https://github.com/srtab/daiv/pull/1759))
- Think adaptively on Claude Opus 4.6 and Sonnet 4.6 on the Anthropic provider, with the thinking level sent as `effort` ([#1693](https://github.com/srtab/daiv/pull/1693))
- Route OpenRouter requests to the cheapest provider first (`provider.sort: price`) ([#1652](https://github.com/srtab/daiv/pull/1652))
- Keep Anthropic prompt-cache entries for 5 minutes instead of 1 hour, which lowers the cost of cache writes ([#1171](https://github.com/srtab/daiv/pull/1171))
- Raise Claude's maximum output tokens from 4,096 to 16,384 ([#951](https://github.com/srtab/daiv/pull/951))
- Expire agent checkpoints in Redis after 7 days instead of 90, set with `DJANGO_REDIS_CHECKPOINT_TTL_MINUTES` ([#981](https://github.com/srtab/daiv/pull/981))
- Run short user-visible tasks, such as titles and notifications, on an `interactive` queue apart from the agent runs on `default` ([#1516](https://github.com/srtab/daiv/pull/1516))
- Run background tasks on Django's built-in `django.tasks`, so worker logs move from the `django_tasks` logger to `django.tasks` ([#1685](https://github.com/srtab/daiv/pull/1685))
- Report commands blocked by `DAIV_SANDBOX_COMMAND_POLICY_DISALLOW` as `global_disallow` instead of `repo_disallow` ([#1581](https://github.com/srtab/daiv/pull/1581))
- Accept thinking levels in any case, and `off` instead of `None` to turn thinking off, in the thinking-level variables ([#1650](https://github.com/srtab/daiv/pull/1650))
- Make `setup_webhooks` create missing webhooks only; pass `--update` to update existing ones ([#943](https://github.com/srtab/daiv/pull/943))
- Deprecate `DAIV_AGENT_EXPLORE_MODEL_NAME` in favour of an `explore` entry in `DAIV_AGENT_SUBAGENT_MODELS`; `django-admin check` warns with `core.W001` while it is set ([#1727](https://github.com/srtab/daiv/pull/1727))
- Move the documentation to [docs.daivagent.com](https://docs.daivagent.com/) ([#1826](https://github.com/srtab/daiv/pull/1826))

### Added

- Add a web dashboard, signed in with a GitLab or GitHub account, an emailed code or a passkey, with admin and member roles ([#993](https://github.com/srtab/daiv/pull/993), [#1559](https://github.com/srtab/daiv/pull/1559))
- Add sessions: every run, whether started from chat, a webhook, the Jobs API, MCP or a schedule, is a session you can watch live, continue, retry, stop and archive in the dashboard ([#1374](https://github.com/srtab/daiv/pull/1374), [#1691](https://github.com/srtab/daiv/pull/1691))
- Give a webhook's session to the DAIV user who signed in with the author's GitLab or GitHub account, who then gets its notifications ([#1744](https://github.com/srtab/daiv/pull/1744))
- Add a dashboard chat that works on one or more repositories, with a model and thinking-level picker, a context and spend meter, and slash-command completion ([#859](https://github.com/srtab/daiv/pull/859), [#1764](https://github.com/srtab/daiv/pull/1764))
- Add a **Home** page listing what needs you, and a **Team insights** page with run and code-velocity trends ([#1708](https://github.com/srtab/daiv/pull/1708), [#1710](https://github.com/srtab/daiv/pull/1710))
- Add the Jobs API (`POST /api/jobs`, `GET /api/jobs/{id}`) to run the agent from code on up to 20 repositories per batch, rate-limited per user by `DAIV_JOBS_THROTTLE_RATE` ([#987](https://github.com/srtab/daiv/pull/987), [#1123](https://github.com/srtab/daiv/pull/1123))
- Add an MCP server at `/mcp/`, authenticated with OAuth 2.0 or a DAIV API key, whose tools submit, schedule and follow jobs and list repositories and sandbox environments ([#1014](https://github.com/srtab/daiv/pull/1014), [#1352](https://github.com/srtab/daiv/pull/1352))
- Add scheduled jobs: recurring and one-off schedules, templates, subscribers and **Run now** ([#1030](https://github.com/srtab/daiv/pull/1030), [#1213](https://github.com/srtab/daiv/pull/1213))
- Add notifications by email, in the dashboard, Rocket.Chat and Telegram, sent when a run found issues, needs attention or failed, as judged by a run classifier ([#1520](https://github.com/srtab/daiv/pull/1520), [#1523](https://github.com/srtab/daiv/pull/1523))
- Add **Configuration** pages where admins set models, providers, web search and fetch, sandbox defaults, feature flags and rate limits without redeploying ([#1022](https://github.com/srtab/daiv/pull/1022), [#1195](https://github.com/srtab/daiv/pull/1195))
- Add sandbox environments, global or personal and assigned to repositories, each with its own image, resources, environment variables and egress allow-list with per-host credentials ([#1194](https://github.com/srtab/daiv/pull/1194), [#1331](https://github.com/srtab/daiv/pull/1331))
- Add per-user repository access mirrored from GitLab or GitHub: members see the repositories they can read and run the agent where they can write ([#1365](https://github.com/srtab/daiv/pull/1365))
- Add MCP server management in the dashboard, global for admins and personal for members, with **Test connection**, tool filters and per-session switches ([#1256](https://github.com/srtab/daiv/pull/1256), [#1472](https://github.com/srtab/daiv/pull/1472))
- Add the `bootstrap_admin` command to create the first admin of a fresh install ([#1093](https://github.com/srtab/daiv/pull/1093))
- Add custom global skills, uploaded in the dashboard as zip packages or mounted at `DAIV_AGENT_CUSTOM_SKILLS_PATH`, with per-skill usage; a custom skill shadows a built-in of the same name ([#1250](https://github.com/srtab/daiv/pull/1250), [#1253](https://github.com/srtab/daiv/pull/1253))
- Add custom subagents per repository, from Markdown files in `.agents/subagents/` ([#972](https://github.com/srtab/daiv/pull/972))
- Add skill and subagent models by name, set in the dashboard, `DAIV_AGENT_SKILL_MODELS` / `DAIV_AGENT_SUBAGENT_MODELS`, or `models.skills` / `models.subagents` in `.daiv.yml` ([#1727](https://github.com/srtab/daiv/pull/1727))
- Add repository memory: the agent records what it learns in runs, consolidates it per repository and recalls it later; it is on by default, and turned off with `DAIV_MEMORY_ENABLED` or `memory.enabled` in `.daiv.yml` ([#1308](https://github.com/srtab/daiv/pull/1308), [#1461](https://github.com/srtab/daiv/pull/1461))
- Add run artifacts: the agent publishes and revises files it generates, such as reports and charts, shown in its session and on an **Artifacts** page ([#1573](https://github.com/srtab/daiv/pull/1573), [#1628](https://github.com/srtab/daiv/pull/1628))
- Let the agent stop and ask you a question, in chat, as an issue or merge request comment, or as a job in `WAITING_INPUT` ([#1582](https://github.com/srtab/daiv/pull/1582))
- Add pipeline watch: after DAIV publishes a merge request it watches CI and tries to fix a failing pipeline, up to `DAIV_PIPELINE_WATCH_MAX_ATTEMPTS` runs; it is on by default, and turned off with `DAIV_PIPELINE_WATCH_ENABLED` or `pipeline_watch.enabled` in `.daiv.yml` ([#1529](https://github.com/srtab/daiv/pull/1529))
- Add orchestration: the agent delegates tailored jobs to other repositories or new sessions with `delegate_jobs`, and resumes with their results; turn it off with `orchestration.enabled` in `.daiv.yml` ([#1677](https://github.com/srtab/daiv/pull/1677), [#1781](https://github.com/srtab/daiv/pull/1781))
- Add multi-repository sessions that work across up to 5 repositories and open one linked merge request per changed repository ([#1700](https://github.com/srtab/daiv/pull/1700))
- Add runs without a repository ([#1192](https://github.com/srtab/daiv/pull/1192))
- Add a session inbox: mentions on a merge request a session owns, its CI failures and messages sent while it is busy go to that session's next run ([#1744](https://github.com/srtab/daiv/pull/1744))
- Archive every session that worked on a merge request when it merges ([#1825](https://github.com/srtab/daiv/pull/1825))
- Resume a session whose checkpoint expired from its archive in Postgres, and export archives with `export_session_archives` ([#1649](https://github.com/srtab/daiv/pull/1649))
- Let the agent merge a branch into the one it works on and resolve the conflicts ([#1615](https://github.com/srtab/daiv/pull/1615))
- Let the agent create GitHub releases with `gh release create` ([#1545](https://github.com/srtab/daiv/pull/1545))
- Choose the agent model and thinking level per run, in chat, schedules and jobs ([#1254](https://github.com/srtab/daiv/pull/1254))
- Support Claude 5 and GPT-6 models, and an **Extra high** (`xhigh`) thinking level ([#1405](https://github.com/srtab/daiv/pull/1405), [#1254](https://github.com/srtab/daiv/pull/1254))
- Add a per-request timeout and retry count for LLM calls, `DAIV_MODEL_REQUEST_TIMEOUT_SECONDS` (120) and `DAIV_MODEL_MAX_RETRIES` (2) ([#1740](https://github.com/srtab/daiv/pull/1740))
- Add deferred tool loading: the agent starts with a small tool set and loads MCP and other tools when a task needs them ([#1140](https://github.com/srtab/daiv/pull/1140), [#1465](https://github.com/srtab/daiv/pull/1465))
- Show the images and files a tool returns in the session transcript, and let the agent open base64 files an MCP server returns ([#1782](https://github.com/srtab/daiv/pull/1782), [#1736](https://github.com/srtab/daiv/pull/1736))
- Add SearXNG as a web-search backend ([#1779](https://github.com/srtab/daiv/pull/1779)) (Tobi)
- Read one section of a page with `web_fetch` by passing its URL `#fragment` ([#1752](https://github.com/srtab/daiv/pull/1752))
- Track token usage and cost per run and session ([#1077](https://github.com/srtab/daiv/pull/1077), [#1776](https://github.com/srtab/daiv/pull/1776))
- Link the merge requests DAIV opens to their session, with a `DAIV-Session` commit trailer; turn it off with `DAIV_SESSION_LINK_ENABLED` or `session_link` in `.daiv.yml` ([#1497](https://github.com/srtab/daiv/pull/1497))
- Accept up to 20 external `references` on Jobs API and MCP jobs, linked from the merge request with the platform's closing syntax ([#1527](https://github.com/srtab/daiv/pull/1527))
- Add `allowed_usernames` to `.daiv.yml` to restrict who can trigger DAIV in a repository ([#948](https://github.com/srtab/daiv/pull/948))
- Suggest an `AGENTS.md` when DAIV opens a merge request in a repository without one; turn it off with `DAIV_SUGGEST_CONTEXT_FILE_ENABLED` or `suggest_context_file` in `.daiv.yml` ([#970](https://github.com/srtab/daiv/pull/970))
- Add `DAIV_TITLING_MODEL_NAME` and `DAIV_TITLING_FALLBACK_MODEL_NAME` for the model that titles sessions ([#1150](https://github.com/srtab/daiv/pull/1150))
- Add the `--update` and `--repo-id` options to `setup_webhooks` ([#943](https://github.com/srtab/daiv/pull/943))
- Add the `setup_langsmith_dashboard` command and model metadata on LangSmith traces ([#988](https://github.com/srtab/daiv/pull/988))
- Add SMTP settings for sending email (`EMAIL_HOST` and related variables) ([#999](https://github.com/srtab/daiv/pull/999))
- Stamp images with their commit and build date, shown in the sidebar and at `/-/version/`, and publish an immutable `sha-` image tag per commit ([#1482](https://github.com/srtab/daiv/pull/1482))
- Add a public homepage and comparison pages for signed-out visitors, with optional cookieless Umami analytics (`DAIV_UMAMI_WEBSITE_ID`) ([#994](https://github.com/srtab/daiv/pull/994), [#1826](https://github.com/srtab/daiv/pull/1826))
- Add `DJANGO_SESSION_COOKIE_NAME` so several instances on one host keep separate sign-ins ([#1733](https://github.com/srtab/daiv/pull/1733))
- Add a Quickstart guide that runs DAIV on one machine with Docker Compose ([#1791](https://github.com/srtab/daiv/pull/1791))
- Add Slack as a notification channel: when an administrator enables it, you can bind your Slack member ID and receive DAIV notifications as direct messages from the DAIV bot; verification looks the member ID up via `users.info` (sent as a query parameter, which is the form Slack reads) and rejects deactivated, bot and Slack Connect accounts, and delivery honours Slack's `Retry-After` on rate limits instead of retrying on the fixed ladder ([#1824](https://github.com/srtab/daiv/pull/1824))

### Removed

- **Breaking:** Remove `GET /api/models` and `GET /api/models/{id}`, the OpenAI-compatible model list ([#859](https://github.com/srtab/daiv/pull/859))
- **Breaking:** Remove the `mcp-proxy` service, `GET /api/automation/mcp-proxy/config/` and the `MCP_PROXY_*`, `MCP_SENTRY_*` and `MCP_CONTEXT7_*` settings; Sentry and Context7 are now MCP servers you manage in the dashboard ([#957](https://github.com/srtab/daiv/pull/957), [#1256](https://github.com/srtab/daiv/pull/1256))
- **Breaking:** Remove `DAIV_SANDBOX_BASE_IMAGE`, `DAIV_SANDBOX_CPU`, `DAIV_SANDBOX_MEMORY`, `DAIV_SANDBOX_EPHEMERAL`, `DAIV_SANDBOX_NETWORK_ENABLED` and the `.daiv.yml` `sandbox:` block, which sandbox environments replace ([#1194](https://github.com/srtab/daiv/pull/1194), [#1331](https://github.com/srtab/daiv/pull/1331))
- **Breaking:** Remove `OPENROUTER_API_BASE`, which the OpenRouter provider's base URL replaces ([#1195](https://github.com/srtab/daiv/pull/1195))
- **Breaking:** Remove the `/clone-to-topics` slash command ([#1754](https://github.com/srtab/daiv/pull/1754))
- **Breaking:** Stop accepting `CODEBASE_CLIENT=swe`; only `gitlab` and `github` are valid ([#1683](https://github.com/srtab/daiv/pull/1683))

### Fixed

- Refuse agent file-tool writes under `.git`, and run DAIV's host-side git without hooks, fsmonitor or external diff, closing a prompt-injection path to the worker ([#1676](https://github.com/srtab/daiv/pull/1676))
- Fix `web_fetch` following unlimited same-host redirects without checking SSRF protection again; it now follows at most 5 and checks each one ([#971](https://github.com/srtab/daiv/pull/971))
- Fix the sandbox command policy skipping commands inside `if`, `case`, `!`, `time` or `coproc`, or after a variable assignment, so a blocked command such as `git push` could run ([#1581](https://github.com/srtab/daiv/pull/1581))
- Reject API keys that belong to deactivated users ([#1365](https://github.com/srtab/daiv/pull/1365))
- Fix webhook duplicate suppression, so a redelivered webhook or an edited comment that mentions DAIV no longer starts another run ([#1670](https://github.com/srtab/daiv/pull/1670))
- Fix a failed web search ending the whole run; the agent now continues without results and the failure is logged at ERROR ([#1542](https://github.com/srtab/daiv/pull/1542))
- Fix an MCP tool call that fails before the server answers ending the whole run; the agent now gets a failed tool result ([#1722](https://github.com/srtab/daiv/pull/1722))
- Fix publishing failing after a non-fast-forward push rejection, and open a new merge request when the source branch is protected ([#1349](https://github.com/srtab/daiv/pull/1349), [#1183](https://github.com/srtab/daiv/pull/1183))
- Fix publishing failing after ten branch-name collisions, by adding a random suffix to a taken name ([#1438](https://github.com/srtab/daiv/pull/1438))
- Fix DAIV on GitHub failing to push changes under `.github/workflows/` when the GitHub App has the **Workflows** permission ([#1721](https://github.com/srtab/daiv/pull/1721))
- Mark a draft GitHub pull request ready for review after DAIV pushes changes to it, as on GitLab ([#1655](https://github.com/srtab/daiv/pull/1655))
- Post the "unable to address" comment when an issue or merge request run fails before the agent starts, such as on a clone failure ([#1572](https://github.com/srtab/daiv/pull/1572))
- Accept webhook payloads up to 10 MB, so large GitLab events are no longer rejected ([#1047](https://github.com/srtab/daiv/pull/1047))
- Fix `/clear` not dropping the conversation history ([#1267](https://github.com/srtab/daiv/pull/1267))
- Fix a mention on a merge request labelled `daiv-max` running on the default model instead of the max model ([#1650](https://github.com/srtab/daiv/pull/1650))
- Fix `.daiv.yml` `models.diff_to_metadata` being ignored ([#1611](https://github.com/srtab/daiv/pull/1611))
- Fix `context_file_name: null` in `.daiv.yml` loading a file named `None` from the repository root as instructions ([#1613](https://github.com/srtab/daiv/pull/1613))
- Fix the agent writing user feedback into the repository's `AGENTS.md`, which could put an `AGENTS.md` edit in the merge request ([#1613](https://github.com/srtab/daiv/pull/1613))
- Fix the agent cutting long tool-call arguments in its earlier messages, which reset the prompt cache, on models whose context window is known ([#1613](https://github.com/srtab/daiv/pull/1613))
- Fix Claude models on OpenRouter running without extended thinking ([#1652](https://github.com/srtab/daiv/pull/1652))
- Fix OpenRouter models losing their reasoning between turns, such as GLM's interleaved thinking and Anthropic's thinking blocks ([#1451](https://github.com/srtab/daiv/pull/1451))
- Fix the **Minimal** thinking level failing on OpenAI GPT-5 models, which now receive `low` ([#1595](https://github.com/srtab/daiv/pull/1595))
- Fix `web_fetch` returning its summarising model's raw output, reasoning included, when that model replies in content blocks, as OpenAI models on the Responses API do ([#1752](https://github.com/srtab/daiv/pull/1752))
- Fix the `gh` and `gitlab` tools dropping a failed command's standard output ([#1654](https://github.com/srtab/daiv/pull/1654))
- Fix sandbox containers left running after a run fails or is stopped ([#1588](https://github.com/srtab/daiv/pull/1588))
- Fix a worker that died while fetching a GitHub token blocking every later token fetch ([#1435](https://github.com/srtab/daiv/pull/1435))
- Fix intermittent database reconnects by enabling libpq TCP keepalives, tuned with `DB_KEEPALIVES_*` ([#1157](https://github.com/srtab/daiv/pull/1157))

## [2.0.0] - 2026-03-14

### Changed

- **Breaking:** Trigger issue addressing with the DAIV labels (`daiv`, `daiv-auto`, `daiv-max`), with `daiv-auto` enabling auto-approval mode. The issue title prefix (`DAIV:`) no longer triggers the agent; use labels instead.
- Integrate the `deepagents` library to power the core agent framework, replacing the custom `PlanAndExecuteAgent`, `CodebaseChatAgent`, and `PullRequestDescriberAgent` implementations with a unified middleware-based architecture.
- Migrate agent checkpoints from Postgres to Redis to prevent sub-agent hangs during concurrent execution.
- Migrate background processing from Celery to Django Tasks using the `django-tasks` database backend.
- Migrate pre-commit tooling to prek.
- Migrate type checking from mypy to ty.
- Rewrite the pull request metadata generation as the `diff_to_metadata` module with improved structure, including `AGENTS.md` context integration.
- Update default models to `claude-sonnet-4.6` and `gpt-5.3-codex`.
- Defer sandbox session creation until the first `bash` tool invocation.
- Change the base sandbox image to include the `git` command.
- Migrate Anthropic prompt caching to automatic cache control for OpenRouter.
- Update Redis configuration to use persistence (`appendonly yes`) with memory limits.
- Increase default worker replicas to 2.
- Return full metadata, including web URLs, when creating merge requests with the GitHub and GitLab clients.
- Rename the `quick_actions` module to `slash_commands` and merge its behavior with the skills system.
- Improve documentation for Review Addressor, Slash Commands, and Issue Addressor with clearer examples and configuration guides.

### Added

- Add `create_api_key` management command to create API keys from the command line without opening a Django shell.
- Add `/agents` slash command to list all available sub-agents with their names and descriptions.
- Add `/clear` slash command to reset conversation context in issues and merge requests.
- Add code review, security audit, plan, and skill creator builtin skills for structured guidance during agent execution.
- Add a deduplicating Django Tasks backend to prevent duplicate async task execution.
- Add support for issue labels to configure agent behavior:
  - `daiv-auto`: Automatically approve the plan and proceed with implementation without manual approval.
  - `daiv-max`: Use high-performance mode with a more capable model and higher thinking level.
- Add `MAX_MODEL_NAME` and `MAX_THINKING_LEVEL` configuration settings for high-performance mode.
- Add `django-crontask` integration and scheduler service for periodic tasks, including automatic webhook setup for GitLab.
- Add GitHub CLI (`gh`) and GitLab specialized tools for git platform operations.
- Add support for the `.agents/skills` directory as an additional location for project-specific skills.
- Add `web_fetch` tool with SSRF protection (multicast and IPv6 checks), replacing the MCP fetch tool.
- Add configurable command policy for the sandbox `bash` tool, allowing repositories to define allowed and disallowed commands via `.daiv.yml`.
- Add Context7 MCP server integration for up-to-date library documentation lookups.
- Add support for inline comments on merge request reviews.
- Let the Review Addressor push code changes directly to the repository as a draft merge request.
- Add per-repository model configuration support through the `models` section in `.daiv.yml`.
- Add Sentry AI integrations for Anthropic, OpenAI, Google GenAI, LangChain, and LangGraph tracing.
- Add configurable Sentry settings: `SENTRY_TRACES_SAMPLE_RATE`, `SENTRY_PROFILES_SAMPLE_RATE`, and `SENTRY_SEND_DEFAULT_PII`.
- Add support for `claude-opus-4.6`, `claude-sonnet-4.6`, `gpt-5.2`, `gpt-5.3-codex`, `glm-5`, `minimax-m2.5`, and `kimi-k2.5` models.

### Removed

- **Breaking:** Remove `pull_request.branch_name_convention` from `.daiv.yml`; define the branch name convention in the `AGENTS.md` file instead.
- Remove builtin `maintaining-changelog` skill.
- Remove `PlanAndExecuteAgent`, `CodebaseChatAgent`, and `PullRequestDescriberAgent` classes, replaced by the `deepagents`-based agent framework.
- Remove Celery worker configuration and bootstrap scripts.
- Remove the `quick_actions` Django app in favor of the `slash_commands` module.
- Remove support for `gpt-5.1`, `gpt-5.1-codex`, `deepseek-v3.1-terminus`, `gemini-2.5-pro`, `grok-code-fast-1`, `glm-4.6`, `qwen3-max`, `qwen3-coder-plus`, `kimi-k2-thinking`, and `minimax-m2` models.

### Fixed

- Fix health check endpoints (`/-/alive/`) generating irrelevant Sentry transactions by adding a `traces_sampler` function.
- Fix handling of empty GitHub repositories when reading config files; the client now gracefully returns `None` instead of raising an exception.

## [1.1.0] - 2025-12-04

### Changed

- Change default model for `PlanAndExecuteAgent` to `gpt-5.1` and `gpt-5.1-codex-mini` for planning fallback and code review respectively.
- Improve `PlanAndExecuteAgent` planning output to be more structured and easier to human understand.
- Improve `PlanAndExecuteAgent` planning prompts with "Code minimalism" guidelines to prevent over-engineering and unnecessary changes.
- Migrate all prompt templates from Jinja2 to Mustache format to prevent code injection attacks.
- Replace `plan_think_tool` with `TodoListMiddleware` to allow the agent to maintain a todo list of the tasks to be completed during the planning phase.

### Added

- Add Agent Skills system implementing Anthropic's progressive disclosure pattern.
- Add `creating-agents-md-file` skill to help generate `AGENTS.md` files for repositories, following the [AGENTS.md](https://agents.md/) format.
- Add `maintaining-changelog` skill to help maintain changelog files for pull requests, following existing format conventions or creating new files using Keep a Changelog format.
- Add `creating-daiv-yml-config` skill to help create `.daiv.yml` configuration files with sandbox settings (base_image and format_code commands) based on repository content.
- Add chunked reading capability to `read_tool` with `start_line` and `max_lines` parameters. The tool now supports reading files in segments rather than loading entire file contents, limiting output to a default maximum of 2000 lines. When content is truncated, a message indicates the range shown and total lines available, guiding further reads. This addresses the issue of costly full-file reads for large files.
- Add `SWERepoClient` to support SWE-bench style evaluations with public OSS repositories. This client clones repositories to temporary directories without requiring credentials and is designed for automated testing scenarios.
- Add support for `gpt-5.1`, `gpt-5.1-codex`, `gpt-5.1-codex-mini` models from OpenAI.
- Add OpenRouter support to Anthropic caching middleware, reducing costs.
- Add `FileNavigationMiddleware`, `FileEditingMiddleware`, `MergeRequestMiddleware` and `WebSearchMiddleware` in replacement of toolkits, leveraging LangChain v1 middlewares capabilities to inject the system prompt and tools into the model call.
- Add `EXECUTION_THINKING_LEVEL` configuration to `PlanAndExecuteAgent` to allow users to enable thinking for execution tasks.
- Add `/clone-to-topic` quick action to clone issues to all repositories matching specified topics, enabling bulk distribution of issues across multiple repositories.

### Fixed

- Fix `format_code_tool` to properly apply the patch to the repository even when the command fails.
- Fix inclusion of `.git` directory in the sandbox archive, preventing the agent from accessing the repository and reducing archive size.
- Fix `InvalidNamespaceError` when branch names contain periods (e.g., `fix/python-version-3.11`) by sanitizing namespace labels for LangGraph store.
- Fix `PlanAndExecuteAgent` to use `ToolStrategy` for execution tasks instead of `AutoStrategy` to handle union types.

## [1.0.0] - 2025-11-17

### Changed

- **Breaking:** Limit `CodebaseChatAgent` to answering questions about one repository at a time, given by the repository id passed as a header. This is a direct consequence of removing codebase indexation, which made answering questions about several repositories at once difficult.
- **Breaking:** Refactor the repository configuration file schema to be more flexible and easier to use.
- **Breaking:** Activate the `help` quick action as `@daiv /help` instead of `@daiv help`.
- **Breaking:** Activate the `plan execute` quick action as `@daiv /approve-plan` instead of `@daiv plan execute`.
- **Breaking:** Activate the `plan revise` quick action as `@daiv /revise-plan` instead of `@daiv plan revise`.
- Fix async Celery tasks to properly handle Django connection pooling by implementing `ThreadSensitiveContext` wrapper and worker process signal handlers, preventing connection pool exhaustion.
- Migrate LangChain and LangGraph to v1.x with updated imports and API patterns.
- Improve planning prompt of `PlanAndExecuteAgent` to deal better with asking for clarification, ensuring the agent will ask questions contextualized to the current state of the codebase.
- Change `PullRequestDescriberAgent` to use diffs to describe the changes instead of commit messages, making it more accurate and concise.
- Replace repository read tools `search_code_snippets`, `retrieve_file_content`, and `repository_structure` with the new `glob`, `grep`, `ls`, and `read` tools.
- Replace repository write tools `create_new_repository_file`, `replace_snippet_in_file`, `rename_repository_file` and `delete_repository_file` with the new `write`, `edit`, `delete` and `rename` tools.
- Replace sandbox tools `run_sandbox_commands` and `run_sandbox_code` with the new `bash` tool.
- Replace LLM-based image extraction in `PlanAndExecuteAgent` with regex-based utility function for improved performance and reduced costs.
- Migrate in-memory store based file changes to actual filesystem based file changes and commits tracking using GitPython.
- Migrate default database from `pgvector/pgvector:pg17` to `postgres:17.6`.
- Migrate project from Python 3.13 to Python 3.14.
- Move tools from `daiv/automation/tools` to `daiv/automation/agents/tools`.
- Move quick actions from `daiv/automation/quick_actions` to `daiv/quick_actions`.
- Update project dependencies.
- Update documentation.

### Added

- Add support for the `github` client type to allow users to use GitHub as the client for the codebase.
- Add to `PlanAndExecuteAgent` the capability to:
  - load images from the user message to help the agent to visualize them (using `InjectImagesMiddleware`).
  - plan and execute commands using the DAIV Sandbox tools. This will allow the agent to perform actions on the codebase, such as installing/updating dependencies ensuring lock files are updated, generating translations, etc.
  - load the repository instructions from a `AGENTS.md` file, which is a markdown file that follows the [AGENTS.md](https://agents.md/) format.
  - fix pipelines by retrieving the pipeline status and job logs when planning using the new `pipeline` and `job_logs` tools.
  - review code changes against the plan tasks using the new `review_code_changes` tool ensuring the changes are correct and complete.
- Add `InjectImagesMiddleware` LangChain v1 middleware to automatically extract and process images from markdown/HTML syntax in user messages, supporting GitHub user-attachments, GitLab uploads, and external URLs.
- Add `AGENTS.md` file to the project.
- Add `omit_content_patterns` to DAIV configuration to allow users to omit files content, but visible for the agents (the agent will only be able to see that the file exists, but not its content).
- Add evaluation tests for `CodebaseChatAgent`, `PullRequestDescriberAgent` and `PlanAndExecuteAgent`.
- Add support for `gpt-5`, `gpt-5-nano`, `gpt-5-mini` and `gpt-5-codex` models from OpenAI.
- Add support for `grok-code-fast-1` model from Grok.
- Add support for `claude-sonnet-4.5`, `claude-opus-4.1` and `claude-haiku-4.5` models from Anthropic.
- Add support for `deepseek-v3.1-terminus` model from DeepSeek.
- Add support for `glm-4.6` model from Z-AI.
- Add support for `qwen3-max` and `qwen3-coder-plus` models from Qwen.
- Add support for `kimi-k2-thinking` model from MoonshotAI.
- Add `RECURSION_LIMIT` configuration to `CodebaseChatAgent` to allow users to change the limit of recursive calls to the agent.
- Add support for deleting entire directories with the `delete` tool.

### Removed

- Remove codebase indexation feature in favor of the new navigation tools.
- Remove `CodeDescriberAgent`.
- Remove `CodebaseSearchAgent`.
- Remove `ImageURLExtractorAgent`.
- Remove `SnippetReplacerAgent`.
- Remove `RunSandboxCodeTool`.
- Remove `IssueAddressorAgent` (replaced by `PlanAndExecuteAgent`).
- Remove `PipelineFixerAgent` (replaced by `ReviewAddressorAgent` + `pipeline` and `job_logs` tools).
- Remove all notebooks from the project.
- Remove support for `claude-sonnet-4` and `claude-opus-4` models from Anthropic.
- Remove support for `deepseek-chat-v3.1` model from DeepSeek.
- Remove support for `o4-mini` model from OpenAI.

### Fixed

- Exclude hours and minutes from the current date time format, making prompts cacheable.
- Block GitLab and GitHub callbacks if the client type is not set to the corresponding client.
- Fix `PlanAndExecuteAgent` to avoid reading the same files twice before executing the planned changes.
- Fix `write` and `rename` tools to create parent directories automatically when they don't exist, preventing `FileNotFoundError`.
- Fix sandbox session management to properly reuse sessions across multiple `bash` tool invocations by replacing ContextVar-based storage with LangGraph store-based persistence.
- Fix DuckDuckGo search tool to use the new `ddgs` package instead of the deprecated `duckduckgo-search` package.

## [0.3.0] - 2025-07-25

### Changed

- Make `RunSandboxCommandsTool` and `RunSandboxCodeTool` async only.
- Trigger `PipelineFixerAgent` with a quick action instead of a webhook, so users request a repair plan to fix pipelines by commenting `@daiv pipeline repair` on the merge request.
- Trigger `IssueAddressorAgent` plan approval with a quick action, so users request a plan approval by commenting `@daiv plan execute` on the issue.

### Added

- Add quick actions feature to allow users perform actions by commenting on the merge request or issue.
- Add quick actions to allow users to trigger plan revision by commenting `@daiv plan revise` on the issue.

### Fixed

- Fix `ReviewAddressorAgent` not handling single line notes without line range, which led to empty diff content.
- Fix `IssueAddressorAgent` not handling issues with the bot label on the title, which led to errors; it now removes the bot label from the title ([#435](https://github.com/srtab/daiv/issues/435)).
- Fix mentions to the bot on review comments not being handled, which led the agent to ask for clarification about who is being mentioned in this context ([#436](https://github.com/srtab/daiv/issues/436)).

## [0.2.1] - 2025-06-17

### Changed

- Change default model for `CodebaseChatAgent` to `gpt-4.1`.
- Use the `medium` thinking level by default in the `PlanAndExecuteAgent` planning phase.
- Update deployment documentation to include information about the MCP proxy.
- Improve `PipelineFixerAgent` to ensure the `troubleshoot_analysis_result` (renamed to `complete_task`) tool is called exactly once at the end of the workflow.
- Improve `PipelineFixerAgent` troubleshooting details to include more context about the issue.
- Accept reviews in `ReviewAddressorAgent` only for merge requests that have DAIV mentions on the discussion thread.
- Update MCP `@sentry/mcp-server` to `0.12.0` version.
- Update base python image to `3.13.5`.

### Added

- Add support for `o3` model from OpenAI.
- Build and push the docker image from the `main` branch to allow testing edge versions of the project.

### Removed

- Remove support for `claude-3-7-sonnet` model from Anthropic.

### Fixed

- Attach an `Authorization: Bearer` header in `MCPServer.get_connection` when the `MCP_PROXY_AUTH_TOKEN` secret is configured, ensuring authenticated requests to the MCP proxy ([#419](https://github.com/srtab/daiv/issues/419)).

## [0.2.0] - 2025-06-09

### Changed

- Improve `PlanAndExecuteAgent`:
  - Completely rewrote planning system prompt to be more structured and concise with clear workflow steps and rules of thumb.
  - Enhanced execution system prompt with better organization and clearer instructions for applying change plans.
  - Simplified plan template format for better readability and reduced verbosity.
  - Enhanced tools schema docstrings with more detailed field descriptions and usage guidelines.
  - These improvements affect all agents that use `PlanAndExecuteAgent`: `ReviewAddressorAgent`, `IssueAddressorAgent`, and `PipelineFixerAgent`.
- Improve `ReviewAddressorAgent`:
  - Completely rewrote reviewer response prompt with structured workflow steps, better context handling, and improved reasoning with the `think` tool.
  - Enhanced review planning prompt with clear workflow steps, better diff handling guidance, and structured reasoning process.
  - Improved prompt organization with visual separators and clearer section headers for better readability.
- Improve `PipelineFixerAgent`:
  - Completely rewrote troubleshooting system prompt to be more structured and concise with clear workflow steps and rules of thumb.
  - Enhanced troubleshooting human prompt with better context handling.
  - Simplified troubleshooting template format for better readability and reduced verbosity.
- Make the project async by default.
- Update project dependencies.

### Added

- Add `author` to metadata on `ReviewAddressorAgent` and `IssueAddressorAgent` to track the agent executions on the `langsmith` platform.
- Add MCP tools support to allow the agent to use external tools through MCP servers ([#274](https://github.com/srtab/daiv/issues/274)).

### Fixed

- Fix the GitLab callback ignoring the webhook when an issue changes state from `closed` to `opened`.
- Fix planning questions on Issue Addressor Agent not being handled.
- Fix the recursion limit not being passed to `PlanAndExecuteAgent`, which limited the agent to 25 calls.

## [0.1.5] - 2025-05-26

### Added

- Add `cleanup_indexes` command to clean up outdated indexes and inaccessible repositories.

### Fixed

- Fix connection closed or lost on `ConnectionPool` by using `check_connection` to verify if the connection is still working.
- Fix chunks length check to use the correct number of tokens instead of the number of characters.

## [0.1.4] - 2025-05-22

### Changed

- Optimize `PullRequestDescriberAgent` prompt to improve the quality of the responses for a 0-shot agent.
- Optimize `CodebaseChatAgent` prompts to improve the quality of the responses, reduce hallucinations, gatekeeping first and improve the reasoning capabilities of the agent.
- Use `claude-sonnet-4` as the default model for `PlanAndExecuteAgent` planning and execution.

### Added

- Add support for `claude-sonnet-4` and `claude-opus-4` models from Anthropic.

### Fixed

- Fix the `reply_reviewer` node of `ReviewAddressorAgent` not using the correct tool to reply to the reviewer comments, by refactoring the agent to make it more reliable and robust.
- Fix `SearchCodeSnippetsTool` being called with the `repository` parameter even when `repo_id` was provided, which led to errors; the `repository` parameter is now added to the tool's signature conditionally.
- Fix `Document.id` sometimes being defined as a uuid when retrieving the document from the database, which led to errors.

## [0.1.3] - 2025-05-20

### Added

- Add mapping for `yaml` language for `.yaml` and `.yml` extensions.

### Fixed

- Stop sending temperature to the `o4-mini` model, which does not support it.
- Skip chunks that are too large (more than 2x the chunk size) when indexing, which caused errors in the embedding process ([#378](https://github.com/srtab/daiv/issues/378)).

## [0.1.2] - 2025-05-15

### Changed

- Improve plan comment template readability by adding a separator between each step of the plan.
- Rename `WEB_SEARCH_API_KEY` to `AUTOMATION_WEB_SEARCH_API_KEY` and follow the same pattern for other keys.

### Added

- Add support for `o4-mini` model from OpenAI.
- Add support for defining `LANGSMITH_API_KEY` as docker secrets.

### Fixed

- Fix `ImportError` when `LanguageParser` tries to parse files with `tree-sitter-languages`, which is not installed.
- Fix system checks to verify if required environment variables or docker secrets are set up.
- Fix `PushCallback` to consider only merge requests created by DAIV to avoid indexing every merge request on the project.

## [0.1.1] - 2025-05-14

### Fixed

- Fix `start-app` script passing illegal option `-o`.

## [0.1.0] - 2025-05-13

### Changed

- Improve how to set up a test project on local GitLab in the README.md file.
- Replace `gpt-4o` and `gpt-4o-mini` with the new OpenAI models `gpt-4.1` and `gpt-4.1-mini`.
- Replace `o3-mini` with the new reasoning OpenAI model `o4-mini`.
- Replace `gemini-2.0-flash` and `gemini-2.0-flash-lite-001` with `gpt-4.1-mini` and `gpt-4.1-nano` respectively.
- Simplify `CodebaseChatAgent` and `PullRequestDescriberAgent` prompts to make the agent job—and the prompt reader's job—simpler and less error‑prone.
- Migrate all evaluators/assessments logics to standalone agents to allow testing and customizing them independently from the main agents.
- Parallelize `update_index` process to improve performance.
- Improve codebase chunking process by replacing `RecursiveCharacterTextSplitter` and integrating more specialized splitters for Markdown and all languages supported by tree-sitter-language-pack using Chonkie package. `RecursiveCharacterTextSplitter` is now used as a fallback splitter.
- Add Roadmap section to the README.md.
- Update project urls declared in `pyproject.toml` to use standard labels.
- Use `SecretStr` for sensitive `pydantic` settings to avoid exposing sensitive information.

### Added

- Add security check to the GitLab callback to validate the `X-Gitlab-Token` header ([#93](https://github.com/srtab/daiv/issues/93)).
- Add the possibility to configure `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `WEB_SEARCH_API_KEY` and `EMBEDDINGS_API_KEY` using docker secrets.

### Removed

- Remove dependency on `gunicorn` and use `uvicorn` as the default server.

### Fixed

- Make Sandbox tools more resilient and prevent failing the whole agent execution when the sandbox is unavailable.
- Fix empty repositories not being considered by the repository structure tool, which caused a not found error.
- Fix the repository index not updating the `sha` field on the `CodebaseIndex` model, which caused the index to be considered outdated even when it was not.

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
