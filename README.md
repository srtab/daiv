<p align="center">
  <img src="docs/assets/logo.svg" alt="DAIV" width="400">
</p>
<p align="center"><strong>Open-source, self-hosted SWE agents for GitLab &amp; GitHub</strong></p>
<p align="center">
  Turn issues into merge requests, answer review comments, and fix failing CI —
  with every agent running in a sandbox you control, the network egress you define,
  and the LLM provider you choose.
</p>
<p align="center">
  <img src="https://img.shields.io/python/required-version-toml?tomlFilePath=https%3A%2F%2Fraw.githubusercontent.com%2Fsrtab%2Fdaiv%2Fmain%2Fpyproject.toml" alt="Python Version">
  <a href="https://github.com/srtab/daiv/blob/main/LICENSE"><img src="https://img.shields.io/github/license/srtab/daiv" alt="License"></a>
  <a href="https://github.com/srtab/daiv/actions"><img src="https://github.com/srtab/daiv/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
</p>

---

<p align="center">
  <a href="https://srtab.github.io/daiv/dev/"><strong>Documentation</strong></a> ·
  <a href="https://daivagent.com"><strong>Website</strong></a> ·
  <a href="https://srtab.github.io/daiv/dev/getting-started/deployment/"><strong>Deploy</strong></a> ·
  <a href="https://github.com/srtab/daiv/blob/main/CONTRIBUTING.md"><strong>Contributing</strong></a>
</p>

DAIV integrates directly with **GitLab** and **GitHub** through webhooks — no new tools to adopt, no context-switching. Beyond your Git workflow, DAIV plugs into your editor over **MCP** and ships with an optional **self-hosted dashboard** to chat with the agent, start and watch runs, schedule jobs, and review what changed. You host it, you pick the model, and every task executes in an isolated sandbox whose network access you define.

## Three ways to put DAIV to work

### In your Git platform — webhooks, zero setup

- **[Issue Addressing](https://srtab.github.io/daiv/dev/features/issue-addressing/)** — DAIV reads a labelled issue, proposes a plan, and — once you approve — opens a merge/pull request with the implementation.
- **[Pull Request Assistant](https://srtab.github.io/daiv/dev/features/pull-request-assistant/)** — answers reviewer comments, applies requested changes, and repairs failing CI/CD pipelines, all inside the merge/pull request thread.
- **[Slash Commands & Skills](https://srtab.github.io/daiv/dev/features/slash-commands/)** — invoke `/plan`, `/code-review`, `/help`, and your own custom skills straight from issues and merge requests.

### From your editor and pipelines

- **[MCP Endpoint](https://srtab.github.io/daiv/dev/features/mcp-endpoint/)** — connect Claude Code, Cursor, or Codex CLI over the [Model Context Protocol](https://modelcontextprotocol.io/) and delegate tasks without leaving your editor.
- **[Jobs API](https://srtab.github.io/daiv/dev/features/jobs-api/)** — trigger agents programmatically from CI, scripts, or other tools, then poll for the result.

### From the dashboard

- **[Sessions](https://srtab.github.io/daiv/dev/features/sessions/)** — a unified workspace and history: chat with the agent live, start background runs, and see every execution — webhook, API, MCP, scheduled, or manual — in one list, with retries.
- **[Scheduled Jobs](https://srtab.github.io/daiv/dev/features/scheduled-jobs/)** — run agents on any cron schedule: dependency audits, code-quality scans, stale-branch cleanup, and more.
- **[Sandbox Environments](https://srtab.github.io/daiv/dev/features/sandbox-environments/)** — define a reusable runtime once: base image, CPU/memory, **network egress policy**, and encrypted secrets, scoped to the repositories you choose.
- **Per-run model & effort** — pick the LLM and thinking effort for each run.
- **[Notifications](https://srtab.github.io/daiv/dev/features/notifications/)** — know the moment work finishes, via the in-app bell, email, or Rocket Chat.
- **[Merge Metrics](https://srtab.github.io/daiv/dev/features/merge-metrics/)** — track code velocity with commit-level DAIV-vs-human attribution.

## Quick example

1. **You create an issue:** "Add rate limiting to the API endpoints"
2. **DAIV posts a plan:** Analyzes the codebase and proposes implementation steps
3. **You approve:** Comment `@daiv proceed`
4. **DAIV implements:** Creates a merge request with the code changes
5. **Reviewer asks for changes:** "@daiv use Redis instead of in-memory storage"
6. **DAIV updates the code:** Modifies the implementation and pushes

## Under the hood

DAIV is powered by [Deep Agents](https://github.com/langchain-ai/deepagents), a general-purpose deep-agent framework built on [LangGraph](https://langchain-ai.github.io/langgraph/) with sub-agent spawning, a middleware stack, and a virtual filesystem. On top of it, DAIV adds:

- **[Subagents](https://srtab.github.io/daiv/dev/features/subagents/)** — specialized agents for fast codebase exploration and complex multi-step tasks.
- **[Sandbox](https://srtab.github.io/daiv/dev/features/sandbox/)** — secure command execution for tests, builds, linters, and package management inside an isolated Docker container.
- **[MCP Tools](https://srtab.github.io/daiv/dev/customization/mcp-tools/)** — external integrations over the [Model Context Protocol](https://modelcontextprotocol.io/), such as Sentry for error tracking.
- **[Monitoring](https://srtab.github.io/daiv/dev/reference/monitoring/)** — trace every agent execution with [LangSmith](https://www.langchain.com/langsmith) to analyze performance and spot issues.
- **Scalable Workers** — background workers scale horizontally by adding replicas, with a dedicated scheduler for recurring jobs.
- **[LLM Providers](https://srtab.github.io/daiv/dev/getting-started/llm-providers/)** — run on OpenRouter, Anthropic, OpenAI, or Google — your keys, your choice.

## Technology Stack

- **Agent Framework**: [Deep Agents](https://github.com/langchain-ai/deepagents) — the core agent engine powering DAIV. A general-purpose deep agent with sub-agent spawning, middleware stack, and virtual filesystem. Built on [LangGraph](https://langchain-ai.github.io/langgraph).
- **Backend Framework**: [Django](https://www.djangoproject.com/) for building robust APIs and managing database models.
- **Async Tasks**: [Django Tasks](https://docs.djangoproject.com/en/6.0/topics/tasks/) with the [`django-tasks` backend](https://pypi.org/project/django-tasks/) and [`django-crontask`](https://pypi.org/project/django-crontask/) for periodic scheduling.
- **Code Executor**: [Sandbox](https://github.com/srtab/daiv-sandbox/) for running commands in a secure sandbox to allow the agents to perform actions on the codebase.
- **Observability**: [LangSmith](https://www.langchain.com/langsmith) for tracing and monitoring all the interactions between DAIV and your codebase.
- **Error Handling**: [Sentry](https://sentry.io/) for tracking and analyzing errors.

## Getting Started

```bash
git clone https://github.com/srtab/daiv.git && cd daiv
make setup                 # creates config files from templates
```

Add at least one LLM provider key to `docker/local/app/config.secrets.env` — it is read at
container start — then bring the stack up:

```bash
docker compose up --build  # db, redis, app, worker, scheduler
```

- **Running DAIV for real** → [Deployment](https://srtab.github.io/daiv/dev/getting-started/deployment/), then [Platform Setup](https://srtab.github.io/daiv/dev/getting-started/platform-setup/) to connect GitLab or GitHub.
- **Developing DAIV** → [CONTRIBUTING.md](CONTRIBUTING.md) covers Compose profiles, the local GitLab instance, webhooks, tests, and linting.

## Roadmap

- [ ] Configurable hooks — run DAIV on specific events with user-defined triggers and actions.
- [ ] Chrome extension — interact with DAIV directly from the git platform without leaving the browser.
- [x] Custom MCP servers — user-defined MCP servers via a JSON config file following the Claude Code `.mcp.json` standard.
- [x] Scheduled maintenance tasks — run DAIV on a cron schedule for tasks like dependency updates, security scans, or documentation drift detection.
- [x] Notifications — in-app, email, and Rocket Chat delivery shipped; Slack, Discord, and Microsoft Teams planned.
- [ ] Self-hosted LLM support — enable local model inference via Ollama or vLLM for air-gapped or cost-sensitive environments.

## Agentic Patterns used in this repo

<!-- AGENTIC_BADGES_START -->
<!-- AGENTIC_BADGES_END -->

## Contributing

We welcome contributions! Whether you want to fix a bug, add a new feature, or improve documentation, please refer to the [CONTRIBUTING.md](CONTRIBUTING.md) file for more information.

## License

This project is licensed under the [Apache 2.0 License](LICENSE).

## Support & Community

For questions or support, [open an issue](https://github.com/srtab/daiv/issues). See the [Community page](https://srtab.github.io/daiv/dev/community/) for contribution paths and project links.
