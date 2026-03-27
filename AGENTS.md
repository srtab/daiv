# AGENTS.md

This file provides guidance to agents when working with code in this repository.

## Commands (verified)

```bash
make test                        # run all unit tests with coverage
make lint-fix                    # auto-fix linting + formatting (always prefer over make lint)
make lint-typing                 # type-check with ty (not mypy)
make docs-serve                  # preview docs at localhost:4000

# Targeted test runs
uv run pytest tests/unit_tests/path/to/test_file.py          # single file
uv run pytest tests/unit_tests/path/to/test_file.py::test_fn # single test
uv run pytest tests/unit_tests -k "pattern"                  # by keyword
```

- All unit tests live under `tests/unit_tests/`, mirroring the `daiv/` structure.
- Never edit `pyproject.toml` directly — use `uv add <pkg>==<version>` / `uv remove <pkg>`.

## Repo map (non-obvious)

- `daiv/automation/` — all agent logic: `agent/` (graph, skills, subagents, toolkits, middlewares, MCP/ACP)
- `daiv/codebase/` — Git platform clients (GitHub/GitLab), repo managers, code search, chunking
- `daiv/chat/` — OpenAI-compatible chat API
- `daiv/core/` — shared utilities, sandbox client, task backends
- `daiv/daiv/` — Django project core: settings, urls, wsgi/asgi, tasks
- `daiv/slash_commands/` — slash command handlers
- `evals/` — LangSmith + openevals evaluation suite (separate from `tests/`)
- `tests/unit_tests/` / `tests/integration_tests/` — pytest suites

## Invariants / footguns

- **Tool state updates**: Tools cannot mutate `runtime.state` directly. Return a `Command` object with a `ToolMessage` in `messages`:
  ```python
  return Command(update={"key": val, "messages": [ToolMessage(content=out, tool_call_id=runtime.tool_call_id)]})
  ```
- **Testing `Command` returns**: In unit tests, unwrap manually — check `isinstance(result, Command)` and extract `.update["messages"][0].content`.
- **Type checker is `ty`**, not mypy — `make lint-typing` runs `ty check daiv`.
- **Python 3.14** — use `str | None`, `list[T]`, `dict[K, V]` (no `Optional`, no `List`, no `Dict`).
- **Changelog**: Always update `CHANGELOG.md` after changes.
- **Commits**: Conventional Commits format — `feat:`, `fix:`, `chore:`, etc., lowercase, ≤72 chars.
- **Branches**: `feat/`, `fix/`, or `chore/` prefix + kebab summary.

## Where changes usually go

- New agent tools → `daiv/automation/agent/toolkits.py` or a new file under `daiv/automation/agent/`
- New skills → `daiv/automation/agent/skills/`
- Git platform logic → `daiv/codebase/clients/`
- Shared utilities → `daiv/core/`
