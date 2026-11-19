# Implementation Plan: Spend Visibility

**Branch**: `claude/daiv-token-spending-visibility-4a845f` (spec-kit feature `004-spend-visibility`) | **Date**: 2026-11-18, updated 2026-11-19 with the mockup review | **Spec**: [spec.md](spec.md) | **Mockup**: [Spend page](https://claude.ai/artifact/EYxfkU58T7b3FodUYzw5Sd) | **Status**: Approved by the maintainer on 2026-11-19, including the [spec refinements](research.md#spec-refinements-for-approval) and the mockup review

**Input**: Feature specification from `specs/004-spend-visibility/spec.md`

## Summary

DAIV already prices every model call inside a run, but writes the totals once, when the run ends cleanly. It records nothing for model calls outside runs, and never adds spend up across sessions. This plan adds three things.

1. **A spend ledger.** `SpendRecord`, in `sessions`, holds one row per provider and model per run, and one per provider and model per background task call. Each row carries its person, repository, trigger type or task, session, run, provider and figures, including cache tokens and the estimated cache saving. A data migration backfills it from existing runs.
2. **Recording as calls finish.**
   - `RunSpendRecorder` listens to the existing accounting handler and rewrites the run's rows and `Run` fields after every call. A run that fails, is cancelled or loses its worker keeps what it spent.
   - `BackgroundSpend` wraps the four model calls DAIV makes outside runs: titles, run classification, memory extraction and memory consolidation.
3. **Reading it.**
   - An **Insights → Spend** page, laid out as in the [mockup](https://claude.ai/artifact/EYxfkU58T7b3FodUYzw5Sd):
     - summary tiles whose change badges say better or worse, including the prompt cache hit rate and its saving;
     - **Behind the totals**: tokens by kind, and how the cost was worked out;
     - spend over time by model, with a hover card per bucket;
     - five breakdowns (person, repository, trigger, model, provider), each with a cache hit rate per row and **Show all**;
     - the costliest sessions, and a coverage note.
   - Exact drill-down filters (provider included), and a cost sort, on the sessions list.
   - A CSV export, and `GET /api/spend/records`.

   All of them use the visibility rule runs already have.

The decisions behind each part are in [research.md](research.md): R1–R13, plus R14–R19 from the mockup review. Six refinements to the spec's wording (SR1–SR6) were approved with it and are applied to the spec.

## Technical Context

**Language/Version**: Python 3.14.

**Primary Dependencies**:
- Django 6 (ORM, `StreamingHttpResponse`, class-based views);
- django-ninja (the API router, `AuthBearer`, `AuthRateThrottle`);
- django-filter (`SessionFilter`);
- LangChain core callbacks (the existing `CostAwareUsageMetadataCallbackHandler`, plus model `metadata` for the provider tag);
- genai-prices (unchanged pricing; its input and cache-read rates also give the cache saving);
- Alpine.js (already loaded) for the hover card and **Show all**.

No new dependency.

**Storage**: PostgreSQL. One new table, `agent_sessions_spendrecord`, with four indexes, one partial unique constraint and one check constraint. One schema migration and one data migration in `sessions`. No change to the `Run` schema.

**Testing**: pytest, pytest-django and pytest-asyncio (`asyncio_mode = "auto"`). Async tests that write use `django_db(transaction=True)`. Models are stubbed with LangChain fake chat models that return `usage_metadata`. No real LLM or network. The query count is pinned with `django_assert_max_num_queries`. No evals (R12).

**Target Platform**: Linux containers: DAIV's web process (page, export, API) and worker processes (recording).

**Project Type**: a Django web service with background workers that run the agent.

**Performance Goals**:
- The page loads in under 2 s for 30 days on 100,000 runs, in eleven queries (SC-003; R8; quickstart step 9).
- Background spend shows up within 1 minute of its task (SC-004).
- Recording adds one to three single-row upserts per model call.

**Constraints**:
- Recording never fails or delays the work it measures (FR-002). Its failures log at ERROR.
- Visibility mirrors `RunManager.visible_to` exactly (FR-012, FR-013).
- `Run` usage fields keep their meaning for every existing reader.
- Layering: nothing below `sessions` learns about spend records; `automation` only gains a callback hook, the ledger keyed by provider, and a provider tag on models.

**Scale/Scope**: about 5 ledger rows per run, so about 500,000 rows at 100,000 runs. The work lands in four apps (`automation`, `sessions`, `memory`, `notifications`) plus `dashboard`, with one new page, one export, one API endpoint and three docs pages.

## Constitution Check

*GATE: checked before Phase 0 and again after Phase 1 design. Both pass; there are no violations to justify.*

| Principle | How the plan complies | Evidence at review |
|---|---|---|
| I. Secure and isolated | Every read goes through `SpendRecord.objects.visible_to`, built from `_started_by_q` and `_readable_repos_q`, the predicates behind run visibility (R6). The API requires a key or a session, and a member's rows are the page's rows. Rows hold no secret: names, repositories, model names, numbers. Errors return no internals | Visibility tests per surface: page, top sessions, CSV, API, sessions-list person and model options (SC-006) |
| II. Architecture boundaries | `SpendRecord`, the recorders and the ledger live in `sessions` (layer 8). `memory` and `notifications` call down into it; `dashboard` reads it. `automation` gains only an `on_update` callable. No new `ignore_imports`. Behaviour sits in classes, one concern each: `RunSpendRecorder`, `BackgroundSpend`, `SpendLedger`, `SpendReport`. Uses the existing extension points: ninja router, class-based views, `FilterSet`. No run-mode branching | `make lint-imports`; review |
| III. Platform and provider neutrality | No git-platform call. Pricing works for every provider genai-prices knows, plus OpenRouter's reported charge. Unpriced models are shown as such, never hidden. The provider breakdown reads the `Provider` row each call resolved to, never a hardcoded list, and a provider that reports no cache tokens shows "not reported" rather than a false 0% | Nothing platform-specific to test; unpriced-model, provider-tag and cache-not-reported tests |
| IV. Tests prove behaviour | Each new module has its mirror test file; see Project Structure. Each requirement has a failing-first test: kill/cancel survival, background-on-exception, breakdowns summing to the total, exact drill-down, visibility, cost sort, CSV sum, API paging, provider tagging, the hit rate and saving, change-badge tones, and Show all past 100 rows | `make test` output in the pull request |
| V. Measured agent behaviour | Not applicable. No prompt, tool, skill, subagent or request-shaping middleware changes; the handler change is accounting only (R12) | Stated in the pull request |
| VI. Honest failure handling | A failed write logs at ERROR with the run id, and the run goes on. Totals with unpriced tokens or unrecorded runs carry a floor marker and a coverage note. "No price" is never shown as `$0` (FR-010). The cost source is labelled. The cache saving is labelled an estimate and marked as a floor where it couldn't be priced | Tests on the logging path and on the floor and coverage branches |
| VII. Documentation | New `docs/features/spend.md`. `docs/reference/monitoring.md` *Usage and cost* stays the one home for what figures mean. `docs/features/sessions.md` gets the tabs, filters and sort. `mkdocs.yml` `nav` and `llmstxt`. A `CHANGELOG.md` `Added` line | `mkdocs build --strict`; `test_docs_links.py`, `test_docs_style.py` |

**Gates**: `make lint`, `make lint-typing`, `make test`, a docs build, and the changelog line. Evals: not applicable (V).

**Post-design re-check (after Phase 1)**: unchanged. The data model adds no import across layers. The contracts expose no field a member couldn't already see in the sessions list. The CSV and API share one row source, so they can't drift.

## Project Structure

### Documentation (this feature)

```text
specs/004-spend-visibility/
├── spec.md
├── plan.md              # this file
├── research.md          # Phase 0: R1–R13, SR1–SR6; R14–R19 from the mockup review
├── data-model.md        # Phase 1: SpendRecord, SpendTask, handler fields, migrations
├── quickstart.md        # Phase 1: validation steps
├── contracts/
│   ├── spend-page.md    # the page, the export URL, the sessions-list params
│   └── spend-export-api.md  # row fields, GET /api/spend/records
├── checklists/requirements.md
└── tasks.md             # Phase 2 (/speckit-tasks; not created here)
```

### Source Code (repository root)

```text
daiv/
├── automation/agent/usage_tracking.py     # handler: ledger by (provider, model), pricing split, cache saving, on_update hook
├── automation/agent/base.py               # get_model tags each model's metadata with its provider
├── sessions/
│   ├── models.py                          # + SpendTask, SpendRecord
│   ├── managers.py                        # + SpendRecordQuerySet (visible_to, started_by, in_window)
│   ├── spend_recording.py                 # new: RunSpendRecorder, BackgroundSpend
│   ├── spend_ledger.py                    # new: SpendLedger (rows, person keys, cursor)
│   ├── executor/run.py                    # _agent_run builds and closes the RunSpendRecorder
│   ├── tasks.py                           # titling tasks wrapped in BackgroundSpend
│   ├── filters.py                         # + spend_* filters (provider included), sort
│   ├── views.py                           # sort and person/model/provider choices in the list context
│   ├── templates/sessions/                # filter bar: Person, Model, Provider, Sort
│   ├── api/views.py, api/schemas.py       # + spend_router, row schema
│   └── migrations/0037_spendrecord.py, 0038_backfill_spendrecord.py
├── chat/api/streaming.py                  # finalize_chat_run: usage write only while unset
├── notifications/tasks.py                 # classify_run_task wrapped in BackgroundSpend
├── memory/tasks.py, memory/consolidation.py  # extraction and consolidation wrapped
├── core/api/throttling.py                 # + SpendRateThrottle
├── daiv/api.py                            # mount spend_router at /spend
├── static_src/css/input.css               # reusable classes for the badge, cache tag and hover card
└── dashboard/
    ├── insights.py                        # _change gains `better` and returns a tone (R16)
    ├── spend.py                           # new: SpendReport (totals, panels, chart + hover, five breakdowns, top sessions, coverage)
    ├── views.py, urls.py                  # + InsightsSpendView, SpendExportView
    ├── context_processors.py              # insights_spend highlights the Insights section
    ├── templates/dashboard/               # insights_spend.html, _insights_nav.html, _change_badge.html,
    │                                      #   _spend_split.html, _spend_breakdown.html; _stacked_bars.html gains the hover card
    └── locale/pt/…                        # new strings, hand-added (makemessages rewrites every app's .po)

tests/unit_tests/
├── automation/agent/test_usage_tracking.py
├── sessions/test_spend_recording.py, test_spend_ledger.py (new), test_models.py, test_managers.py,
│   test_filters.py, test_views_list.py, test_tasks.py, test_api.py, test_migrations.py, executor/test_run.py
├── chat/api/test_streaming.py
├── notifications/test_tasks.py
├── memory/test_extraction_task.py, test_consolidation_task.py, test_consolidation.py
├── automation/agent/test_base.py
└── dashboard/test_spend.py, test_insights.py, test_views.py

docs/features/spend.md (new) · docs/reference/monitoring.md · docs/features/sessions.md · mkdocs.yml · CHANGELOG.md
```

**Structure Decision**: the existing Django apps, by layer. The ledger, its recording and its row source live with `Run` in `sessions`, the lowest app that both the trigger-side callers and the dashboard can import. The page and export live in `dashboard` beside **Insights**, whose period machinery they reuse. There is no new app.

## Delivery order

Each step is shippable on its own and ordered by the spec's priorities.

1. Ledger, backfill and recording: `SpendRecord`, the migrations, the provider tag, the handler split with the cache saving, `RunSpendRecorder` (FR-001a, FR-003, FR-003a).
2. The page for P1: summary tiles and badges, **Behind the totals**, the chart and its hover card, the five breakdowns with cache tags and **Show all**, scope and visibility (User Stories 1 and 2).
3. P2: top sessions, the sessions-list filters and sort (User Story 3), and `BackgroundSpend` at the four call sites (User Story 4).
4. P3: CSV export and API (User Story 5).
5. Docs and changelog. These land with whichever step ships the page users see, so each step's pull request carries its own pages (constitution VII).

## Complexity Tracking

No constitution violations, and no new import-linter exception.
