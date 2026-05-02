# AGENTS.md

This file provides guidance to agents when working with code in this repository.

## Commands (verified)

```bash
make test                   # all unit tests with coverage (sets LANGCHAIN_TRACING_V2=false)
make lint-fix               # fix linting + formatting (ruff + pyproject-fmt; prefer over make lint)
make lint-typing            # type-check with ty (daiv/ only, excludes tests/)
make lint                   # check only, no fixes

# Single test / pattern
uv run pytest tests/unit_tests/accounts/test_views.py
uv run pytest tests/unit_tests/ -k "test_notes"

# Integration tests (separate from unit tests; requires DB)
make integration-tests      # runs with -m diff_to_metadata marker

# Translations
make makemessages && make compilemessages
```

- Unit tests live in `tests/unit_tests/` mirroring `daiv/` structure.
- `asyncio_mode = "auto"` — no need for `@pytest.mark.asyncio`.
- Python **3.14 only** (`requires-python = ">=3.14,<3.15"`).
- Never edit `pyproject.toml` directly; use `uv add <pkg>==<version>` / `uv remove <pkg>`.

## Repo map

- `daiv/automation/` — LangGraph/deepagents agent, skills (`agent/skills/`), deferred tools (`agent/deferred/`), middleware stack (`agent/middlewares/`)
- `daiv/codebase/` — GitLab/GitHub clients (`clients/`), webhook handling, `.daiv.yml` repo config
- `daiv/accounts/` — users, roles (`admin`/`member`), API keys, OAuth2/allauth adapters
- `daiv/mcp_server/` — FastMCP ASGI sub-app; OAuth2 Bearer auth; `submit_job` / `get_job_status` tools
- `daiv/jobs/` — thin `run_job_task` consumed by MCP and webhook handlers
- `daiv/core/` — sandbox client, caching helpers, shared constants (`BOT_NAME`, `BOT_LABEL`, etc.)
- `daiv/chat/` — OpenAI-compatible chat API; `daiv/slash_commands/` — slash-command parsing
- `daiv/daiv/settings/components/` — split-settings; `common.py` has `INSTALLED_APPS`
- `evals/` — openevals + langsmith evaluation suite (**not** run by `make test`)

## Invariants / footguns

**Tool state updates** — tools cannot mutate `runtime.state` directly; return a `Command`:
```python
from langgraph.types import Command
from langchain_core.messages import ToolMessage

return Command(update={"key": value, "messages": [ToolMessage(content=output, tool_call_id=runtime.tool_call_id)]})
```
In unit tests that call tools directly, check `isinstance(result, Command)` and unpack manually.

**Accounts / auth**
- Standard email signup is **disabled** (`AccountAdapter.is_open_for_signup` → `False`).
- `AdminRequiredMixin` enforces `user.is_admin`; `user.is_last_active_admin()` guards deletion.
- `APIKey.objects.create_key(user, name, expires_at)` is **async** — use `async_to_sync` from sync contexts.

**Dependency management** — pin to exact versions (`==`), never edit `pyproject.toml` by hand.

**Repository config** — `.daiv.yml` per repo cached 1 hour (`codebase/repo_config.py`). Invalidate via `RepositoryConfig.invalidate_cache(repo_id)`.

**Bot labels** — `daiv` triggers agent, `daiv-max` uses max model, `daiv-auto` enables auto-addressing.

**Per-repo agent memory** — agent reads `.agents/AGENTS.md`; custom skills from `.agents/skills/`; subagents from `.agents/subagents/`.

**Django settings** — test module is `daiv.settings.test`; `NINJA_SKIP_REGISTRY=true` is injected automatically in tests.

## Where changes usually go

| Change type | Start here |
|---|---|
| New agent tool | `daiv/automation/agent/tools/` |
| New built-in skill | `daiv/automation/agent/skills/<name>/` |
| New agent middleware | `daiv/automation/agent/middlewares/` |
| Auth / user model | `daiv/accounts/models.py`, `daiv/accounts/views.py` |
| Git platform client | `daiv/codebase/clients/` |
| MCP tool | `daiv/mcp_server/server.py` |
| Shared settings | `daiv/daiv/settings/components/common.py` |
| New management command | `daiv/<app>/management/commands/` |
