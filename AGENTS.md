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

**Chat run streaming** — chat runs execute detached from the HTTP request (`chat/api/runner.py` spawns an asyncio task) and publish AG-UI events to the Redis stream `daiv:chat:run-events:<thread_id>:<run_id>` (`chat/api/relay.py`). SSE readers replay via `Last-Event-ID`; the terminal sentinel entry is `{"end": "1"}`. Client disconnects never stop a run — stopping is the explicit `POST /api/chat/cancel` (Redis flag checked at the next event boundary once the heartbeat interval elapses, plus an immediate local `asyncio.Task` cancel for the in-process run). Never revert to running the agent inside a `StreamingHttpResponse` generator.

**Bot labels** — `daiv` triggers agent, `daiv-max` uses max model, `daiv-auto` enables auto-addressing. Constants live in `daiv/core/constants.py`; do not hardcode the strings.

**Per-repo agent memory** — agent reads `.agents/AGENTS.md`; custom skills from `.agents/skills/`; subagents from `.agents/subagents/`. A custom skill with the same name as a built-in **shadows** the built-in (runtime + storage are consistent; the UI flags the card with "Overrides built-in"). The `code-review` skill additionally reads `.agents/review-rules.md` for per-repo review rules (with `AGENTS.md` as a secondary source).

**Repository memory ("dreaming")** — `MemoryEntry` rows are the source of truth; `RepositoryMemory.content` is a **render cache** produced by `render_memory_document` (`memory/render.py`; fixed `CATEGORY_SECTIONS` order, one whitespace-collapsed bullet per entry) and is the read location agent runs inject. Never make the document model-generated again: `run_consolidation_round` (`memory/consolidation.py`) asks the model for a flat `MemoryOperations` list (ADD/UPDATE/MERGE/CONFIRM/DISCARD) and returns a `RoundOutcome | None`; `ConsolidationRound.validate` checks operations semantically — unknown/superseded entry IDs, cross-category MERGEs, reason-less DISCARDs, out-of-round or double-claimed observations are dropped and logged, the valid remainder applies, and the dropped ops' observations keep their status to be re-queued. `MemoryOperation.shape_error()` (`memory/schemas.py`) handles self-consistency only (arity, required fields); reference validity and the MERGE same-category fence need the round's entry snapshot and live in `ConsolidationRound.validate`. `MemoryOperation` deduplicates `entry_ids`/`observation_ids` in a field validator, so arity checks and the apply phase always reason about the same targets — never re-add caller-side dedup. Entry writes, observation status flips, pruning and the re-render commit in **one** transaction (`ConsolidationRound.apply`). Entries are append-only in content: use `entry.supersede(successor)` / `entry.confirm(when)`, never in-place content edits or deletes; budget pressure supersedes with no successor. `memory_max_lines`/`memory_max_bytes` are enforced by `prune_to_budget` (`memory/render.py`; pressure-triggered, largest category, least-recently-confirmed first, never evicting the last entry) — never by slicing the rendered string.

`consolidate_memory_task` refuses to run a round when a repo has a document but no **active** entries, since the re-render would discard it — that is what `backfill_memory_entries` (and the `0004` data migration) exist to repair, with `--reset-document` as the escape hatch for a document no replay can reconstruct. `document_would_be_discarded` is sync so the task and the dashboard's consolidate view share one definition of *that* guard — the view still mirrors the pending-observations check separately, and checks neither the per-repo `memory.enabled` flag nor anything added to the task later, so a green "queued" toast is not proof the round will run. It lives in the task rather than in `run_consolidation_round`, so the backfill command — whose whole job is creating those entries — is exempt without a caller-identity flag. The **partial**-backfill case (entries exist, but cover a fraction of the document) is reported by `backfill_memory_entries` comparing its final render against the document it started from — never per round inside `ConsolidationRound.apply`, where a mid-replay render is legitimately short and every healthy batched backfill reported its own first batch as unrecoverable data loss. The cron's cooldown reads `RepositoryMemory.last_attempted_at`, which `consolidate_memory_task` bumps in a **`finally`** so it covers the legacy-document skip, failed *and* raising rounds — leave it there, since a round that crashes is exactly the one that must not retry hourly. It does *not* cover the early returns above it, so a repo with `memory.enabled: false` is re-enqueued (cheaply) every sweep. `consolidate_memory_task` is `locked_task`-guarded per repo: the claim bookkeeping in `ConsolidationRound` is per-process, so two overlapping rounds would double-apply the same observations and orphan supersede links.

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

**`thread_id` contract** — callers of `run_job_task` must supply a non-empty UUID `thread_id`. The `Activity` row and LangGraph checkpointer share this key; a missing ID breaks chat resume.

**Skill asset paths** — inside a skill, paths like `scripts/foo.py` resolve to `<location>/<skill-name>/scripts/foo.py`, **not** the bash CWD (repo root). Always invoke skill scripts by absolute path. See `daiv/automation/agent/skills/code-review/scripts/marker.py` as the reference.

**Code-review detector output** — the `cr-*` detectors defer their `{"findings":[...]}` to `/workspace/tmp/subagent-output/<name>-<hash>.json` (via `DeferredOutputMiddleware`, added in `_build_detector_middleware`); the review orchestrator passes those paths to `scripts/findings.py merge` instead of re-typing the JSON. The detector charters are unaware of this — they still just return the structured object. (A detector with no structured response — e.g. one the `LoopBreakerMiddleware` stopped — defers a `.txt` error file instead, which `findings.py merge` counts as a `skipped`/failed detector, never as empty findings.)

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
