# AGENTS.md

This file provides guidance to agents when working with code in this repository.

## Commands (verified)

```bash
make test                   # all unit tests with coverage
make lint-fix               # fix linting + formatting (always prefer over make lint)
make lint-typing            # type-check with ty
make lint                   # check only, no fixes

# Single test / pattern
uv run pytest tests/unit_tests/accounts/test_views.py
uv run pytest tests/unit_tests/ -k "test_notes"
```

- Unit tests live in `tests/unit_tests/` mirroring `daiv/` structure.
- `asyncio_mode = "auto"` — no need for `@pytest.mark.asyncio`.
- Never edit `pyproject.toml` directly; use `uv add <pkg>==<version>` / `uv remove <pkg>`.
- Translations: `make makemessages` → edit `.po` files → `make compilemessages`.

## Repo map

- `daiv/accounts/` — users, roles (`admin`/`member`), API keys, OAuth2/allauth adapters, dashboard
- `daiv/automation/` — LangGraph agent, skills (`automation/agent/skills/`), tools, subagents
- `daiv/codebase/` — GitLab/GitHub clients, webhook handling, repo config (`.daiv.yml`), merge metrics
- `daiv/core/` — sandbox client, caching helpers, shared constants (`BOT_NAME`, `BOT_LABEL`, etc.)
- `daiv/mcp_server/` — FastMCP ASGI sub-app; OAuth2 Bearer auth; `submit_job` / `get_job_status` tools
- `daiv/jobs/` — job submission task (`run_job_task`) consumed by MCP and webhook handlers
- `daiv/chat/` — OpenAI-compatible chat API
- `daiv/slash_commands/` — slash-command parsing and dispatch
- `daiv/daiv/settings/components/` — split-settings; `common.py` has `INSTALLED_APPS`
- `tests/unit_tests/`, `tests/integration_tests/` — pytest suites
- `evals/` — openevals + langsmith evaluation suite (separate from pytest)

## Invariants / footguns

**Tool state updates** — tools cannot mutate `runtime.state` directly; return a `Command`:
```python
from langgraph.types import Command
from langchain_core.messages import ToolMessage

return Command(update={"key": value, "messages": [ToolMessage(content=output, tool_call_id=runtime.tool_call_id)]})
```
In unit tests that call tools directly, check `isinstance(result, Command)` and unpack manually.

**Accounts / auth**
- Standard email signup is **disabled** (`AccountAdapter.is_open_for_signup` → `False`). Users are created by admins only.
- First social login (GitHub/GitLab) bootstraps the initial admin (`SocialAccountAdapter.save_user`).
- `AdminRequiredMixin` enforces `user.is_admin` on all admin-only CBVs; `user.is_last_active_admin()` guards deletion.
- `APIKey.objects.create_key(user, name, expires_at)` is **async** — use `async_to_sync` from sync contexts.

**Dependency management** — pin to exact versions (`==`), never edit `pyproject.toml` by hand.

**Repository config** — `.daiv.yml` per repo is cached for 1 hour (`codebase/repo_config.py`). Invalidate via `RepositoryConfig.invalidate_cache(repo_id)`.

**Bot labels** — `daiv` triggers the agent, `daiv-max` uses the max model (claude-opus), `daiv-auto` enables auto-addressing.

**Django settings** — split across `daiv/daiv/settings/components/`; the test module is `daiv.settings.test`.

**Python 3.14 except syntax (PEP 758)** — `except E1, E2:` is valid and equivalent to `except (E1, E2):`. Ruff canonicalises to the unparenthesised form, so do NOT "fix" it back to parens; both run, and rewriting is just churn.

**Sandbox wire schemas** — `daiv/core/sandbox/schemas.dump.json` is the canonical sandbox-side schema dump. The `tests/unit_tests/core/sandbox/test_schema_consistency.py` test will fail if the daiv-side schemas drift from it. Regenerate after any change to `daiv-sandbox/daiv_sandbox/schemas.py`:

```bash
cd ~/work/personal/daiv-sandbox && uv run --all-extras python scripts/dump_schemas.py \
    > ~/work/personal/daiv/daiv/core/sandbox/schemas.dump.json
```

## Where changes usually go

| Change type | Start here |
|---|---|
| New agent tool | `daiv/automation/agent/tools/` |
| New skill | `daiv/automation/agent/skills/<name>/` |
| Auth / user model | `daiv/accounts/models.py`, `daiv/accounts/views.py` |
| Git platform client | `daiv/codebase/clients/` |
| MCP tool | `daiv/mcp_server/server.py` |
| Shared settings | `daiv/daiv/settings/components/common.py` |
| New management command | `daiv/<app>/management/commands/` |
