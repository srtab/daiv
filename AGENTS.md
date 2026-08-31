# AGENTS.md

This file provides guidance to agents when working with code in this repository.

## Commands (verified)

```bash
make test          # unit tests + coverage; CI gate
make lint-fix       # check + fix lint/format in one step — prefer this over make lint
make lint           # check only (no fixes); CI gate
make lint-typing    # ty, daiv/ only

# Single test / pattern
uv run pytest tests/unit_tests/accounts/test_views.py
uv run pytest tests/unit_tests/ -k "test_notes"

# Integration tests (real LLM calls; need docker/local/app/config.secrets.env: LLM key + GitLab creds)
make integration-tests

# Translations
make makemessages && make compilemessages
```

- `asyncio_mode = "auto"` — no `@pytest.mark.asyncio` needed.
- Python **3.14 only** (`requires-python = ">=3.14,<3.15"`).
- Never edit `pyproject.toml` by hand — use `uv add <pkg>==<version>` / `uv remove <pkg>` (deps are `==`-pinned; `parable` is git-pinned, not on PyPI).

## Repo map (only what's non-obvious)

- `daiv/automation/` — LangGraph/deepagents agent: tools (`agent/tools/`), skills (`agent/skills/`), deferred tools (`agent/deferred/`), middleware (`agent/middlewares/`)
- `daiv/codebase/` — GitLab/GitHub clients (`clients/`), webhooks, `.daiv.yml` repo config
- `daiv/mcp_server/` + `daiv/jobs/` — MCP sub-app (`submit_job`/`get_job_status`) + `run_job_task` (MCP + webhooks)
- `daiv/core/` — sandbox client, Redis, shared constants
- `daiv/daiv/settings/components/` — split settings; `common.py` has `INSTALLED_APPS`
- `evals/` — eval suite, **not** run by `make test`
- UI rules live in `DESIGN.md` (icon system, layout tokens, floating surfaces) and `docs/` (mkdocs) — consult before touching templates/CSS.

## Invariants / footguns

- **Test imports** — `pythonpath = ["daiv", "."]`; in tests import `from automation.agent.graph import ...` (no `daiv.` prefix).
- **Tools can't mutate `runtime.state`** — return a `Command(update={...}, messages=[ToolMessage(...)])`; in tests unpack `isinstance(result, Command)`.
- **Auth** — email signup disabled (`AccountAdapter.is_open_for_signup` → `False`); `AdminRequiredMixin` needs `user.is_admin`; `APIKey.objects.create_key(...)` is **async**.
- **`run_job_task` requires a non-empty UUID `thread_id`** — the `Activity` row and checkpointer share it; missing breaks chat resume.
- **Bot labels** (`daiv` / `daiv-max` / `daiv-auto`) live in `daiv/core/constants.py` — don't hardcode.
- **Per-repo agent config** — agent reads `.agents/AGENTS.md`, skills from `.agents/skills/`, subagents from `.agents/subagents/`; a custom skill shadows a same-named built-in.
- **Repository memory** — `MemoryEntry` rows are append-only truth; `RepositoryMemory.content` is a render cache from `memory/render.py`, never model-generated; enforce `memory_max_*` via `prune_to_budget`, not by slicing.
- **Sandbox wire schemas** — `daiv/core/sandbox/schemas.dump.json` is canonical; `tests/unit_tests/core/sandbox/test_schema_consistency.py` fails on drift. Regenerate from the [daiv-sandbox](https://github.com/srtab/daiv-sandbox) repo after changing `daiv_sandbox/schemas.py`.
- **Skill asset paths** resolve to `<location>/<skill>/...`, not the bash CWD — invoke skill scripts by absolute path.
- **PEP 758** — `except E1, E2:` is valid; ruff canonicalises it unparenthesised — don't "fix" it back to parens (pure churn).
- **Django** — test settings module is `daiv.settings.test`; `NINJA_SKIP_REGISTRY=true` is auto-set in tests.
- **Views by content type** — HTML = CBVs in `daiv/<app>/views.py`; JSON = a django-ninja `Router` in `daiv/<app>/api/views.py` (or `api/router.py`), registered in `daiv/daiv/api.py`. Filtered lists use `django_filters.FilterSet` + `FilterView(strict=False)`, not hand-rolled `request.GET`/`Paginator`.
- **Dependency upgrade blockers** (re-verify first): **redis 8.x** blocked by `redisvl` (via `langgraph-checkpoint-redis`); **mcp 2.0** blocked by `langchain-mcp-adapters` (removes v1 import paths used in `mcp_server/` + `automation/agent/mcp/`). Deps are `==`-pinned, so `uv lock --upgrade --dry-run` shows only transitive updates — check the PyPI JSON API per direct dep.
- **Detailed behaviour lives in module docstrings**, not here — e.g. `automation/agent/chat_models.py`, `core/ui_events.py`, `chat/api/relay.py`, `sessions/pipeline_watch/`. Read the relevant docstring before changing that area.

## Where changes usually go

| Change type | Start here |
|---|---|
| New agent tool | `daiv/automation/agent/tools/` |
| New built-in skill | `daiv/automation/agent/skills/<name>/` (`SKILL.md` + optional `scripts/`, `examples/`) |
| New agent middleware | `daiv/automation/agent/middlewares/` |
| MCP tool | `daiv/mcp_server/server.py` |
| Shared settings / new app | `daiv/daiv/settings/components/common.py` (`LOCAL_APPS`) |
| LLM model list / provider | `daiv/automation/agent/base.py`, `daiv/automation/agent/constants.py` |
