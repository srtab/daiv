# DAIV - Agent instructions

The system is built on Django with Celery for async task processing, LangChain/LangGraph for LLM integration, and includes `daiv-sandbox` for commands execution.

## Project Structure

* `daiv/` - Django project with all the application code.
    * `daiv/automation/` - Automation module with all the agents logic and tools.
    * `daiv/codebase/` - Codebase module with all the repository interaction and related logic.
    * `daiv/chat/` - Chat module with the OpenAI compatible API.
    * `daiv/core/` - Core module with common logic.
    * `daiv/quick_actions/` - Quick actions module.
    * `daiv/daiv/` - Main logic of the Django project: settings, urls, wsgi, asgi, celery, etc.
* `docker/` - Dockerfiles and configurations for local and production deployments.
* `docs/` - Documentation for the project.
* `evals/` - Evaluation suite for the project (openevals + langsmith + pytest).
* `tests/` - Test suite for the project (pytest).

## Depedency Management

`uv` is used to manage dependencies. All the dependencies are defined in the `pyproject.toml` file.

```bash
uv sync --all-groups  # install all the dependencies from all the groups
uv sync --only-group=dev  # install only the dev dependencies
uv sync --only-group=docs  # install only the docs dependencies
uv lock  # update the lock file
```

To add, update or remove dependencies, use the native `uv add` or `uv remove` commands to ensure the lock file is always updated. Avoid editing the `pyproject.toml` file directly to install/update/remove dependencies.

## Testing

We use `pytest` to write the tests with `pytest-asyncio` for async tests. All the tests are located in the `tests/` directory. Add/update unit tests to cover new added code or changed code.

Do not try to run the tests directly, it will not work.

## Linting

We use `ruff` to lint and format the code. All the linting and formating rules are defined in the `pyproject.toml` file.

```bash
make lint  # lint check and format check
make lint-fix  # fix linting and formatting issues
```

We use `mypy` to type check the code. All the type checking rules are defined in the `pyproject.toml` file.

```bash
make lint-typing  # type check the code
```

## Documentation

We use `mkdocs` to build the documentation. All the documentation is located in the `docs/` directory. Add/Update documentation to cover new added feature or changes

## Changelog

All the changes are documented in the `CHANGELOG.md` file. Add/update the changelog when making changes to the code and follow the [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) format.
