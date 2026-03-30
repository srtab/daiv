<p align="center">
  <img src="assets/logo.svg" alt="DAIV" width="400">
</p>

<p align="center">
  <strong>Open-source async SWE agent for your Git platform</strong>
</p>

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

DAIV is powered by [Deep Agents](https://github.com/langchain-ai/deepagents), a general-purpose deep agent framework built on [LangGraph](https://langchain-ai.github.io/langgraph/) with sub-agent spawning, a middleware stack, and virtual filesystem. On top of this foundation, DAIV adds:

- **Subagents** — Specialized agents for fast codebase exploration and complex multi-step tasks.
- **Sandbox** — Secure command execution for running tests, builds, linters, and package management inside an isolated Docker container.
- **MCP Tools** — External tool integrations via the [Model Context Protocol](https://modelcontextprotocol.io/), such as Sentry for error tracking.

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

</div>
