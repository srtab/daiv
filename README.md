# DAIV: Development AI Assistant

DAIV is an open-source automation assistant designed to enhance developer productivity. It integrates seamlessly with GitLab repositories and uses AI agents, semantic code search, and configurable actions to automate common software development tasks such as:

- **Issue Addressing**: Suggesting and implementing solutions directly from issue descriptions.
- **Code Review Assistance**: Automatically responding to reviewer comments, adjusting code, and improving pull requests.
- **Pipeline Failures**: Analyzing CI/CD logs and applying known fixes automatically.
- **Semantic & Lexical Code Search**: Quickly retrieving code snippets, classes, or functions from a large codebase.

DAIV leverages Django for its core framework, Celery for asynchronous tasks, LangChain and related models for AI-driven logic, PGVector and Tantivy for code search indexing, and GitLab’s API/webhooks for continuous integration with your source code workflow.

> **Note:** DAIV is currently in an **alpha** stage. It is **not recommended for production use** at this time. Features are under active development and may change without warning.

## Key Features

- **Automated Issue Resolution**: When an issue is created in your repository, DAIV can parse the description, propose a step-by-step plan, and, after human approval, execute code changes and open a merge request.

- **Code Review Addressor**: Assists with code review comments by providing context-aware answers or directly applying requested changes. This reduces the overhead of going back and forth on merge requests.

- **Pipeline Fixing**: Identifies failing pipeline jobs, analyzes logs, and attempts auto-remediations (e.g., lint fixes, dependency updates) to get the CI/CD pipeline back to green.

- **Semantic Code Search**: Combines vector embeddings (via PGVector) and lexical search (via Tantivy) to quickly find relevant code snippets within your repository, improving developer efficiency.

- **Configurable Behavior**: A `.daiv.yml` file in your repo’s default branch lets you tailor DAIV’s features (like toggling auto-issue addressing or pipeline autofix).

## Technology Stack

- **Backend Framework**: [Django](https://www.djangoproject.com/) for building robust APIs and managing database models.
- **Async Tasks**: [Celery](https://docs.celeryproject.org/) + Redis, orchestrating indexing, processing merges, and applying code changes in the background.
- **AI & LLM Integration**: [LangChain](https://langchain.ai/), integrating various LLM agents for intent understanding, query transformation, and natural language reasoning about code changes.
- **Search Engines**:
  - **Semantic**: PGVector (PostgreSQL extension) for embedding-based semantic retrieval.
  - **Lexical**: Tantivy-based lexical indexing for keyword-driven code search.
- **Code Interaction**: Tools and managers for fetching files from GitLab, applying code changes via merge requests, and running code in a secure [sandbox](https://github.com/srtab/daiv-sandbox/).

## Getting Started

### Prerequisites

- **Python 3.12+**
- **Docker & Docker Compose**

### Local Development Setup

1. **Clone the repository**:

   ```bash
   git clone https://github.com/srtab/daiv.git
   cd daiv
   ```

2. **Configure Environment**:
   Copy `docker/local/app/config.secrets.example.env` to `docker/local/app/config.secrets.env` and update it with your GitLab token, OpenAI API Key, Anthropic API Key and LangSmith API Key.

   ```bash
   cp docker/local/app/config.secrets.example.env docker/local/app/config.secrets.env
   ```

3. **Install Dependencies** (optional):
   Use [uv](https://astral.sh/uv/):

   ```bash
   uv sync
   ```

   This will install the project dependencies into a virtual environment. Useful for running tests and linting outside of Docker.

4. **Start the Server**:

   ```bash
   docker-compose up --build
   ```

   This will start the web app, workers, Redis, PostgreSQL, and GitLab services locally.

## Running Tests

DAIV includes a suite of tests. To run tests with coverage:

```bash
make test
```

This runs `pytest` under the hood.

## Contributing

We welcome contributions! Whether you want to fix a bug, add a new feature, or improve documentation:

1. Fork the repository.
2. Create a new feature branch: `git checkout -b feat/my-feature`.
3. Commit changes and push to your fork.
4. Open a pull request explaining your changes.

Please ensure all code follows project coding standards (lint with `make lint`) and that you add tests or documentation as needed.

## License

This project is licensed under the [Apache 2.0 License](LICENSE).

## Support & Community

For questions or support, open an issue in the GitHub repository. Contributions, suggestions, and feedback are greatly appreciated!

**Happy Coding!**
