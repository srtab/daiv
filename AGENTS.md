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
- Python **3.14 only** (`requires-python = ">=3.14,<3.15"`). Ask uv for the specifier, never the bare `3.14` family: the family matches `3.14.0rc2`, on which pydantic cannot build a single model, so every test errors at collection. `.claude/hooks/session-start.sh` pins this for web sessions.
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

**Chat run streaming** — chat runs execute detached from the HTTP request (`chat/api/runner.py` spawns an asyncio task) and publish AG-UI events to the Redis stream `daiv:chat:run-events:<thread_id>:<run_id>` (`chat/api/relay.py`). SSE readers replay via `Last-Event-ID`; the terminal sentinel entry is `{"end": "1"}`. Client disconnects never stop a run — stopping is the explicit `POST /api/chat/cancel` (Redis flag checked at the next event boundary once the heartbeat interval elapses, plus an immediate local `asyncio.Task` cancel for the in-process run). Never revert to running the agent inside a `StreamingHttpResponse` generator.

**Chat composer anatomy** — one anatomy at every width: a sticky context row (repo · branch · MR pill) above the composer box, then a single action row (options `+`, progress pill, plain-text model label, icon-only Send/Stop). Everything else lives in a sheet — bottom sheet below 1100px, popover anchored above the trigger at ≥1100px; the model picker follows the same rule, via `.picker-popover` (see **Pickers** below). The mobile `.chat-summary` strip and the desktop `.chat-rail` are gone; do not reintroduce either. The progress pill is suppressed on **content** (no todos, no files, no diff → no trigger at all), and its `+/−` numbers come from `GitState.diff_stats`, measured by `GitChangePublisher` with `diff_line_stats` over the snapshot diff it already holds — never from the platform (`test_never_asks_the_platform_for_the_numbers` pins this), and never summed client-side from `editDiffCounts`, which double-counts repeat edits and scores `bash` edits as zero. The live agent/env pickers in the composer are `x-if`'d on `!thread`: left mounted they seed hidden inputs with the site default and get the next turn 409'd by the first-turn pin.

**Per-thread MCP servers** — `MCPServer.status` is tri-state (`active` / `on-demand` / `disabled`); there is no `enabled` boolean. `deduped_pool_rows` is the pool (everything not `disabled`, globals shadowing same-named user rows), `is_default` marks the `active` subset, and `build_runtime_servers` loads the defaults with `Session.mcp_overrides` (`{name: "on"|"off"}`) applied. Stored values are **pool-relative diffs, never name sets**: `diff_selection` writes only deviations and `effective_selection` reads them back, so an admin's status flip re-resolves instead of freezing the thread, and a stale `"on"` for a deleted server self-heals. `disabled` is absolute — an override can never re-enable one. Unhealthy servers stay *in* a selection (dropping them would evict them from the thread permanently once the outage cleared) and render inert rather than unchecked. Unlike the model and the env the selection is **not** pinned on the first turn: any turn may retune it, and a *retune* lands *after* `SessionLock.try_claim` so a rejected duplicate leaves no memory of a turn that never ran (a first turn's selection is persisted with the session row, on the same terms as the model and the env). Bounds live in `parse_server_names` (shared by the chat endpoint and `MCPSelectionField`), and the shadow warning fires only on the runtime path (`warn_shadowed=True`) — the display paths run per page render.

**Bot labels** — `daiv` triggers agent, `daiv-max` uses max model, `daiv-auto` enables auto-addressing. Constants live in `daiv/core/constants.py`; do not hardcode the strings.

**Per-repo agent memory** — agent reads `.agents/AGENTS.md`; custom skills from `.agents/skills/`; subagents from `.agents/subagents/`. A custom skill with the same name as a built-in **shadows** the built-in (runtime + storage are consistent; the UI flags the card with "Overrides built-in"). The `code-review` skill additionally reads `.agents/review-rules.md` for per-repo review rules (with `AGENTS.md` as a secondary source).

**Repository memory ("dreaming")** — `MemoryEntry` rows are the source of truth; `RepositoryMemory.content` is a render cache produced by `render_memory_document` (`memory/render.py`), never model-generated. Entries are append-only — `entry.supersede(successor)` / `entry.confirm(when)`, never in-place edits or deletes — and `memory_max_lines`/`memory_max_bytes` are enforced by `prune_to_budget`, never by slicing the rendered document. The structured-output schemas (`memory/schemas.py`) deliberately carry **no** pydantic length or size constraints: parsing is all-or-nothing, so one over-long field would discard a whole valid batch; limits are checked per item instead.

**Django settings** — test module is `daiv.settings.test`; `NINJA_SKIP_REGISTRY=true` is injected automatically in tests.

**Python 3.14 except syntax (PEP 758)** — `except E1, E2:` is valid and equivalent to `except (E1, E2):`. Ruff canonicalises to the unparenthesised form, so do NOT "fix" it back to parens; both run, and rewriting is just churn.

**Sandbox wire schemas** — `daiv/core/sandbox/schemas.dump.json` is the canonical sandbox-side schema dump. The `tests/unit_tests/core/sandbox/test_schema_consistency.py` test will fail if the daiv-side schemas drift from it. Regenerate after any change to `daiv_sandbox/schemas.py` in the [daiv-sandbox](https://github.com/srtab/daiv-sandbox) repo:

```bash
# from a checkout of the daiv-sandbox repo (PYTHONPATH is required — that repo declares no build
# backend, so `uv run` never puts `daiv_sandbox` on sys.path)
PYTHONPATH=. uv run --all-extras python scripts/dump_schemas.py \
    > /path/to/daiv/daiv/core/sandbox/schemas.dump.json
```

A type the sandbox deletes disappears from the dump; if the daiv side still models it, delete the
dead model rather than adding an exemption to that test. Note the comparison normalizes
`title`/`description` away — field docstrings are *not* pinned and can silently drift from the
sandbox's own wording.

**MCP tool-load degradation** — `_load_server_tools` (`daiv/automation/agent/mcp/toolkits.py`) degrades to `[]` and never raises. Anticipated/transient failures log at **WARNING** (no traceback) so an external outage doesn't mint a Sentry error event per agent run; only genuinely unexpected errors fall through to `logger.exception` (ERROR + traceback). The canonical transient set lives in `_is_transient_mcp_error` (the authority — don't re-enumerate it here): a bare `TimeoutError` is caught earlier by its own `except TimeoutError:` clause, and the classifier covers upstream 5xx (`httpx.HTTPStatusError`), a broken MCP stream (`anyio.BrokenResourceError`, when anyio is importable), and a `BaseExceptionGroup` whose leaves are *all* transient (how anyio surfaces TaskGroup teardown). `CancelledError` is a `BaseException` → a group containing it is a `BaseExceptionGroup` that `except Exception` never catches, so outer cancellation keeps propagating; don't "fix" this.

**`thread_id` contract** — callers of `run_job_task` must supply a non-empty UUID `thread_id`. The `Activity` row and LangGraph checkpointer share this key; a missing ID breaks chat resume.

**Skill asset paths** — inside a skill, paths like `scripts/foo.py` resolve to `<location>/<skill-name>/scripts/foo.py`, **not** the bash CWD (repo root). Always invoke skill scripts by absolute path. See `daiv/automation/agent/skills/skill-creator/scripts/init_skill.py` as the reference.

**Floating surfaces** — popovers, sheets and dropdowns all rise into place on open, via the shared `surface-rise` keyframe. The roster of surface classes is one grouped rule at the top of `input.css`; `.surface-rise` is the hook for the dropdowns built from utilities alone. Put new surfaces on that roster instead of hand-rolling motion, tune travel with `--surface-rise-from` (sheets come from further out than a menu), and never pair it with Alpine's `x-transition` — both drive `opacity`/`transform` and fight over the same frames. Side drawers (`_env_drawer.html`, `_template_gallery.html`, `_env_paste_overlay.html`) keep `x-transition` on purpose: they slide in horizontally and animate on leave too. **Pickers** — every `.picker-popover` (model, repo, branch, env, subscriber) is an anchored popover at ≥1100px and a **bottom sheet below it**, on every screen that hosts one — a trigger anywhere but hard left puts a 320px panel over the right edge of a phone, and no anchor tweak covers every case. Geometry belongs in CSS (`.picker-popover`, `--wide`, `--inset`), never as `left-*`/`w-*` utilities on the element: Tailwind's utilities layer outranks every component rule regardless of selector, so a utility anchor survives into widths where it doesn't fit — that is what put the model popover off the right edge of a phone. Every popover includes `core/_picker_sheet_head.html` (title + dismiss, `display: none` above 1100px) because a sheet covers the trigger that would otherwise dismiss it; pass `close_expr` since pickers name their close differently.

**Icons in templates** — never hand-roll an inline `<svg>` for a UI icon. Use `{% load icon_tags %}{% icon "name" "css-classes" %}`; see `DESIGN.md` §Icon System for the mechanism and the icon directory. Exceptions (keep inline): animated spinners, SVGs that need `<title>`/Alpine `:class` on the element itself, and brand/logo `<img>` tags.

**Views split by content type** — server-rendered HTML (dashboard pages, forms) lives in `daiv/<app>/views.py` as **CBVs** subclassing `View` / `TemplateView` / `ListView` / `UpdateView` with `LoginRequiredMixin` / `AdminRequiredMixin`. JSON endpoints (including those consumed by dashboard JS like autocompletes and the agent-picker catalog) live in `daiv/<app>/api/views.py` (or `api/router.py` — both names exist) as a **django-ninja `Router`** with `auth=django_auth` for session callers, registered on the central `NinjaAPI` in `daiv/daiv/api.py` (`api.add_router("/<app>", <app>_router)`). Set `url_name="..."` on each route and reverse via `{% url 'api:<route_name>' %}` from templates (or pass the URL into JS as an init prop instead of hardcoding `/api/...` paths); see `daiv/automation/api/views.py` + `_agent_picker.html` for the reference pair.

**Filtered list views** — do **not** hand-roll `request.GET` parsing or a manual `Paginator` in a `TemplateView`. Declare a `django_filters.FilterSet` in `daiv/<app>/filters.py` (filters declared explicitly, `Meta.fields = []` to disable auto-generation) and use a `FilterView` with `filterset_class`, `paginate_by`, and `strict = False` (an invalid param like `?status=bogus` then drops that filter instead of blanking the list). Echo selected values back to the template via `cleaned = ctx["filter"].form.cleaned_data if ctx["filter"].form.is_valid() else {}`. Reference pairs: `daiv/activity/filters.py` + `ActivityListView`, `daiv/accounts/filters.py` + `UserListView`, `daiv/memory/filters.py` + `MemoryDetailView` (a hybrid detail page that is a `FilterView` over a sub-list — it overrides `get()` to stash the parent object + unfiltered total for the 404 guard before `super().get()`). **Exception:** a list whose rows are a union/merge of multiple tables rather than a single-model queryset legitimately stays a `TemplateView` (e.g. `MemoryListView`, which unions `MemoryObservation` aggregates with `RepositoryMemory` rows) — `ListView`/`FilterView` only fit a single base queryset.

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
