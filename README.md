# DAIV: Development AI Assistant

![Python Version](https://img.shields.io/python/required-version-toml?tomlFilePath=https%3A%2F%2Fraw.githubusercontent.com%2Fsrtab%2Fdaiv%2Fmain%2Fpyproject.toml)
[![GitHub License](https://img.shields.io/github/license/srtab/daiv)](https://github.com/srtab/daiv/blob/main/LICENSE)
[![Actions Status](https://github.com/srtab/daiv/actions/workflows/ci.yml/badge.svg)](https://github.com/srtab/daiv/actions)

DAIV is an open-source automation assistant designed to enhance developer productivity. It integrates seamlessly with **GitLab** repositories to streamline your development process. It uses AI agents and configurable actions to automate common software development tasks such as:

- **Issue Addressing**: Planning and executing solutions directly from issue titles and descriptions.
- **Code Review Assistance**: Automatically responding to reviewer comments, adjusting code, and improving pull requests.
- **Pipeline Failures**: Monitoring CI/CD logs and applying fixes automatically when a pipeline fails.
- **Codebase Chat**: A ChatGPT-like experience to chat with your codebase and get answers.

> [!WARNING] > **Note:** DAIV is currently in a **beta** stage. It is **not recommended for production use** at this time. Bugs or performance issues are expected, and contributions are welcome!

## Key Features

- 🚀 **Automated Issue Resolution**: When an issue is created in your repository, DAIV can parse the description, propose a step-by-step plan, and, after human approval, execute code changes and open a merge request for you to review.
- 💬 **Code Review Addressor**: Assists with code review comments by providing context-aware answers or directly applying requested changes. This reduces the overhead of going back and forth on merge requests.
- 🔧 **Pipeline Fixing**: Identifies failing pipeline jobs, analyzes logs, and attempts auto-remediations (e.g., lint fixes and unit tests) to get the CI/CD pipeline back to green.
- 🧠 **Codebase Chat**: Chat with your codebase for context-aware answers. An OpenAI-compatible API is available for easy integration with tools such as [Open-WebUI](https://github.com/open-webui/open-webui).
- ⚙️ **Configurable Behavior**: A `.daiv.yml` file in your repo's default branch lets you tailor DAIV's features (like toggling auto-issue addressing or pipeline autofix).

## Technology Stack

- **Backend Framework**: [Django](https://www.djangoproject.com/) for building robust APIs and managing database models.
- **Async Tasks**: [Celery](https://docs.celeryproject.org/) with Redis, orchestrating indexing, processing merges, and applying code changes in the background.
- **LLM Frameworks**: [LangChain](https://python.langchain.com/) and [LangGraph](https://langchain-ai.github.io/langgraph), integrating various LLM agents for intent understanding, query transformation, and natural language reasoning about code changes.
- **Search Engines**:
  - **Semantic**: [PGVector](https://github.com/pgvector/pgvector) (PostgreSQL extension) for embedding-based semantic retrieval.
  - **Lexical**: [Tantivy](https://github.com/quickwit-oss/tantivy) for keyword-driven code search.
- **Code Executor**: Tools and managers for fetching files from GitLab, applying code changes via merge requests, and running code in a secure [sandbox](https://github.com/srtab/daiv-sandbox/).
- **Observability**: [Langsmith](https://www.langchain.com/langsmith) for tracing and monitoring all the interactions between DAIV and your codebase.
- **AI Providers**: [OpenAI](https://openai.com/api/), [Anthropic](https://www.anthropic.com/api), and [Gemini](https://ai.google.dev/gemini) are the supported LLM providers.

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
   Copy `docker/local/app/config.secrets.example.env` to `docker/local/app/config.secrets.env` and update it with your GitLab token, OpenAI API Key, Anthropic API Key, Gemini API Key, and LangSmith API Key.

   ```bash
   cp docker/local/app/config.secrets.example.env docker/local/app/config.secrets.env
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
   - PGAdmin (database management): http://localhost:8080
   - GitLab (test repository): http://localhost:8929
   - Sandbox (secure code execution): http://localhost:8888/docs

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
   $ make lint      # to check for linting issues
   $ make lint-fix  # to automatically fix linting errors
   ```

## Contributing

We welcome contributions! Whether you want to fix a bug, add a new feature, or improve documentation, please refer to the [CONTRIBUTING.md](CONTRIBUTING.md) file for more information.

## License

This project is licensed under the [Apache 2.0 License](LICENSE).

## Support & Community

For questions or support, please open an issue in the GitHub repository. Contributions, suggestions, and feedback are greatly appreciated!

**Happy Coding!**
