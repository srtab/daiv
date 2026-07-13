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
- Python **3.14 only** (`requires-python = ">=3.14,<3.15>"`).
- Never edit `pyproject.toml` directly; use `uv add <pkg>==<version>` / `uv remove <pkg>`.

## Repo map

- `daiv/automation/` — LangGraph/deepagents agent; skills (`agent/skills/`), deferred tools (`agent/deferred/`), middleware stack (`agent/middlewares/`)
- `daiv/sessions/` — unified `Session` + `Run` model (successor to old `activity` + `chat` models; both are now historical stubs kept for migration history). `sessions/locks.py` is the unified execution lock (claim/heartbeat/release with stale takeover). Registered with Django label `agent_sessions`.
- `daiv/chat/` — AG-UI streaming layer (`chat/api/runner.py` `RunSupervisor`, `chat/api/relay.py` `RunRelay`); models live in `sessions/` now.
- `daiv/codebase/` — GitLab/GitHub clients, webhook handling, `.daiv.yml` repo config, per-user authorization (`codebase/authorization.py`)
- `daiv/mcp_server/` (singular) — FastMCP ASGI sub-app with `submit_job` / `get_job_status` tools. **`daiv/mcp_servers/` (plural) is a separate app** — admin UI + DB records for external MCP servers (not the FastMCP server itself).
- `daiv/sandbox_envs/` — per-repo sandbox environment presets (form, services, models). `daiv/core/sandbox/` is the sandbox client.
- `daiv/accounts/` — users, roles (`admin`/`member`), API keys, OAuth2/allauth adapters
- `daiv/jobs/` — thin `run_job_task` consumed by MCP and webhook handlers (claims the unified session lock with wait + heartbeat)
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

**Sessions / Runs** — `daiv.sessions.models.Run` UUIDs are preserved by the backfill migration from the old `activity.Activity` PKs (external job IDs keep resolving). `MemoryObservation.run` is a FK to `agent_sessions.Run` (not `Activity`); signals fire `run_finished` (not `activity_finished`) and chat triggers are intentionally **excluded** from memory observation mining.

**Per-user repository authorization** — every chat/job/MCP run goes through `codebase/authorization.py`: admins always pass; non-admins must have a linked `SocialAccount` and a fresh `RepositoryAccess` row (hard TTL = `REPO_ACCESS_HARD_TTL_HOURS`, default 24h). `assert_can_run` / `aassert_can_run` raise `RepositoryAccessDenied`; pickers use `viewable_repo_ids` / `search_viewable_repositories`. Tests not focused on authz use the `mock_repo_authorization` fixture (allow-all), but `search_viewable_repositories` is **not** patched — those tests must seed `RepositoryCatalog` rows.

**Git credentials** — never put the clone token in the URL. `RepoClient.get_git_auth_env` returns a frozen `GitAuthEnv` (in `codebase/clients/base.py`) that carries a command-scoped `http.<origin>.extraheader` via `GIT_CONFIG_*` env vars; `.as_env()` materialises plaintext only at the subprocess boundary (so it never appears in repr/log/Sentry). All clone/push code paths must go through `get_git_auth_env`; never write credentials into `.git/config` or argv. `GIT_TERMINAL_PROMPT=0` + empty `GIT_ASKPASS` must stay set so a rejected credential fails fast (the clone-retry self-heal relies on the "could not read Username" marker). GitHub installation tokens are scoped to the single run repo (`repository_ids=[repo.pk]`).

**Chat run streaming** — chat runs execute detached (`chat/api/runner.py` `RunSupervisor` spawns an asyncio task) and publish AG-UI events to the Redis stream `daiv:chat:run-events:<thread_id>:<run_id>` (`chat/api/relay.py` `RunRelay`). SSE readers replay via `Last-Event-ID`; the terminal sentinel entry is `{"end": "1"}`. Client disconnects never stop a run — stopping is the explicit `POST /api/chat/cancel` (Redis cancel flag + local `asyncio.Task` cancel). Resumability requires a non-empty UUID `thread_id` shared by the `Session` row and LangGraph checkpointer. Never revert to running the agent inside a `StreamingHttpResponse` generator.

**Sandbox connection pool** — `daiv/core/sandbox/client.py` caps the run-scoped `httpx.AsyncClient` at `SANDBOX_CONNECTION_LIMITS = httpx.Limits(max_connections=32, max_keepalive_connections=16)`. Long runs combined with LLM/git/Redis pools hit container fd limits (`[Errno 24] Too many open files`) without this cap. `DAIVSandboxClient.open()` rejects double-open; `set_runtime_ctx()` in `daiv/codebase/context.py` opens one client per run and guarantees `close()` + contextvar reset in a `finally` block. Deployment needs `nofile` raised (Docker Swarm: `/etc/docker/daemon.json` `default-ulimits`; Compose: per-service `ulimits`) — see `docs/getting-started/deployment.md`.

**Schedule dispatch** — `dispatch_scheduled_jobs_cron_task` filters with a `User.objects.filter(is_active=True).values("pk")` subquery (not a join) so `FOR UPDATE SKIP LOCKED` only locks `ScheduledJob`. Inactive-owner schedules are **skipped** — `next_run_at` stays untouched so they auto-resume on reactivation.

**Accounts / auth** — standard email signup is **disabled** (`AccountAdapter.is_open_for_signup` → `False`). `AdminRequiredMixin` enforces `user.is_admin`; `user.is_last_active_admin()` guards deletion. `APIKey.objects.create_key(user, name, expires_at)` is **async** — use `async_to_sync` from sync contexts.

**Dependency management** — pin to exact versions (`==`), never edit `pyproject.toml` by hand. `parable` is git-pinned (not on PyPI); do not install it independently.

**Repository config** — `.daiv.yml` per repo cached 1 hour (`codebase/repo_config.py`). Invalidate via `RepositoryConfig.invalidate_cache(repo_id)`.

**Bot labels** — `daiv` triggers agent, `daiv-max` uses max model, `daiv-auto` enables auto-addressing. Constants live in `daiv/core/constants.py`; do not hardcode the strings.

**Per-repo agent memory** — agent reads `.agents/AGENTS.md`; custom skills from `.agents/skills/`; subagents from `.agents/subagents/`. A custom skill with the same name as a built-in **shadows** the built-in (runtime + storage are consistent; the UI flags the card with "Overrides built-in"). The `code-review` skill additionally reads `.agents/review-rules.md` for per-repo review rules (with `AGENTS.md` as a secondary source).

**Django settings** — test module is `daiv.settings.test`; `NINJA_SKIP_REGISTRY=true` is injected automatically in tests.

**Python 3.14 except syntax (PEP 758)** — `except E1, E2:` is valid and equivalent to `except (E1, E2):`. Ruff canonicalises to the unparenthesised form, so do NOT "fix" it back to parens; both run, and rewriting is just churn.

**Sandbox wire schemas** — `daiv/core/sandbox/schemas.dump.json` is the canonical sandbox-side schema dump. `tests/unit_tests/core/sandbox/test_schema_consistency.py` fails if daiv-side schemas drift from it. Regenerate after any change to `daiv_sandbox/schemas.py` in the [daiv-sandbox](https://github.com/srtab/daiv-sandbox) repo:
```bash
# from a checkout of the daiv-sandbox repo
uv run --all-extras python scripts/dump_schemas.py \
    > /path/to/daiv/daiv/core/sandbox/schemas.dump.json
```

**Skill asset paths** — inside a skill, paths like `scripts/foo.py` resolve to `<location>/<skill-name>/scripts/foo.py`, **not** the bash CWD (repo root). Always invoke skill scripts by absolute path. See `daiv/automation/agent/skills/code-review/scripts/marker.py` as the reference.

**Code-review detector output** — `cr-*` detectors defer their `{"findings":[...]}` to `/workspace/tmp/subagent-output/<name>-<hash>.json` (via `DeferredOutputMiddleware`); the review orchestrator passes those paths to `scripts/findings.py merge`. A detector with no structured response defers a `.txt` error file instead, which `findings.py merge` counts as a `skipped`/failed detector, never as empty findings.

**Icons in templates** — never hand-roll an inline `<svg>` for a UI icon. Use `{% load icon_tags %}{% icon "name" "css-classes" %}` (see `DESIGN.md` §Icon System). Exceptions: animated spinners, SVGs needing `<title>`/Alpine `:class` on the element itself, and brand/logo `<img>` tags.

**Views split by content type** — server-rendered HTML lives in `daiv/<app>/views.py` as **CBVs** subclassing `View` / `TemplateView` / `ListView` / `UpdateView` with `LoginRequiredMixin` / `AdminRequiredMixin`. JSON endpoints live in `daiv/<app>/api/views.py` (or `api/router.py` — both names exist) as a **django-ninja `Router`** with `auth=django_auth` for session callers, registered on the central `NinjaAPI` in `daiv/daiv/api.py` (`api.add_router("/<app>", <app>_router)`). Set `url_name="..."` on each route and reverse via `{% url 'api:<route_name>' %}` from templates; reference pair: `daiv/automation/api/views.py` + `_agent_picker.html`.

**Filtered list views** — do **not** hand-roll `request.GET` parsing or a manual `Paginator` in a `TemplateView`. Declare a `django_filters.FilterSet` in `daiv/<app>/filters.py` (filters declared explicitly, `Meta.fields = []` to disable auto-generation) and use a `FilterView` with `filterset_class`, `paginate_by`, and `strict = False`. Echo selected values via `cleaned = ctx["filter"].form.cleaned_data if ctx["filter"].form.is_valid() else {}`. Reference pairs: `daiv/accounts/filters.py` + `UserListView`, `daiv/memory/filters.py` + `MemoryDetailView` (hybrid detail — a `FilterView` over a sub-list; overrides `get()` to stash the parent + unfiltered total before `super().get()`). **Exception:** a list whose rows are a union/merge of multiple tables (e.g. `MemoryListView` — `MemoryObservation` aggregates + `RepositoryMemory` rows) legitimately stays a `TemplateView`.

## Where changes usually go

| Change type | Start here |
|---|---|
| New agent tool / skill / middleware | `daiv/automation/agent/{tools,skills,middlewares}/` |
| Session/Run model, view, or filter | `daiv/sessions/` (see "Sessions / Runs" above) |
| Per-repo authz | `daiv/codebase/authorization.py` + enforcement call site |
| Auth / user model | `daiv/accounts/models.py`, `daiv/accounts/views.py` |
| Git platform client | `daiv/codebase/clients/` (use `GitAuthEnv` for credentials) |
| MCP FastMCP tool (singular) | `daiv/mcp_server/server.py` |
| External MCP server record (plural) | `daiv/mcp_servers/` admin UI |
| Sandbox env preset | `daiv/sandbox_envs/` |
| Shared settings / new app | `daiv/daiv/settings/components/common.py` (`LOCAL_APPS`) |
| New management command | `daiv/<app>/management/commands/` |
| LLM model list / provider | `daiv/automation/agent/base.py`, `daiv/automation/agent/constants.py` |
