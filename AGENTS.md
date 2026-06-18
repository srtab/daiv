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

**Import paths** — `pythonpath = "daiv"` in pytest config; imports are `from automation.agent.graph import ...` (no `daiv.` prefix inside tests).

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

**Dependency management** — pin to exact versions (`==`), never edit `pyproject.toml` by hand. `parable` is git-pinned (not on PyPI); do not install it independently.

**Repository config** — `.daiv.yml` per repo cached 1 hour (`codebase/repo_config.py`). Invalidate via `RepositoryConfig.invalidate_cache(repo_id)`.

**Bot labels** — `daiv` triggers agent, `daiv-max` uses max model, `daiv-auto` enables auto-addressing. Constants live in `daiv/core/constants.py`; do not hardcode the strings.

**Per-repo agent memory** — agent reads `.agents/AGENTS.md`; custom skills from `.agents/skills/`; subagents from `.agents/subagents/`. A custom skill with the same name as a built-in **shadows** the built-in (runtime + storage are consistent; the UI flags the card with "Overrides built-in"). The `code-review` skill additionally reads `.agents/review-rules.md` for per-repo review rules (with `AGENTS.md` as a secondary source).

**Django settings** — test module is `daiv.settings.test`; `NINJA_SKIP_REGISTRY=true` is injected automatically in tests.

**Python 3.14 except syntax (PEP 758)** — `except E1, E2:` is valid and equivalent to `except (E1, E2):`. Ruff canonicalises to the unparenthesised form, so do NOT "fix" it back to parens; both run, and rewriting is just churn.

**Sandbox wire schemas** — `daiv/core/sandbox/schemas.dump.json` is the canonical sandbox-side schema dump. The `tests/unit_tests/core/sandbox/test_schema_consistency.py` test will fail if the daiv-side schemas drift from it. Regenerate after any change to `daiv_sandbox/schemas.py` in the [daiv-sandbox](https://github.com/srtab/daiv-sandbox) repo:

```bash
# from a checkout of the daiv-sandbox repo
uv run --all-extras python scripts/dump_schemas.py \
    > /path/to/daiv/daiv/core/sandbox/schemas.dump.json
```

**`thread_id` contract** — callers of `run_job_task` must supply a non-empty UUID `thread_id`. The `Activity` row and LangGraph checkpointer share this key; a missing ID breaks chat resume.

**Skill asset paths** — inside a skill, paths like `scripts/foo.py` resolve to `<location>/<skill-name>/scripts/foo.py`, **not** the bash CWD (repo root). Always invoke skill scripts by absolute path. See `daiv/automation/agent/skills/code-review/scripts/marker.py` as the reference.

**Code-review detector output** — the `cr-*` detectors defer their `{"findings":[...]}` to `/workspace/tmp/subagent-output/<name>-<hash>.json` (via `DeferredOutputMiddleware`, added in `_build_detector_middleware`); the review orchestrator passes those paths to `scripts/findings.py merge` instead of re-typing the JSON. The detector charters are unaware of this — they still just return the structured object.

**Icons in templates** — never hand-roll an inline `<svg>` for a UI icon. Use `{% load icon_tags %}{% icon "name" "css-classes" %}`; see `DESIGN.md` §Icon System for the mechanism and the icon directory. Exceptions (keep inline): animated spinners, SVGs that need `<title>`/Alpine `:class` on the element itself, and brand/logo `<img>` tags.

**Views split by content type** — server-rendered HTML (dashboard pages, forms) lives in `daiv/<app>/views.py` as **CBVs** subclassing `View` / `TemplateView` / `ListView` / `UpdateView` with `LoginRequiredMixin` / `AdminRequiredMixin`. JSON endpoints (including those consumed by dashboard JS like autocompletes and the agent-picker catalog) live in `daiv/<app>/api/views.py` (or `api/router.py` — both names exist) as a **django-ninja `Router`** with `auth=django_auth` for session callers, registered on the central `NinjaAPI` in `daiv/daiv/api.py` (`api.add_router("/<app>", <app>_router)`). Set `url_name="..."` on each route and reverse via `{% url 'api:<route_name>' %}` from templates (or pass the URL into JS as an init prop instead of hardcoding `/api/...` paths); see `daiv/automation/api/views.py` + `_agent_picker.html` for the reference pair.

## Where changes usually go

| Change type | Start here |
|---|---|
| New agent tool | `daiv/automation/agent/tools/` |
| New built-in skill | `daiv/automation/agent/skills/<name>/` — add `SKILL.md` + optional `scripts/` and `examples/` |
| New agent middleware | `daiv/automation/agent/middlewares/` |
| Auth / user model | `daiv/accounts/models.py`, `daiv/accounts/views.py` |
| Git platform client | `daiv/codebase/clients/` |
| MCP tool | `daiv/mcp_server/server.py` |
| Shared settings / new app | `daiv/daiv/settings/components/common.py` (`LOCAL_APPS`) |
| New management command | `daiv/<app>/management/commands/` |
| LLM model list / provider | `daiv/automation/agent/base.py`, `daiv/automation/agent/constants.py` |
