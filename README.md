<p align="center">
  <img src="docs/assets/logo.svg" alt="DAIV" width="400">
</p>
<h1 align="center">Issues in. Merge requests out. On your infrastructure.</h1>
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
  <a href="https://github.com/srtab/daiv/stargazers"><img src="https://img.shields.io/github/stars/srtab/daiv?style=flat" alt="GitHub stars"></a>
</p>
<p align="center">
  <img src="https://github.com/user-attachments/assets/52fc593f-ab5e-4fcb-8660-1a885448c40f" alt="A GitLab issue is labelled daiv; DAIV posts a plan; the author replies @daiv proceed; DAIV opens a merge request whose pipeline passes" width="800">
</p>

---

<p align="center">
  <a href="https://srtab.github.io/daiv/dev/"><strong>Documentation</strong></a> ·
  <a href="https://daivagent.com"><strong>Website</strong></a> ·
  <a href="https://srtab.github.io/daiv/dev/getting-started/deployment/"><strong>Deploy</strong></a> ·
  <a href="https://github.com/srtab/daiv/blob/main/CONTRIBUTING.md"><strong>Contributing</strong></a>
</p>

DAIV (pronounced "dive", from *dev* + *AI*) integrates directly with **GitLab** and **GitHub** through webhooks — no new tools to adopt, no context-switching. Beyond your Git workflow, DAIV plugs into your editor over **MCP** and ships with an optional **self-hosted dashboard** to chat with the agent, start and watch runs, schedule jobs, and review what changed. You host it, you pick the model, and every task executes in an isolated sandbox whose network access you define.

## Why DAIV

- **It lives where you already work.** Issue → plan → merge request, replies to review comments, and CI fixes all happen inside GitLab and GitHub threads, self-managed GitLab included.
- **Your infrastructure, your model, your network rules.** Run it on your own servers, with any of the built-in LLM providers or your own OpenAI-compatible server, in a sandbox whose egress you define.
- **Open source, no per-seat pricing.** Apache-2.0, with nothing to buy per developer.

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

## Security model

DAIV acts on issues, comments and code that other people wrote, so each layer below limits what a run can reach:

- **Isolated sandbox.** Every command the agent runs executes in a Docker container managed by [daiv-sandbox](https://github.com/srtab/daiv-sandbox), optionally under [gVisor](https://gvisor.dev/) for a stronger boundary against container escape.
- **No network unless you allow it.** The sandbox has no network access by default. A [sandbox environment](https://srtab.github.io/daiv/dev/features/sandbox-environments/) can turn it on with an egress allow-list that blocks every other host, and if the egress proxy is unavailable the run aborts rather than falling back to an open network.
- **Credentials stay outside the container.** A credential for an allowed host, such as a private registry token, is stored encrypted and added to matching requests by the egress proxy, so the agent never sees it.
- **Short-lived, scoped git tokens.** On GitLab, DAIV clones and pushes with a short-lived token scoped to one project instead of its main token (on GitLab.com this needs Premium or Ultimate, otherwise it falls back to the main token). On GitHub, it uses a short-lived GitHub App installation token scoped to the one repository.
- **People stay in charge.** On an issue labelled `daiv`, DAIV posts a plan and writes code only after someone approves it. `allowed_usernames` in [`.daiv.yml`](https://srtab.github.io/daiv/dev/customization/repository-config/) limits who can trigger DAIV, which you should set on public repositories.

Prompt injection through an issue, a comment or repository content is still possible: these layers limit what an injected instruction could reach, not whether the agent reads one. Report vulnerabilities privately, as described in [SECURITY.md](SECURITY.md).

## How it compares

Pick the tool that fits your team:

- **GitHub Copilot's coding agent** is built into GitHub and needs no infrastructure of your own. If your code is on GitHub and you already pay for Copilot, start there.
- **GitLab Duo** builds agents into GitLab itself. If your GitLab subscription already includes it, try it first.
- **Claude Code in CI** (GitHub Actions or GitLab CI/CD) runs Anthropic's agent inside your own pipelines, if you want Claude in CI rather than a long-running service.
- **OpenHands** is also open source and self-hostable, with a larger community and a general-purpose agent that goes beyond Git workflows.

DAIV is for teams that want one agent in their GitLab or GitHub threads, running on their own servers, with the model and network rules they choose, and a dashboard, MCP endpoint and scheduled jobs in the same deployment.

## Under the hood

DAIV is powered by [Deep Agents](https://github.com/langchain-ai/deepagents), a general-purpose deep-agent framework built on [LangGraph](https://langchain-ai.github.io/langgraph/) with sub-agent spawning, a middleware stack, and a virtual filesystem. On top of it, DAIV adds:

- **[Subagents](https://srtab.github.io/daiv/dev/features/subagents/)** — specialized agents for fast codebase exploration and complex multi-step tasks.
- **[Sandbox](https://srtab.github.io/daiv/dev/features/sandbox/)** — secure command execution for tests, builds, linters, and package management inside an isolated Docker container.
- **[MCP Tools](https://srtab.github.io/daiv/dev/customization/mcp-tools/)** — external integrations over the [Model Context Protocol](https://modelcontextprotocol.io/), such as Sentry for error tracking.
- **[Monitoring](https://srtab.github.io/daiv/dev/reference/monitoring/)** — trace every agent execution with [LangSmith](https://www.langchain.com/langsmith) to analyze performance and spot issues.
- **Scalable Workers** — background workers scale horizontally by adding replicas, with a dedicated scheduler for recurring jobs.
- **[LLM Providers](https://srtab.github.io/daiv/dev/getting-started/llm-providers/)** — run on OpenRouter, Anthropic, OpenAI, or Google, or on your own OpenAI-compatible server (vLLM, llama.cpp, LiteLLM) — your keys, your choice. Tested with Claude Sonnet 5.5, Opus 5.5 and Haiku 5.5, GPT-6.1 Sol and GPT-6 Luna, GLM-5.2 through OpenRouter, and Qwen3.8 on a self-hosted vLLM server.

## Technology Stack

- **Agent Framework**: [Deep Agents](https://github.com/langchain-ai/deepagents) — the core agent engine powering DAIV. A general-purpose deep agent with sub-agent spawning, middleware stack, and virtual filesystem. Built on [LangGraph](https://langchain-ai.github.io/langgraph).
- **Backend Framework**: [Django](https://www.djangoproject.com/) for building robust APIs and managing database models.
- **Async Tasks**: [Django Tasks](https://docs.djangoproject.com/en/6.0/topics/tasks/) with the [`django-tasks-db` backend](https://pypi.org/project/django-tasks-db/) and [`django-crontask`](https://pypi.org/project/django-crontask/) for periodic scheduling.
- **Code Executor**: [Sandbox](https://github.com/srtab/daiv-sandbox/) for running commands in a secure sandbox to allow the agents to perform actions on the codebase.
- **Observability**: [LangSmith](https://www.langchain.com/langsmith) for tracing and monitoring all the interactions between DAIV and your codebase.
- **Error Handling**: [Sentry](https://sentry.io/) for tracking and analyzing errors.

## Getting Started

To try DAIV, follow the [Quickstart](https://srtab.github.io/daiv/dev/getting-started/quickstart/): it runs DAIV on one
machine with Docker Compose and turns a GitLab issue into a merge request in about 15 minutes, no clone needed.

To run the development stack from a clone instead:

```bash
git clone https://github.com/srtab/daiv.git && cd daiv
make setup                 # creates config files from templates
```

Add at least one LLM provider key to `docker/local/app/config.secrets.env` — it is read at
container start — then bring the stack up:

```bash
docker compose up --build  # db, redis, app, worker, worker-interactive, scheduler
```

- **Running DAIV for real** → [Deployment](https://srtab.github.io/daiv/dev/getting-started/deployment/), then [Platform Setup](https://srtab.github.io/daiv/dev/getting-started/platform-setup/) to connect GitLab or GitHub.
- **Developing DAIV** → [CONTRIBUTING.md](CONTRIBUTING.md) covers Compose profiles, the local GitLab instance, webhooks, tests, and linting.

## Roadmap

- [ ] Configurable hooks — run DAIV on specific events with user-defined triggers and actions.
- [ ] Chrome extension — interact with DAIV directly from the git platform without leaving the browser.
- [x] Custom MCP servers — user-defined MCP servers via a JSON config file following the Claude Code `.mcp.json` standard.
- [x] Scheduled maintenance tasks — run DAIV on a cron schedule for tasks like dependency updates, security scans, or documentation drift detection.
- [x] Notifications — in-app, email, and Rocket Chat delivery shipped; Slack, Discord, and Microsoft Teams planned.
- [x] Self-hosted LLM support — register any OpenAI-compatible server, such as vLLM, llama.cpp, or LiteLLM, as a provider for air-gapped or cost-sensitive environments.


## Contributing

We welcome contributions! Whether you want to fix a bug, add a new feature, or improve documentation, please refer to the [CONTRIBUTING.md](CONTRIBUTING.md) file for more information.

## License

DAIV is [Apache-2.0](LICENSE) and will stay that way. There is no paid tier today; if paid support or hosting is ever offered, it will run the same open-source code, not a separately licensed edition. It is maintained by Sandro Rodrigues.

## Support & Community

Ask questions and share ideas in [Discussions](https://github.com/srtab/daiv/discussions), and report bugs in [Issues](https://github.com/srtab/daiv/issues). Report security vulnerabilities privately, as described in [SECURITY.md](SECURITY.md). Everyone taking part is expected to follow the [Code of Conduct](CODE_OF_CONDUCT.md). See the [Community page](https://srtab.github.io/daiv/dev/community/) for contribution paths and project links.

If DAIV saves you time, [star it on GitHub](https://github.com/srtab/daiv): it helps other teams find it.
