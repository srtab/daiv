# Project Overview

DAIV is an AI-powered development assistant built on Django with Django Tasks for async task processing, LangChain/LangGraph for LLM integration, and includes `daiv-sandbox` for sandboxed command execution. It integrates with GitLab and GitHub to automate issue resolution, code reviews, and CI/CD pipeline repairs.

## Project Structure

* `daiv/` - Django project with all the application code.
    * `automation/` - Automation module with all the agents logic and tools.
    * `codebase/` - Codebase module with all the repository interaction and related logic.
    * `chat/` - Chat module with the OpenAI compatible API.
    * `core/` - Core module with common logic.
    * `slash_commands/` - Slash commands module.
    * `daiv/` - Main logic of the Django project: settings, urls, wsgi, asgi, tasks, etc.
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
- Use `asyncio.gather()` for concurrent operations

## Tool State Updates

**CRITICAL**: Tools CANNOT directly modify `runtime.state`. To update state, tools must return a `Command` object from `langgraph.types`:

```python
from langgraph.types import Command
from langchain_core.messages import ToolMessage

@tool("my_tool")
async def my_tool(param: str, runtime: ToolRuntime) -> str | Command:
    # Simple case: just return output as string
    if not needs_state_update:
        return "output"
    
    # When state update is needed: return Command with ToolMessage
    output = "tool output"
    state_update = {
        "key": "value",
        "messages": [ToolMessage(content=output, tool_call_id=runtime.tool_call_id)]
    }
    return Command(update=state_update)
```

**Examples**:
- ❌ `runtime.state["key"] = "value"` - WRONG! Direct state modification doesn't work
- ❌ `return Command(update={"key": "value"}, resume=output)` - WRONG! Output should be in ToolMessage
- ✅ `return Command(update={"key": "value", "messages": [ToolMessage(content=output, tool_call_id=runtime.tool_call_id)]})` - CORRECT!

**Note**: When a tool needs to both return output and update state, the output MUST be included as a `ToolMessage` in the `messages` key of the state update. The `tool_call_id` must match `runtime.tool_call_id`.

**Testing Command returns**: In unit tests, when calling a tool directly (not through the agent framework), you must manually handle Command returns:
```python
result = await tool.coroutine(params, runtime=runtime)
if isinstance(result, Command):
    # Extract output from ToolMessage
    messages = result.update.get("messages", [])
    output = messages[0].content if messages else None
    
    # Apply state updates (excluding messages which are handled by framework)
    state_updates = {k: v for k, v in result.update.items() if k != "messages"}
    runtime.state.update(state_updates)
else:
    output = result
```

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
- Organize tests in functions.
- Use descriptive test names: `test_<what_it_does>`
- Use fixtures for common setup (e.g., `@pytest.fixture`)
- Mock external dependencies for unit tests with `unittest.mock` or `pytest-mock` or `pytest-httpx`.
- All unit tests should be in the `tests/unit_tests/` directory mirroring the `daiv/` structure
- All integration tests should be in the `tests/integration_tests/` directory.

## Documentation

- Use `mkdocs` for documentation (Material theme)
- Documentation is located in `docs/` directory
- Add/update docs when adding new features or making significant changes
- Run `make docs-serve` to preview documentation locally

## Changelog

**ALWAYS** update `CHANGELOG.md` after making changes to the project using the `changelog-curator` subagent.

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
