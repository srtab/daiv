<p align="center">
  <img src="assets/logo.svg" alt="DAIV" width="400">
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

DAIV integrates directly with **GitLab** and **GitHub** through webhooks — no new tools to adopt, no context-switching. Beyond your Git workflow, DAIV plugs into your editor over **MCP** and ships with an optional **self-hosted dashboard** to chat with the agent, start and watch runs, schedule jobs, and review what changed. You host it, you pick the model, and every task executes in an isolated sandbox whose network access you define.

## Three ways to put DAIV to work

### In your Git platform — webhooks, zero setup

- **[Issue Addressing](features/issue-addressing.md)** — DAIV reads a labelled issue, proposes a plan, and — once you approve — opens a merge/pull request with the implementation.
- **[Pull Request Assistant](features/pull-request-assistant.md)** — answers reviewer comments, applies requested changes, and repairs failing CI/CD pipelines, all inside the merge/pull request thread.
- **[Slash Commands & Skills](features/slash-commands.md)** — invoke `/plan`, `/code-review`, `/help`, and your own custom skills straight from issues and merge requests.

### From your editor and pipelines

- **[MCP Endpoint](features/mcp-endpoint.md)** — connect Claude Code, Cursor, or Codex CLI over the [Model Context Protocol](https://modelcontextprotocol.io/) and delegate tasks without leaving your editor.
- **[Jobs API](features/jobs-api.md)** — trigger agents programmatically from CI, scripts, or other tools, then poll for the result.

### From the dashboard

- **[Chat](features/chat.md)** — a live workspace: prompt the agent and watch it read, edit, and run commands in real time, on a thread you can leave and return to.
- **[Activity & run composer](features/activity-tracking.md)** — start runs from the UI and see every execution — webhook, API, MCP, scheduled, or manual — in one log, with retries.
- **[Scheduled Jobs](features/scheduled-jobs.md)** — run agents on any cron schedule: dependency audits, code-quality scans, stale-branch cleanup, and more.
- **[Sandbox Environments](features/sandbox-environments.md)** — define a reusable runtime once: base image, CPU/memory, **network egress policy**, and encrypted secrets, scoped to the repositories you choose.
- **Per-run model & effort** — pick the LLM and thinking effort for each run.
- **[Notifications](features/notifications.md)** — know the moment work finishes, via the in-app bell, email, or Rocket Chat.
- **[Merge Metrics](features/merge-metrics.md)** — track code velocity with commit-level DAIV-vs-human attribution.

## Quick example

1. **You create an issue:** "Add rate limiting to the API endpoints"
2. **DAIV posts a plan:** Analyzes the codebase and proposes implementation steps
3. **You approve:** Comment `@daiv proceed`
4. **DAIV implements:** Creates a merge request with the code changes
5. **Reviewer asks for changes:** "@daiv use Redis instead of in-memory storage"
6. **DAIV updates the code:** Modifies the implementation and pushes

## Under the hood

DAIV is powered by [Deep Agents](https://github.com/langchain-ai/deepagents), a general-purpose deep-agent framework built on [LangGraph](https://langchain-ai.github.io/langgraph/) with sub-agent spawning, a middleware stack, and a virtual filesystem. On top of it, DAIV adds:

- **Subagents** — specialized agents for fast codebase exploration and complex multi-step tasks.
- **Sandbox** — secure command execution for tests, builds, linters, and package management inside an isolated Docker container.
- **MCP Tools** — external integrations over the [Model Context Protocol](https://modelcontextprotocol.io/), such as Sentry for error tracking.
- **Monitoring** — trace every agent execution with [LangSmith](https://www.langchain.com/langsmith) to analyze performance and spot issues. See [Monitoring](reference/monitoring.md).
- **Scalable Workers** — background workers scale horizontally by adding replicas, with a dedicated scheduler for recurring jobs.
- **LLM Providers** — run on OpenRouter, Anthropic, OpenAI, or Google — your keys, your choice. See [LLM Providers](getting-started/llm-providers.md).

## Supported platforms

<div class="grid cards" markdown>

-   :simple-gitlab: **GitLab**

    ---

    GitLab.com and self-hosted instances. Full feature support.

-   :simple-github: **GitHub**

    ---

    GitHub.com and GitHub Enterprise. Full feature support.

</div>

## Get started

<div class="grid cards" markdown>

-   **Deploy DAIV**

    ---

    Install and run DAIV with Docker Compose or Docker Swarm.

    [:octicons-arrow-right-24: Deployment](getting-started/deployment.md)

-   **Connect your repository**

    ---

    Link DAIV to your GitLab or GitHub repository.

    [:octicons-arrow-right-24: Platform Setup](getting-started/platform-setup.md)

-   **Choose your LLM**

    ---

    Configure OpenRouter, Anthropic, OpenAI, or Google as your provider.

    [:octicons-arrow-right-24: LLM Providers](getting-started/llm-providers.md)

-   **Customize behavior**

    ---

    Tailor DAIV to your team with `.daiv.yml`, skills, and MCP tools.

    [:octicons-arrow-right-24: Repository Config](customization/repository-config.md)

-   **Use from your editor**

    ---

    Connect Claude Code, Cursor, or Codex CLI to DAIV via MCP.

    [:octicons-arrow-right-24: MCP Endpoint](features/mcp-endpoint.md)

-   **Automate recurring tasks**

    ---

    Run agents on a schedule — dependency audits, cleanup, reports, and more.

    [:octicons-arrow-right-24: Scheduled Jobs](features/scheduled-jobs.md)

</div>
