# AGENTS.md

This file provides guidance to agents when working with code in this repository.

`constitutions/index.md` gives the reading order; start there. `constitutions/constitution.md` is the
charter, and with its companion documents it outranks this file.

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
make lint-typing    # ty, daiv/ only; CI gate. Runs on the full env (uv run syncs it)
make lint-imports   # app layering contracts in .importlinter; part of lint and lint-fix
make lint-design    # official design.md linter on DESIGN.md (npx, needs Node; not part of lint); fails on warnings too
make up             # this checkout's stack; a git worktree gets its own on http://localhost:<8000+slot> (CONTRIBUTING.md "Worktree stacks"); preview_start "daiv" runs it

make integration-tests   # real LLM calls; needs docker/local/app/config.secrets.env (LLM key + GitLab creds). Runs -m "diff_to_metadata or memory"; DAIV_EVAL_REPEATS=1 for a fast local pass (not a gate).

make eval-prompts SUITES="skills todos" OUT=eval-runs/before.jsonl   # prompt evals on EVAL_MODEL (GLM-5.2): DAIV_EVAL_REPEATS passes (default 3), one JSONL row per case per pass; markers: ask_user skills todos web_search subagents code_review
uv run evals/compare_runs.py BEFORE.jsonl AFTER.jsonl                # PR-body table and verdict: regressed / unconfirmed / inconclusive / improved / neutral
# "unconfirmed" = a flip over 3 votes; it prints a `make eval-prompts CASES=...` re-run for each side, whose two files go back in with --confirm BEFORE_CONFIRM AFTER_CONFIRM
```

- `parable` is git-pinned, not on PyPI.

## Repo map (only what's non-obvious)

- `daiv/automation/` — LangGraph/deepagents agent: middlewares (`agent/middlewares/`), skills (`agent/skills/`), deferred tools (`agent/deferred/`); a run's context (`RuntimeCtx`, `set_runtime_ctx`) is in `agent/context.py`, its external references in `agent/references.py`. `agent/` is runtime only: the model-picker UI (display helpers, picker context, per-provider model catalog) is `picker/`
- `daiv/codebase/` — platform access only: GitLab/GitHub clients (`clients/`), `.daiv.yml` repo config, repo authorization, the repository catalog
- `daiv/webhooks/` — GitLab/GitHub webhook callbacks, the `address_*` tasks and the issue/MR addressors; each platform's `views.py` has its own router, composed in `webhooks/api/router.py` and mounted at `/codebase` in `daiv/daiv/api.py`, so the URLs stay `/api/codebase/callbacks/{gitlab,github}`
- `daiv/mcp_api/` — DAIV's own MCP endpoint (`server.py`, mounted at `/mcp` by `daiv/daiv/asgi.py`) + its OAuth discovery and client registration; `daiv/mcp_connectors/` — the external MCP servers the agent connects to (`MCPServer`), app label still `mcp_servers` (tables, migrations, `apps.get_model`)
- `daiv/jobs/` — jobs REST API (`jobs/tasks.py` only re-exports `run_job_task` for task rows queued under the old path). `run_job_task` lives in `sessions/executor/tasks.py` and is enqueued only by `sessions.queue.aenqueue_run`, for `sessions.services.asubmit_batch_runs` (jobs API, MCP, schedules, dashboard, the agent's `delegate_jobs` via `sessions.delegation`), `sessions.queue.dispatch_next_queued` (a session's next queued run), `sessions.queue.release_orphan_queued_runs` (orphan sweep) and `sessions.pipeline_watch.dispatch` (CI fix runs). Chat runs inline; webhooks call the executor directly.
- `daiv/core/` — sandbox client, Redis, shared constants, the site skeleton `base.html` and the app-agnostic partials (`core/_pagination.html`, `core/_breadcrumb.html`); query strings use Django's built-in `{% querystring %}`
- `daiv/accounts/` — identity only: users, sign-in (allauth adapters, OAuth), API keys, user admin, and the `accounts/_avatar.html` partial (+ `avatar_tags`) other apps include
- `daiv/dashboard/` — the app shell: dashboard and homepage views, the `nav` context processor, the nav SSE badges (`api/views.py`), `nav_tags`, and the `base_app.html` layout every signed-in page extends. A lower app's template gets request-wide values from that app's own context processor (e.g. `codebase.context_processors.git_platform`), and a view whose URL belongs to another sidebar section sets `request.nav_view_name` to that section's URL name
- `daiv/configuration/` — the site-configuration UI (forms, views, templates); the data it edits (`SiteConfiguration`, `Provider`, `site_settings`) stays in `core` because lower layers read it
- `evals/` — eval suite, **not** run by `make test`; `/code-review` on ReviewBench: how to run and judge it, the experiment log and the judged baseline to compare against are in `evals/results/reviewbench/README.md`

## Invariants / footguns

- **`InlineToolDefinitionsMiddleware` stays the last DAIV middleware** (main and subagent stacks) — a middleware that appends messages to the model request must sit before it, or an Anthropic request gets a system turn followed by a user turn and langchain raises; see `automation/agent/deferred/inline.py`.
- **Tools can't mutate `runtime.state`** — return a `Command(update={...}, messages=[ToolMessage(...)])`; in tests unpack `isinstance(result, Command)`.
- **Auth** — email signup disabled (`AccountAdapter.is_open_for_signup` → `False`); `AdminRequiredMixin` needs `user.is_admin`; `APIKey.objects.create_key(...)` is **async**.
- **`run_job_task` requires a non-empty `thread_id`** — missing breaks chat resume. The task checks only non-empty; the UUID check is in `sessions.services.asubmit_batch_runs`.
- **Per-repo agent config** — agent reads `.agents/AGENTS.md`, skills from `.agents/skills/`, subagents from `.agents/subagents/`; a custom skill shadows a same-named built-in.
- **Repository memory** — `MemoryEntry` rows are append-only truth; `RepositoryMemory.content` is a render cache from `memory/render.py`, never model-generated; enforce `memory_max_*` via `prune_to_budget`, not by slicing.
- **Sandbox wire schemas** — `tests/unit_tests/core/sandbox/test_schema_consistency.py` fails when `daiv/core/sandbox/schemas.dump.json` drifts. Regenerate it from the [daiv-sandbox](https://github.com/srtab/daiv-sandbox) repo after changing `daiv_sandbox/schemas.py`.
- **Skill asset paths** resolve to `<location>/<skill>/...`, not the bash CWD — invoke skill scripts by absolute path.
- **PEP 758** — `except E1, E2:` is valid; ruff canonicalises it unparenthesised — don't "fix" it back to parens (pure churn).
- **Django** — test settings module is `daiv.settings.test`; `NINJA_SKIP_REGISTRY=true` is auto-set in tests.
- **Run artifacts** — `MEDIA_ROOT` must be a volume shared by the web and worker containers (the worker writes `RunArtifact` bytes, the web serves them); when adding viewer kinds keep `content_type` extension-derived and the raw endpoint's `sandbox` CSP.
- **Detailed behaviour lives in module docstrings**, not here — e.g. `automation/agent/chat_models.py`, `core/ui_events.py`, `sessions/live/relay.py`, `sessions/pipeline_watch/`. Read the relevant docstring before changing that area.

## Where changes usually go

| Change type | Start here |
|---|---|
| New agent tool | a middleware exposing `self.tools` (see `web_fetch.py`, `artifacts.py`) |
| New built-in skill | `daiv/automation/agent/skills/<name>/` (`SKILL.md` + optional `scripts/`, `examples/`) |
| New agent middleware | `daiv/automation/agent/middlewares/` |
| MCP tool | `daiv/mcp_api/server.py` |
| Webhook event handling | `daiv/webhooks/<platform>/callbacks.py` |
| Shared settings / new app | `daiv/daiv/settings/components/common.py` (`LOCAL_APPS`), then `.importlinter` |
| LLM model list / provider | `daiv/automation/agent/base.py` (building the chat model), `daiv/automation/agent/model_spec.py` (parsing `slug:model` against `Provider` rows), `daiv/core/constants.py` (`ModelName`) |
