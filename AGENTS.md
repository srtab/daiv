# Project Overview

The system is built on Django with Celery for async task processing, LangChain/LangGraph for LLM integration, and includes `daiv-sandbox` for sandboxed commands execution.

## Project Structure

* `daiv/` - Django project with all the application code.
    * `automation/` - Automation module with all the agents logic and tools.
    * `codebase/` - Codebase module with all the repository interaction and related logic.
    * `chat/` - Chat module with the OpenAI compatible API.
    * `core/` - Core module with common logic.
    * `quick_actions/` - Quick actions module.
    * `daiv/` - Main logic of the Django project: settings, urls, wsgi, asgi, celery, etc.
* `docker/` - Dockerfiles and configurations for local and production deployments.
* `docs/` - Documentation for the project.
* `evals/` - Evaluation suite for the project (openevals + langsmith + pytest).
* `tests/` - Test suite for the project (pytest).

## Depedency Management

Use `uv` to manage dependencies. All the dependencies are defined in the `pyproject.toml` file.

```bash
uv sync --all-groups  # install all the dependencies from all the groups
uv sync --only-group=dev  # install only the dev dependencies
uv sync --only-group=docs  # install only the docs dependencies
uv lock  # update the lock file
```

Avoid editing `pyproject.toml` directly to manage dependencies. Use the native `uv` commands (`uv add <package>`, `uv remove <package>`, `uv lock`, etc.) to add, remove, or update dependencies, and to update the lock file.

## Testing

The recommended way to write tests is to use `pytest` with `pytest-asyncio` for async tests. All the tests are located in the `tests/` directory. Add/update unit tests to ensure the changes are working as expected.

To run the unit tests:
```bash
make test  # run all the unit tests

uv run pytest tests/automation/test_utils.py  # run a specific test
```

## Linting

The tool used to lint and format the code is `ruff`. All the linting and formating rules are defined in the `pyproject.toml` file.

To lint and format the code:

```bash
make lint-fix  # lint check and format the code. Try to automatically fix the linting and formatting issues found in the code.
```

## Documentation

The tool used to build the documentation is `mkdocs`. All the documentation is located in the `docs/` directory. Add/Update documentation to cover new added features or changes.

## Changelog

All the changes made to the codebase should be documented in the `CHANGELOG.md` file by following the [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) format.

## Translations

To generate the translations, use the `make makemessages` command. Compile the translations after changing `*.po` files, use the `make compilemessages` command. All the translations are located in a `locale/` directory, with a subdirectory for each language.

```bash
make makemessages  # generate the translations
make compilemessages  # compile the translations
```

## Repository Conventions

### Branch Naming

Use the following prefixes for branch names:
- `feat/` - for new features
- `fix/` - for bug fixes
- `chore/` - for maintenance tasks

Format: `<prefix>/<short-kebab-summary>`

Example: `feat/add-user-auth`, `fix/resolve-memory-leak`, `chore/update-dependencies`

### Commit Messages

Follow the [Conventional Commits](https://www.conventionalcommits.org/) format:
- Format: `<type>: <short summary>`
- Types: `feat`, `fix`, `chore`, `docs`, `refactor`, `test`, `style`, `perf`, `ci`, `build`
- Summary should be lowercase, no period at the end, max 72 characters

Examples:
- `feat: add user authentication`
- `fix: resolve memory leak in worker process`
- `docs: update installation instructions`
