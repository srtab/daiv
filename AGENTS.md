# Project Overview

DAIV is an AI-powered development assistant built on Django with Celery for async task processing, LangChain/LangGraph for LLM integration, and includes `daiv-sandbox` for sandboxed command execution. It integrates with GitLab and GitHub to automate issue resolution, code reviews, and CI/CD pipeline repairs.

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

## Build, Lint, and Test Commands

### Testing
```bash
make test                                      # run all unit tests with coverage
uv run pytest tests/automation/test_utils.py   # run a specific test file
uv run pytest tests/automation/test_utils.py::TestFileReadFunctions::test_register_file_read  # run a single test
uv run pytest tests/ -k "test_notes"           # run tests matching pattern
uv run pytest tests/ -v                        # verbose output
```

### Linting & Formatting
```bash
make lint-fix       # check and fix linting and formatting issues (recommended)
make lint           # check linting and formatting without fixing
make lint-check     # run ruff linter check only
make lint-format    # check code formatting only
make lint-typing    # run type checking with mypy
```

**IMPORTANT**: Always run `make lint-fix` instead of `make lint` to automatically fix issues. Only manually fix issues that cannot be auto-fixed by `make lint-fix`.

### Building Documentation
```bash
make docs-serve     # serve documentation locally at localhost:4000
```

## Code Style Guidelines

### Formatting
- Line length: 120 characters maximum
- Use double quotes for strings
- No trailing commas in function calls (skip-magic-trailing-comma enabled)
- Use type hints for all function parameters and return types

### Naming Conventions
- Functions/variables: `snake_case`
- Classes: `PascalCase`
- Constants: `UPPER_SNAKE_CASE`
- Private methods/attributes: prefix with single underscore `_private_method`
- Django models: use verbose names with `gettext_lazy` for i18n

### Type Annotations
- Always provide type hints for function parameters and return values
- Use `str | None` instead of `Optional[str]` (Python 3.10+ union syntax)
- Use `list[Type]` instead of `List[Type]` (built-in generics)
- Use `dict[str, Any]` instead of `Dict[str, Any]`
- Annotate class attributes in Django models when needed

Example:
```python
def commit_changes(
    self,
    commit_message: str,
    *,
    branch_name: str,
    skip_ci: bool = False,
) -> str:
    """Commit changes to the repository."""
    ...
```

### Docstrings
- Use Google-style docstrings for all public functions and classes
- Include Args, Returns, and Raises sections as needed
- Keep descriptions concise but informative

Example:
```python
def get_repo_ref(repo: Repo) -> str:
    """
    Get the current reference (branch name or commit SHA) from a repository.

    When HEAD is attached to a branch, returns the branch name.
    When HEAD is detached (e.g., checking out a specific commit), returns the commit SHA.

    Args:
        repo: The Git repository object.

    Returns:
        The branch name if HEAD is attached, or the commit SHA if HEAD is detached.
    """
```

### Error Handling
- Raise `ValueError` for invalid arguments
- Raise `RuntimeError` for operation failures
- Use `contextlib.suppress()` for expected, ignorable exceptions
- Log warnings with `logger.warning()` for non-critical issues
- Chain exceptions with `raise ... from e` to preserve context

Example:
```python
try:
    self.repo.git.apply(*diff_args, tmp_path)
except GitCommandError as e:
    raise RuntimeError("git apply failed. The patch is not valid.") from e
```

### Async Code
- Use `async`/`await` for async functions
- Test async functions with `pytest-asyncio` (asyncio_mode = "auto")
- Use `asyncio.gather()` for concurrent operations

## Dependency Management

Use `uv` to manage dependencies. All dependencies are defined in `pyproject.toml`.

**IMPORTANT**: Never edit `pyproject.toml` directly. Use `uv` commands and pin dependencies to exact versions with `==`.

```bash
uv sync --all-groups                    # install all dependencies
uv sync --only-group=dev                # install only dev dependencies
uv add <package>==<version>             # add a new dependency with exact version
uv remove <package>                     # remove a dependency
uv lock                                 # update the lock file
```

## Testing Guidelines

- Use `pytest` with `pytest-asyncio` for async tests
- Organize tests in classes prefixed with `Test`
- Use descriptive test names: `test_<what_it_does>`
- Use fixtures for common setup (e.g., `@pytest.fixture`)
- Mock external dependencies with `unittest.mock` or `pytest-mock`
- All tests should be in the `tests/` directory mirroring the `daiv/` structure

## Documentation

- Use `mkdocs` for documentation (Material theme)
- Documentation is located in `docs/` directory
- Add/update docs when adding new features or making significant changes
- Run `make docs-serve` to preview documentation locally

## Changelog

**ALWAYS** update `CHANGELOG.md` following [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) format:
- Use sections: Added, Changed, Deprecated, Removed, Fixed, Security
- Add entries under `[Unreleased]` section
- Include brief descriptions of changes

## Translations

Translations are in `locale/` directories with subdirectories for each language.

```bash
make makemessages       # generate translation files
# Edit *.po files with translations
make compilemessages    # compile translations
```

## Repository Conventions

### Branch Naming
Format: `<prefix>/<short-kebab-summary>`

Prefixes:
- `feat/` - new features (e.g., `feat/add-github-integration`)
- `fix/` - bug fixes (e.g., `fix/resolve-memory-leak`)
- `chore/` - maintenance tasks (e.g., `chore/update-dependencies`)

### Commit Messages
Follow [Conventional Commits](https://www.conventionalcommits.org/):
- Format: `<type>: <short summary>`
- Types: `feat`, `fix`, `chore`, `docs`, `refactor`, `test`, `style`, `perf`, `ci`, `build`
- Summary: lowercase, no period, max 72 characters

Examples:
- `feat: add gitlab webhook support`
- `fix: resolve race condition in task queue`
- `docs: update installation instructions`
