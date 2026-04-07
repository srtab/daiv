<p align="center">
  <img src="docs/assets/logo.svg" alt="DAIV" width="400">
</p>
<p align="center"><strong>Open-source async SWE agent for your Git platform</strong></p>
<p align="center">
  <img src="https://img.shields.io/python/required-version-toml?tomlFilePath=https%3A%2F%2Fraw.githubusercontent.com%2Fsrtab%2Fdaiv%2Fmain%2Fpyproject.toml" alt="Python Version">
  <a href="https://github.com/srtab/daiv/blob/main/LICENSE"><img src="https://img.shields.io/github/license/srtab/daiv" alt="License"></a>
  <a href="https://github.com/srtab/daiv/actions"><img src="https://github.com/srtab/daiv/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
</p>

---

DAIV integrates directly with **GitLab** and **GitHub** repositories through webhooks. No separate interface needed — you keep using your existing workflow while DAIV handles automation in the background.

## What DAIV does

DAIV automates routine software engineering work so you can focus on creative problem-solving:

- **Issue Addressing** — Converts issue descriptions into working code. DAIV reads the issue, generates a plan, waits for your approval, then opens a merge/pull request with the implementation.
- **Pull Request Assistant** — Responds to reviewer comments, applies requested changes, and repairs failing CI/CD pipelines — all from within the merge/pull request conversation.
- **Slash Commands & Skills** — Invoke commands and skills directly from issues and merge requests (`/help`, `/plan`, `/code-review`, `/clone-to-topics`). Built-in skills provide planning, code review, and security audits — and you can create your own.
- **Scheduled Jobs** — Run agents on a recurring basis — hourly, daily, weekly, or any custom cron expression. Automate dependency audits, code quality scans, stale branch cleanup, and more without CI pipelines or external schedulers.

## Quick example

1. **You create an issue:** "Add rate limiting to the API endpoints"
2. **DAIV posts a plan:** Analyzes the codebase and proposes implementation steps
3. **You approve:** Comment `@daiv proceed`
4. **DAIV implements:** Creates a merge request with the code changes
5. **Reviewer asks for changes:** "@daiv use Redis instead of in-memory storage"
6. **DAIV updates the code:** Modifies the implementation and pushes

## Under the hood

DAIV's agent has access to a set of capabilities that make this possible:

- **Scalable Workers** — Handle more concurrent tasks by increasing worker replicas — no architecture changes needed.
- **Subagents** — Specialized agents for fast codebase exploration and complex multi-step tasks.
- **Sandbox** — Secure command execution for running tests, builds, linters, and package management inside an isolated Docker container.
- **MCP Tools** — External tool integrations via the [Model Context Protocol](https://modelcontextprotocol.io/), such as Sentry for error tracking.
- **Monitoring** — Track agent behavior with [LangSmith](https://www.langchain.com/langsmith) to analyze performance and identify issues.
- **LLM Providers** — [OpenRouter](https://openrouter.ai/), [Anthropic](https://www.anthropic.com/api), [OpenAI](https://openai.com/api/), and [Google Gemini](https://ai.google.dev/gemini).

## Technology Stack

- **Agent Framework**: [Deep Agents](https://github.com/langchain-ai/deepagents) — the core agent engine powering DAIV. A general-purpose deep agent with sub-agent spawning, middleware stack, and virtual filesystem. Built on [LangGraph](https://langchain-ai.github.io/langgraph).
- **Backend Framework**: [Django](https://www.djangoproject.com/) for building robust APIs and managing database models.
- **Async Tasks**: [Django Tasks](https://docs.djangoproject.com/en/6.0/topics/tasks/) with the [`django-tasks` backend](https://pypi.org/project/django-tasks/) and [`django-crontask`](https://pypi.org/project/django-crontask/) for periodic scheduling.
- **Code Executor**: [Sandbox](https://github.com/srtab/daiv-sandbox/) for running commands in a secure sandbox to allow the agents to perform actions on the codebase.
- **Observability**: [LangSmith](https://www.langchain.com/langsmith) for tracing and monitoring all the interactions between DAIV and your codebase.
- **Error Handling**: [Sentry](https://sentry.io/) for tracking and analyzing errors.

## Getting Started

### Prerequisites

- **Docker & Docker Compose**

### Local Development Setup

1. **Clone the repository**:

   ```bash
   git clone https://github.com/srtab/daiv.git
   cd daiv
   ```

2. **Run setup**:

   ```bash
   make setup
   ```

   This creates config files from their templates (`config.secrets.env` and `config.toml`). Edit `docker/local/app/config.secrets.env` and add your API keys — at minimum one LLM provider key (Anthropic, OpenAI, Google, or OpenRouter) and `CODEBASE_GITLAB_AUTH_TOKEN` if using GitLab.

3. **Install Dependencies** (optional):
   We use [uv](https://docs.astral.sh/uv/) to manage dependencies on DAIV.

   ```bash
   uv sync
   ```

   > [!NOTE]
   > This will install the project dependencies into a virtual environment. Useful for running linting outside of Docker or enabling autocompletion in VSCode.

4. **Start core services**:

   ```bash
   docker compose up --build
   ```

   This starts the core services (db, redis, app, worker, scheduler). SSL certificates are auto-generated on first run.

   - DAIV API documentation: https://localhost:8000/api/docs/

5. **Start optional services** (as needed):

   ```bash
   docker compose --profile gitlab up     # local GitLab instance + runner
   docker compose --profile sandbox up    # sandbox code executor
   docker compose --profile mcp up        # MCP servers
   docker compose --profile full up       # all services
   ```

   > [!NOTE]
   > Profiles can be combined: `docker compose --profile gitlab --profile sandbox up`

6. **Run the tests** (optional):
   DAIV includes a comprehensive test suite. To run tests with coverage:

   ```bash
   $ docker compose exec -it app bash
   $ make test
   ```

7. **Run linting** (optional):
   To ensure code quality:

   ```bash
   $ docker compose exec -it app bash
   $ make lint      # to check for linting and formatting issues
   $ make lint-fix  # to automatically fix linting and formatting issues
   ```

### Optional: Local GitLab

To test DAIV with a local GitLab instance:

1. **Start GitLab**:

   ```bash
   docker compose --profile gitlab up
   ```

2. **Get the root password**:

   ```bash
   docker compose exec -it gitlab grep 'Password:' /etc/gitlab/initial_root_password
   ```

3. **Configure a personal access token** at [http://localhost:8929](http://localhost:8929) (use the root user or create a new user) and add it to `docker/local/app/config.secrets.env` as `CODEBASE_GITLAB_AUTH_TOKEN`.

4. **Create a test project** in GitLab and push your testing code to it.

   > [!TIP]
   > You can import using repository URL: go to `Admin Area` -> `Settings` -> `General` -> `Import and export settings` and check the `Repository by URL` option.

5. **Set up webhooks**:

   ```bash
   docker compose exec -it app django-admin setup_webhooks
   ```

   > [!NOTE]
   > If you get the error `Invalid url given`, go to `Admin Area` -> `Settings` -> `Network` -> `Outbound requests` and check `Allow requests to the local network from webhooks and integrations`.

6. **Test DAIV** by creating an issue in your repository with the `daiv` label. DAIV will automatically present a plan to address the issue.

> [!NOTE]
> For GitHub integration, you'll need to use GitHub.com or your own GitHub Enterprise instance. Set `CODEBASE_CLIENT=github` in `docker/local/app/config.env` and configure the GitHub App credentials.


## Roadmap

- [ ] Configurable hooks — run DAIV on specific events with user-defined triggers and actions.
- [ ] Chrome extension — interact with DAIV directly from the git platform without leaving the browser.
- [x] Custom MCP servers — user-defined MCP servers via a JSON config file following the Claude Code `.mcp.json` standard.
- [x] Scheduled maintenance tasks — run DAIV on a cron schedule for tasks like dependency updates, security scans, or documentation drift detection.
- [ ] Team notifications — Slack, Discord, and Microsoft Teams integrations to notify teams about DAIV actions and request approvals.
- [ ] Self-hosted LLM support — enable local model inference via Ollama or vLLM for air-gapped or cost-sensitive environments.


## Contributing

We welcome contributions! Whether you want to fix a bug, add a new feature, or improve documentation, please refer to the [CONTRIBUTING.md](CONTRIBUTING.md) file for more information.

## License

This project is licensed under the [Apache 2.0 License](LICENSE).

## Support & Community

For questions or support, please open an issue in the GitHub repository. Contributions, suggestions, and feedback are greatly appreciated!
