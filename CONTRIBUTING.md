# Contributing to DAIV

Thank you for your interest in contributing to DAIV! This document provides guidelines and instructions for contributing to the project. By participating in this project, you agree to abide by its terms.

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Development Environment](#development-environment)
- [Development Guidelines](#development-guidelines)
  - [Code Style](#code-style)
  - [Testing](#testing)
  - [Type Checking](#type-checking)
- [Making Contributions](#making-contributions)
  - [Branch Naming Convention](#branch-naming-convention)
  - [Commit Messages](#commit-messages)
  - [Pull Request Process](#pull-request-process)
- [Reporting Issues](#reporting-issues)
- [License](#license)

## Code of Conduct

We expect all contributors to be respectful and constructive. Please ensure that your interactions with the community are positive and inclusive.

## Development Environment

**Prerequisites:** Docker and Docker Compose.

1. **Clone and configure**

   ```bash
   git clone https://github.com/srtab/daiv.git && cd daiv
   make setup
   ```

   `make setup` creates `config.secrets.env` and `config.toml` from their templates. Edit `docker/local/app/config.secrets.env` and add your API keys — at minimum one LLM provider key (Anthropic, OpenAI, Google, or OpenRouter), plus `CODEBASE_GITLAB_AUTH_TOKEN` if you are using GitLab.

2. **Install dependencies** (optional)

   DAIV uses [uv](https://docs.astral.sh/uv/) for dependency management:

   ```bash
   uv sync
   ```

   This installs into a virtual environment — useful for running linters outside Docker or for editor autocompletion.

3. **Start core services**

   ```bash
   docker compose up --build
   ```

   Starts db, redis, app, worker, and scheduler. SSL certificates are generated on first run. The API docs are then at <https://localhost:8000/api/docs/>.

4. **Start optional services** as needed

   ```bash
   docker compose --profile gitlab up     # local GitLab instance + runner
   docker compose --profile sandbox up    # sandbox code executor
   docker compose --profile mcp up        # MCP servers
   docker compose --profile full up       # everything
   ```

   Profiles combine: `docker compose --profile gitlab --profile sandbox up`.

### Testing against a local GitLab

1. **Start GitLab**

   ```bash
   docker compose --profile gitlab up
   ```

2. **Get the root password**

   ```bash
   docker compose exec -it gitlab grep 'Password:' /etc/gitlab/initial_root_password
   ```

3. **Create a personal access token** at <http://localhost:8929> and add it to `docker/local/app/config.secrets.env` as `CODEBASE_GITLAB_AUTH_TOKEN`.

4. **Create a test project** and push some code to it.

   To import an existing repository: `Admin Area` → `Settings` → `General` → `Import and export settings`, and enable `Repository by URL`.

5. **Set up webhooks**

   ```bash
   docker compose exec -it app django-admin setup_webhooks
   ```

   If this fails with `Invalid url given`, go to `Admin Area` → `Settings` → `Network` → `Outbound requests` and enable `Allow requests to the local network from webhooks and integrations`.

6. **Try it** — create an issue labelled `daiv`. DAIV will respond with a plan.

For GitHub, use GitHub.com or a GitHub Enterprise instance: set `CODEBASE_CLIENT=github` in `docker/local/app/config.env` and configure the GitHub App credentials.

Deploying DAIV for real use is a different path — see the [Deployment guide](https://srtab.github.io/daiv/latest/getting-started/deployment/).

## Development Guidelines

### Code Style

DAIV uses [ruff](https://github.com/astral-sh/ruff) for linting and formatting:

- **Linting**: `make lint-check`
- **Formatting**: `make lint-format`
- **Linting and formatting**: `make lint`
- **Fix linting and formatting issues**: `make lint-fix`

Our code formatting configuration includes:

- Line length: 120 characters
- Target Python version: 3.14
- isort configuration for import sorting

Before submitting a pull request, ensure your code passes all linting checks:

```bash
make lint
```

### Testing

DAIV uses pytest for testing:

1. **Run all tests**:

   ```bash
   make test
   ```

2. **Writing tests**:

   - Tests should be placed in the `tests/` directory.
   - Test file names should start with `test_` and follow the same directory structure as the source code.
   - Test classes should follow the pattern `Test*` or `*Test`.
   - Use pytest fixtures for test setup/teardown.

3. **Coverage**:
   - The test suite reports coverage using the pytest-cov plugin.
   - Aim for high test coverage with meaningful tests.

### Type Checking

We use [ty](https://github.com/astral-sh/ty) for static type checking but we don't enforce it, we encourage you to use it to improve your code quality:

```bash
make lint-typing
```

## Making Contributions

### Branch Naming Convention

Use descriptive branch names that reflect the purpose of your changes:

- `feat/description` for new features
- `fix/description` for bug fixes
- `chore/description` for chores
- `security/description` for security fixes

### Commit Messages

Write clear and concise commit messages that explain what changes were made and why. Follow these guidelines:

- Use the present tense ("Add feature" not "Added feature")
- Use the imperative mood ("Move cursor to..." not "Moves cursor to...")
- Limit the first line to 72 characters or less
- Reference issues and pull requests where appropriate

### Pull Request Process

1. **Fork the repository** and create your branch from `main`
2. **Ensure code quality** by running `make lint`
3. **Ensure all tests pass** by running `make test`
4. **Update documentation** if necessary
5. **Submit a pull request** to the `main` branch
6. **Respond to feedback** from maintainers during the review process
7. **Update your PR** if requested with additional changes

## Reporting Issues

When reporting issues, please include as much information as possible:

1. **Steps to reproduce** the issue
2. **Expected behavior** and what actually happened
3. **Environment details**: Python version, OS, etc.
4. **Screenshots** if applicable
5. **Possible solutions** if you have suggestions

## License

By contributing to DAIV, you agree that your contributions will be licensed under the project's [Apache-2.0 license](LICENSE).

---

Thank you for contributing to DAIV! Your efforts help make this project better for everyone.
