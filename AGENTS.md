# AGENTS.md

This file provides guidance to agents when working with code in this repository.

## Commands (verified)

```bash
make test          # unit tests + coverage; CI gate
make lint-fix       # check + fix lint/format in one step — prefer this over make lint
make lint           # check only (no fixes); CI gate
# NOTE: the djade step only checks *tracked* files (git ls-files). Run
#   uv run --only-group=dev djade --target-version 6.0 <new .html files>
# manually on newly created templates, or CI lint fails on files make lint never saw.
# NOTE: ruff check caches results (.ruff_cache) and can report "All checks passed!"
# on a changed file — before declaring a CI lint failure un-reproducible, run
#   rm -rf .ruff_cache && uv run --only-group=dev ruff check --no-cache .
make lint-typing    # ty, daiv/ only
make lint-imports   # app layering contracts in .importlinter; part of lint and lint-fix

# Single test / pattern
uv run pytest tests/unit_tests/accounts/test_views.py
uv run pytest tests/unit_tests/ -k "test_notes"

# Test layout — one test file per application file: tests for
# `accounts/adapter.py` go in `test_adapter.py`, `accounts/emails.py` in
# `test_emails.py`, `accounts/views.py` in `test_views.py`, etc. Don't create
# feature-named catch-all test files. Shared fixtures live in tests/unit_tests/conftest.py.

make integration-tests   # real LLM calls; needs docker/local/app/config.secrets.env (LLM key + GitLab creds). Runs -m "diff_to_metadata or memory"; DAIV_EVAL_REPEATS=1 for a fast local pass (not a gate).

# Translations
make makemessages && make compilemessages
```

- `asyncio_mode = "auto"` — no `@pytest.mark.asyncio` needed.
- Python **3.14 only** (`requires-python = ">=3.14,<3.15"`).
- Never edit `pyproject.toml` by hand — use `uv add <pkg>==<version>` / `uv remove <pkg>` (deps are `==`-pinned; `parable` is git-pinned, not on PyPI).

## Repo map (only what's non-obvious)

- `daiv/automation/` — LangGraph/deepagents agent: tools are provided by middlewares (`agent/middlewares/`, e.g. `web_fetch.py`, `artifacts.py`), skills (`agent/skills/`), deferred tools (`agent/deferred/`)
- `daiv/codebase/` — GitLab/GitHub clients (`clients/`), `.daiv.yml` repo config
- `daiv/webhooks/` — GitLab/GitHub webhook callbacks, the `address_*` tasks and the issue/MR addressors; the views register onto `codebase.api.router` in `WebhooksConfig.ready()`, so the URLs stay `/api/codebase/callbacks/{gitlab,github}`
- `daiv/mcp_api/` — DAIV's own MCP endpoint (`server.py`, mounted at `/mcp` by `daiv/daiv/asgi.py`) + its OAuth discovery and client registration; `daiv/mcp_connectors/` — the external MCP servers the agent connects to (`MCPServer`), app label still `mcp_servers` (tables, migrations, `apps.get_model`)
- `daiv/jobs/` — jobs REST API (`jobs/tasks.py` only re-exports `run_job_task` for task rows queued under the old path). `run_job_task` lives in `sessions/executor/tasks.py`; enqueued via `sessions.services.asubmit_batch_runs` (jobs API, MCP, schedules, dashboard), `sessions.signals` (a session's next queued run) and `sessions.pipeline_watch.dispatch` (CI fix runs). Chat runs inline; webhooks call the executor directly.
- `daiv/core/` — sandbox client, Redis, shared constants
- `daiv/configuration/` — the site-configuration UI (forms, views, templates); the data it edits (`SiteConfiguration`, `Provider`, `site_settings`) stays in `core` because lower layers read it
- `daiv/daiv/settings/components/` — split settings; `common.py` has `INSTALLED_APPS`
- `evals/` — eval suite, **not** run by `make test`
- UI rules live in `DESIGN.md` (icon system, layout tokens, floating surfaces) and `docs/` (mkdocs) — consult before touching templates/CSS.

## Invariants / footguns

- **Test imports** — `pythonpath = ["daiv", "."]`; in tests import `from automation.agent.graph import ...` (no `daiv.` prefix).
- **App layering** — `.importlinter` (run by `make lint`) forbids upward imports along `core` < `codebase`/`sandbox_envs` < `automation` < `sessions` < trigger apps (`chat`, `jobs`, `mcp_api`, `webhooks`), and forbids the trigger apps from importing each other, even indirectly. A broken contract means moving the code, not adding an `ignore_imports` line; a new line is a design decision for review. Deleting an import that an exception covers fails the run until its line goes too.
- **Tools can't mutate `runtime.state`** — return a `Command(update={...}, messages=[ToolMessage(...)])`; in tests unpack `isinstance(result, Command)`.
- **Auth** — email signup disabled (`AccountAdapter.is_open_for_signup` → `False`); `AdminRequiredMixin` needs `user.is_admin`; `APIKey.objects.create_key(...)` is **async**.
- **Run terms** — **Session** = one conversation (agent thread); its PK `thread_id` is also the LangGraph checkpoint key. **Run** = one agent execution inside a Session. **Origin** (`SessionOrigin`, `Session.origin`) = what started the Session; each Run records its own trigger in `Run.trigger_type`, so a webhook-origin Session can hold chat Runs.
- **`run_job_task` requires a non-empty `thread_id`** — it is the `Session` PK and the checkpoint key; missing breaks chat resume. The task checks only non-empty; the UUID check is in `sessions.services.asubmit_batch_runs`.
- **Bot labels** (`daiv` / `daiv-max` / `daiv-auto`) live in `daiv/core/constants.py` — don't hardcode.
- **Per-repo agent config** — agent reads `.agents/AGENTS.md`, skills from `.agents/skills/`, subagents from `.agents/subagents/`; a custom skill shadows a same-named built-in.
- **Disk vs sandbox** — agent code never branches on a run's mode: the executor builds a `DiskWorkspace` or `SandboxWorkspace` (`automation/agent/workspace/`), and each part asks it (see the `Workspace` protocol in `automation/agent/workspace/base.py`).
- **Repository memory** — `MemoryEntry` rows are append-only truth; `RepositoryMemory.content` is a render cache from `memory/render.py`, never model-generated; enforce `memory_max_*` via `prune_to_budget`, not by slicing.
- **Sandbox wire schemas** — `daiv/core/sandbox/schemas.dump.json` is canonical; `tests/unit_tests/core/sandbox/test_schema_consistency.py` fails on drift. Regenerate from the [daiv-sandbox](https://github.com/srtab/daiv-sandbox) repo after changing `daiv_sandbox/schemas.py`.
- **Skill asset paths** resolve to `<location>/<skill>/...`, not the bash CWD — invoke skill scripts by absolute path.
- **PEP 758** — `except E1, E2:` is valid; ruff canonicalises it unparenthesised — don't "fix" it back to parens (pure churn).
- **Django** — test settings module is `daiv.settings.test`; `NINJA_SKIP_REGISTRY=true` is auto-set in tests.
- **Views by content type** — HTML = CBVs in `daiv/<app>/views.py`; JSON = a django-ninja `Router` in `daiv/<app>/api/views.py` (or `api/router.py`), registered in `daiv/daiv/api.py`. Filtered lists use `django_filters.FilterSet` + `FilterView(strict=False)`, not hand-rolled `request.GET`/`Paginator`.
- **Dependency upgrade blockers** (re-verify first): **redis 8.x is now UNBLOCKED** — as of `redisvl 0.27.1` (transitive, via `langgraph-checkpoint-redis==0.5.2`, which caps `redis>=5.2.1`/`redisvl<1.0.0`) the redis cap is `redis<9.0,>=6.3.0,!=8.0.0`, so `redis 8.1.0` resolves; `uv lock --upgrade` bumps `redisvl 0.26.0 -> 0.27.1` automatically. The code is already forward-compatible: `daiv/core/redis.py` explicitly passes `socket_timeout=None` on the async client and documents the redis-py 8.0 default change, and no `setex`/`can_read_destructive`/`protocol=` usage exists (RESP3 default preserves legacy shapes). Safe to bump the direct `redis` pin. **mcp 2.x is adopted** (`mcp==2.2.0`): the MCP client is `langchain.mcp` (beta) + `fastmcp-slim[client]==4.0.10`, imported only in `automation/agent/mcp/client.py` (used by `automation/agent/mcp/toolkits.py` + `mcp_connectors/services.py`) — keep `fastmcp-slim` within `langchain`'s supported `fastmcp>=4.0.1,<5` range. DAIV's own server uses `mcp.server.mcpserver.MCPServer` (`mcp_api/server.py`); `stateless_http` / `transport_security` are `streamable_http_app(...)` arguments in `daiv/daiv/asgi.py`. Also: **django-tasks-db must stay at 0.12.0** — 0.13.0 removed its `django-tasks` dependency entirely (it now uses Django 6.1's built-in `django.tasks`), but the repo registers tasks via the third-party `django_tasks` package (`from django_tasks import task`, ~9 files) and `from django.tasks.backends.immediate import ImmediateBackend` (`core/backends/immediate.py`), so `DBTaskResult.task`'s `isinstance(task, Task)` would raise `SuspiciousOperation`; 0.13.0 is only safe after migrating the repo to `django.tasks` built-ins and dropping the `django-tasks` direct dep. Deps are `==`-pinned, so `uv lock --upgrade --dry-run` shows only transitive updates — check the PyPI JSON API per direct dep. Also BLOCKED: **`ag-ui-protocol` is pinned `<1`** in `pyproject.toml` (transitive cap, since `ag-ui-langgraph==0.0.45` declares only `ag-ui-protocol>=0.1.22` with no upper bound) — `uv lock --upgrade` would otherwise auto-bump it to the 1.x major, whose breaking changes (wire-protocol version bump, renamed message parts, dropped file sources, null-omission in serialization) are incompatible with both `ag-ui-langgraph==0.0.45` (targets 0.x) and the repo's tight coupling to `ag_ui.core.events`/`ag_ui.core` shapes in `chat/api/event_filter.py`, `chat/api/runner.py`, `chat/api/streaming.py`. Do **not** remove the cap until `ag-ui-langgraph` ships a 1.x-compatible release and those call sites are re-verified. Also: **deepagents is at 0.7.19** (was 0.7.13) — the 0.7.13→0.7.19 "patch" bumps removed the private `_remaining_lines_notice` helper (its prose notice was refactored into the structured `_window_fields` header fields + `_assemble_read`). The 3 `TestSandboxReadPagination` tests in `tests/unit_tests/automation/agent/middlewares/test_file_system.py` were ported to assert on `_window_fields(result)` (window span, total, resume offset) instead of the removed prose; the "exact remainder" is now verified as `total_lines - next_offset` rather than the old "N lines remaining from offset M" string (which upstream no longer emits). No production code imported the removed helper. Note: **patch releases can still break private-API consumers** — review the deepagents filesystem module before bumping it again.
- **Run artifacts** — `MEDIA_ROOT` must be a volume shared by the web and worker containers (the worker writes `RunArtifact` bytes, the web serves them); when adding viewer kinds keep `content_type` extension-derived and the raw endpoint's `sandbox` CSP. Every agent run carries its `Run` id (`RunSpec.run_id` / `bind_active_run`): webhook tasks take `takes_context=True` and resolve it with `aget_task_run_id`, because their callback creates the Run only after enqueueing. A new entry point must do the same — unbound code gets no run, never a guessed one.
- **Detailed behaviour lives in module docstrings**, not here — e.g. `automation/agent/chat_models.py`, `core/ui_events.py`, `chat/api/relay.py`, `sessions/pipeline_watch/`. Read the relevant docstring before changing that area.

## Where changes usually go

| Change type | Start here |
|---|---|
| New agent tool | a middleware in `daiv/automation/agent/middlewares/` exposing `self.tools` (see `web_fetch.py`, `artifacts.py`); registered in `graph.py` — deferred unless added to `ALWAYS_LOADED_TOOLS` |
| New built-in skill | `daiv/automation/agent/skills/<name>/` (`SKILL.md` + optional `scripts/`, `examples/`) |
| New agent middleware | `daiv/automation/agent/middlewares/` |
| MCP tool | `daiv/mcp_api/server.py` |
| Webhook event handling | `daiv/webhooks/<platform>/callbacks.py` |
| Shared settings / new app | `daiv/daiv/settings/components/common.py` (`LOCAL_APPS`); every new top-level package under `daiv/`, app or not, also goes in `.importlinter`: in `root_packages` and the `core` contract's `forbidden_modules` (`test_import_hygiene.py` checks both), and in the `forbidden_modules` of every other layer below it; a new trigger app also goes in the `triggers` contract's `modules` |
| LLM model list / provider | `daiv/automation/agent/base.py`, `daiv/core/constants.py` (`ModelName`) |
