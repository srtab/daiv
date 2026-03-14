<h1 align="center">DAIV</h1>
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

## Quick example

1. **You create an issue:** "Add rate limiting to the API endpoints"
2. **DAIV posts a plan:** Analyzes the codebase and proposes implementation steps
3. **You approve:** Comment `@daiv proceed`
4. **DAIV implements:** Creates a merge request with the code changes
5. **Reviewer asks for changes:** "@daiv use Redis instead of in-memory storage"
6. **DAIV updates the code:** Modifies the implementation and pushes

## Under the hood

DAIV's agent has access to a set of capabilities that make this possible:

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

2. **Configure Environment**:
   Copy `docker/local/app/config.secrets.env.example` to `docker/local/app/config.secrets.env` and update it with your Git platform credentials (GitLab token or GitHub App credentials), OpenAI API Key, Anthropic API Key, Google API Key, and LangChain API Key.

   ```bash
   cp docker/local/app/config.secrets.env.example docker/local/app/config.secrets.env
   ```

3. **Install Dependencies** (optional):
   We use [uv](https://docs.astral.sh/uv/) to manage dependencies on DAIV.

   ```bash
   uv sync
   ```

   > [!NOTE]
   > This will install the project dependencies into a virtual environment. Useful for running linting outside of Docker or enabling autocompletion in VSCode.

4. **Start the Server**:

   ```bash
   docker compose up --build
   ```

   This will start all needed services locally. You can access them at:

   - DAIV API documentation: https://localhost:8000/api/docs/
   - GitLab (local test repository platform): http://localhost:8929
   - Sandbox (secure code execution): http://localhost:8888/docs

   > [!NOTE]
   > The local development setup includes a GitLab instance for testing. For GitHub integration, you'll need to use GitHub.com or your own GitHub Enterprise instance.

5. **Run the tests** (optional):
   DAIV includes a comprehensive test suite. To run tests with coverage:

   ```bash
   $ docker compose exec -it app bash
   $ make test
   ```

6. **Run linting** (optional):
   To ensure code quality:

   ```bash
   $ docker compose exec -it app bash
   $ make lint      # to check for linting and formatting issues
   $ make lint-fix  # to automatically fix linting and formatting issues
   ```

7. **Configure test repository**:
   To be able to test DAIV, you need to configure a test repository on the local GitLab instance (or use your own GitHub repository).

   1. First you need to obtain root password to authenticate with [local GitLab](http://localhost:8929):

      ```bash
      $ docker compose exec -it gitlab grep 'Password:' /etc/gitlab/initial_root_password
      ```

   2. Then you need to configure a personal access token (you can use the root user or create a new user) and add it to the `docker/local/app/config.secrets.env` file.

   3. Now you need to create a new project in GitLab and follow the instructions to push your testing code to it.

      > [!TIP]
      > You can import using repository URL, go to `Admin Area` -> `Settings` -> `General` -> `Import and export settings` and check the `Repository by URL` option.

   4. After you push/import your code to the repository, you need to set up webhooks and index the repository in DAIV:

      ```bash
      # Enter the app container
      $ docker compose exec -it app bash

      # Set up webhooks to trigger automatically DAIV actions. You can disable SSL verification for local development by adding `--disable-ssl-verification` to the command.
      $ django-admin setup_webhooks

      ```

      > [!NOTE]
      > If you're getting the error `Invalid url given` when setting up the webhooks on local GitLab, go to `Admin Area` -> `Settings` -> `Network` -> `Outbound requests` and check the `Allow requests to the local network from webhooks and integrations` option.

   5. Finally, you can test DAIV by creating an issue in your repository, add `daiv` label to it and see how DAIV will automatically present a plan to address the issue.


## Roadmap

- [x] Add support to GitHub.
- [x] Add support to [AGENTS.md](https://agents.md/) format to guide agents.
- [x] Add support to Agent Skills.
- [ ] Add support to custom MCP servers.
- [x] Add an evaluation system to measure the quality of DAIV's agents.
- [ ] Add support to automated code review.
- [ ] Create a frontend to DAIV initial setup and configuration, dashboard with some metrics, a chat interface to interact with DAIV...
- [ ] Automate the onboarding of new projects into DAIV, by adding a `.daiv.yml` file to the repository.


## Contributing

We welcome contributions! Whether you want to fix a bug, add a new feature, or improve documentation, please refer to the [CONTRIBUTING.md](CONTRIBUTING.md) file for more information.

## License

This project is licensed under the [Apache 2.0 License](LICENSE).

## Support & Community

For questions or support, please open an issue in the GitHub repository. Contributions, suggestions, and feedback are greatly appreciated!
