# AGENTS.md

This document provides agent-specific guidance for working with the DAIV codebase. It follows the standard format for agent documentation to help AI coding assistants understand the project structure, development workflow, and best practices.

## Project Overview

DAIV is an open-source AI automation assistant designed to enhance developer productivity by integrating seamlessly with GitLab repositories. It uses an agent-based architecture with specialized agents for different development tasks:

- **Issue Addressing**: Planning and executing solutions directly from issue titles and descriptions
- **Code Review Assistance**: Automatically responding to reviewer comments and adjusting code
- **Pipeline Repair**: Repairing failed CI/CD pipeline jobs by suggesting and applying fixes
- **Codebase Chat**: Providing context-aware conversations about the codebase
- **PR Description**: Generating comprehensive descriptions for merge requests

The system is built on Django with Celery for async task processing, LangChain/LangGraph for LLM integration, and includes secure sandbox execution for code changes.

## Setup Commands

### Dependency Installation
```bash
# Install all dependencies using uv (recommended)
uv sync --locked

# Install with development dependencies
uv sync --locked --only-group=dev
```

### Linting and Formatting
```bash
# Check for linting issues only
make lint-check

# Fix linting and formatting issues automatically
make lint-fix

# Check both linting and formatting (without fixing)
make lint

# Check code formatting only
make lint-format

# Run type checking (optional)
make lint-typing
```

### Development Environment
```bash
# Start all services with Docker Compose
docker compose up --build

# Access services:
# - DAIV API: https://localhost:8000/api/docs/
# - PGAdmin: http://localhost:8080
# - GitLab: http://localhost:8929
# - Sandbox: http://localhost:8888/docs
```

## Code Style Guidelines

DAIV enforces strict code formatting requirements:

### Ruff Configuration
- **Line length**: 120 characters
- **Linting**: Comprehensive rule set including flake8-builtins, flake8-bugbear, flake8-comprehensions, flake8-django, pycodestyle, eradicate, Pyflakes, isort, pep8-naming, flake8-use-pathlib, flake8-bandit, flake8-simplify, flake8-print, flake8-type-checking, and pyupgrade
- **Formatting**: Uses ruff format with skip-magic-trailing-comma enabled

### Import Sorting (isort)
Import sections are ordered as follows:
1. `future` - Future imports
2. `standard-library` - Python standard library
3. `django` - Django framework imports
4. `third-party` - External packages
5. `first-party` - Project modules (accounts, automation, chat, codebase, core)
6. `local-folder` - Local relative imports

### Additional Tools
- **pyproject-fmt**: Formats pyproject.toml files
- **mypy**: Type checking (encouraged but not enforced)
- **Target Python version**: 3.13

## Agent Architecture

DAIV implements several specialized agents:

### Plan and Execute Agent
- Analyzes issue descriptions and creates step-by-step implementation plans
- Executes approved plans by making code changes and creating merge requests
- Handles complex multi-file changes and dependency updates

### Review Addressor Agent
- Processes merge request comments and reviewer feedback
- Provides context-aware responses or applies requested changes directly
- Reduces back-and-forth communication in code reviews

### Pipeline Fixer Agent
- Analyzes failed CI/CD pipeline jobs
- Suggests fixes for common pipeline issues
- Can apply fixes automatically after human approval

### Codebase Chat Agent
- Provides contextual conversations about the codebase
- Offers an OpenAI-compatible API for integration with tools like Open-WebUI
- Maintains context awareness across conversations

### PR Describer Agent
- Generates comprehensive descriptions for merge requests
- Analyzes code changes and provides meaningful summaries
- Helps maintain clear documentation of changes

## MCP Tools Integration

DAIV supports Model Context Protocol (MCP) for extending agent capabilities:

- **MCP Servers**: Can be configured to provide additional tools to agents
- **Tool Extension**: Allows agents to access external services and APIs
- **Configurable Integration**: MCP servers can be set up based on project needs
- **Enhanced Capabilities**: Extends beyond built-in tools for specialized tasks

## Sandbox Integration

DAIV uses a secure sandbox environment for code execution:

### Configuration
- **Base Image**: `ghcr.io/astral-sh/uv:python3.13-bookworm-slim`
- **Secure Execution**: Isolated environment for running agent commands
- **Docker-based**: Containerized execution for security and consistency

### Format Code Commands
The sandbox includes predefined commands for code formatting:
```yaml
format_code:
  - "uv sync --locked --only-group=dev"
  - "uv run --only-group=dev ruff check . --fix"
  - "uv run --only-group=dev ruff format ."
  - "uv run --only-group=dev pyproject-fmt pyproject.toml"
```

### Capabilities
- Installing and updating dependencies
- Running linting and formatting tools
- Executing project-specific commands
- Generating translations and other build artifacts

## Repository Configuration

### .daiv.yml Configuration
The repository behavior is controlled by a `.daiv.yml` file in the default branch:

```yaml
pull_request:
  branch_name_convention: "Use 'feat/', 'fix/', or 'chore/' prefixes."

sandbox:
  base_image: "ghcr.io/astral-sh/uv:python3.13-bookworm-slim"
  format_code:
    - "uv sync --locked --only-group=dev"
    - "uv run --only-group=dev ruff check . --fix"
    - "uv run --only-group=dev ruff format ."
    - "uv run --only-group=dev pyproject-fmt pyproject.toml"
```

### Key Configuration Options
- **Branch Naming**: Enforces consistent branch naming conventions
- **Sandbox Settings**: Configures the execution environment
- **Format Commands**: Defines code formatting procedures

## Development Guidelines

### Branch Naming Convention
Use descriptive branch names with appropriate prefixes:
- `feat/description` - New features
- `fix/description` - Bug fixes  
- `chore/description` - Maintenance tasks
- `security/description` - Security fixes

### Commit Message Standards
- Use present tense ("Add feature" not "Added feature")
- Use imperative mood ("Move cursor to..." not "Moves cursor to...")
- Limit first line to 72 characters or less
- Reference issues and pull requests where appropriate

### CI/CD Pipeline Requirements
The GitLab CI pipeline includes:
- **Linting Stage**: Runs `make lint` to check code quality
- **Testing Stage**: Executes unit tests with coverage reporting
- **Docker Build**: Builds production Docker images
- **Interruptible Jobs**: Allows cancellation of running jobs

### Required Checks
All merge requests must pass:
- Linting checks (ruff, pyproject-fmt)
- Unit tests with coverage
- Docker build verification

## Security Considerations

### Database Connectivity
- Database connectivity is not available in testing environments
- Agents should handle database unavailability gracefully
- Use appropriate mocking for database-dependent tests

### Sandbox Security
- All agent code execution happens in isolated Docker containers
- Sandbox environment prevents access to host system
- Secure execution model protects against malicious code

### Secrets Management
- No secrets or credentials should be committed to the repository
- Use environment variables for sensitive configuration
- Follow secure coding practices for API integrations

## Quick Actions

DAIV provides a quick actions system for command-based interactions:

### Available Actions
- **Regenerate Plan**: Creates a new implementation plan for issues
- **Approve Plan**: Approves and executes a proposed plan
- **Repair Pipeline**: Fixes failed CI/CD pipeline jobs
- **Update Documentation**: Generates or updates project documentation
- **Format Code**: Applies code formatting across the repository

### Usage
Quick actions are triggered through:
- Issue comments with specific commands
- Merge request comments with action keywords
- GitLab webhook events for automated responses

### Integration
- Works with GitLab's webhook system
- Provides immediate feedback on action status
- Supports both manual and automated triggering

## Technology Stack

### Core Framework
- **Django 5.2.6**: Web framework and API development
- **Celery 5.5.3**: Async task processing with Redis
- **PostgreSQL**: Primary database with connection pooling

### AI/ML Integration
- **LangChain 0.3.27**: LLM framework with multiple provider support
- **LangGraph 0.6.6**: Agent workflow orchestration
- **LangSmith**: Tracing and monitoring for LLM interactions

### Development Tools
- **uv**: Fast Python package manager
- **ruff**: Linting and formatting
- **pytest**: Testing framework with coverage
- **mypy**: Optional type checking

### Supported LLM Providers
- OpenAI
- Anthropic
- Google Gemini
- OpenRouter

This documentation provides the essential information needed for AI coding agents to work effectively with the DAIV codebase. For additional details, refer to the README.md and CONTRIBUTING.md files.