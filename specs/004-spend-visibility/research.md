# Research: Spend Visibility

Phase 0 of the [plan](plan.md). Every decision below was read from the code on `main` at `baf6f6d23`; file references are to that tree.

## What the code does today

- **Accounting.** `automation/agent/usage_tracking.py` holds `CostAwareUsageMetadataCallbackHandler`, a LangChain callback registered through an inheritable `ContextVar` hook, so every model call inside `track_usage_metadata()` reaches it: subagents, fallbacks, `web_fetch`'s summariser, `diff_to_metadata` during publish. It prices each call on its own: OpenRouter's reported charge when present (`provider_cost_usd`), else genai-prices, else nothing, which makes the model's cost sticky-unpriced.
- **One place opens it.** `sessions/executor/run.py::_agent_run` (line 485) wraps every agent run, chat included, in `track_usage_metadata()`.
- **Written once, at the end.** The totals reach the `Run` row only through the agent result: `_after_run` → `AgentResult.usage` → `Run.sync_from_task_result` (guarded by `input_tokens is None`) for task-backed runs, and `chat.api.streaming.finalize_chat_run` for chat. A run that raises, is cancelled or loses its worker never reaches `_after_run`, so it records nothing.
- **Model calls outside a run** have no handler: `sessions/titling.py` (sync `.invoke`, per session and per batch), `notifications/classification/llm.py::classify_response_text` (per run), `memory/extraction.py` (per run) and `memory/consolidation.py` (per repository, from a cron).
- **Reads.** `sessions/spend.py::build_session_spend` (session **Spend** section), `session_tags.session_cost` (sessions-list row), the jobs API, the MCP job tools (`mcp_api/server.py::_serialize_job_summary`) and the batch notification read the `Run` fields. Nothing aggregates across sessions. `dashboard/insights.py` has the period, window, previous-period and stacked-chart machinery this feature reuses.

## Spec refinements for approval

These change the spec's letter, not its intent. Approved by the maintainer on 2026-11-19 and applied to `spec.md`.

| # | Spec text | Refinement | Why |
|---|---|---|---|
| SR1 | FR-001, Terms, User Story 4 list "push classification" as background spend | Remove it. The background tasks are session titling (per session and per batch), run classification, memory extraction and memory consolidation | `Ownerships.aclassify_push` (`sessions/inbox/ownership.py:305`) compares commit SHAs; it calls no model |
| SR2 | The **DAIV (background)** row holds repository-wide background spend | Rename it **DAIV** and let it also hold runs no person started (a CI-fix run on a session nobody owns) | `sessions/inbox/drain.py` creates runs "for no one"; without this they have no row and the by-person table stops adding up to the total |
| SR3 | FR-008: every breakdown row links to the sessions list | Rows whose spend served sessions link to them. The **DAIV** person row, the memory-consolidation row and a batch title's row link nowhere, and say why on hover | Repository-wide spend has no session to list |
| SR4 | "Origin" as a run dimension | The run dimension is the run's **trigger type** (`Run.trigger_type`), labelled **Started by** on the page. Background spend is one more value, split by task | The glossary: origin is what started a session, trigger type what started a run. A webhook session holds chat runs, so the session's origin would misfile them |
| SR5 | FR-004: reached "from the main navigation and from Insights" | **Insights** gets a segmented nav, **Activity** and **Spend**, each its own URL; Home's **Team insights** link stays. No new sidebar item | **Insights** itself has no sidebar item; DESIGN.md's segmented nav is the pattern for a page's views |
| SR6 | FR-005: "average cost per run" | Average over runs that recorded spend; the count of runs that recorded nothing is in the coverage note | A run that never called a model would drag the average toward zero |

## Decisions

### R1. A spend ledger table, not aggregation over `Run.usage_by_model`

**Decision**: a new `SpendRecord` model in `sessions`: one row per model per run, and one row per model per background task call. Each row carries its dimensions (person, repository, trigger type or task, session, run, model, time) and its figures, so every breakdown is one `GROUP BY` over one table.

**Rationale**:
- The by-model breakdown and the per-model chart would otherwise unnest `usage_by_model` JSON with `jsonb_each` in raw SQL, and cache and reasoning tokens sit two levels deep in it.
- Background spend has no `Run` row to live on. One table holds both, so totals and every breakdown add up by construction (SC-002).
- Rows outlive their run and session (`SET_NULL`), so deleting a session never rewrites past totals.

**Alternatives**: aggregate `Run` JSON plus a separate background table. Rejected: two sources to keep in step, and raw SQL for the main query. One row per model call: 100–500 rows per run, 50M rows at 100,000 runs, for a per-call detail no requirement reads (LangSmith has it).

### R2. Record a run's spend as each call finishes (FR-001a)

**Decision**:
- **Signal.** `CostAwareUsageMetadataCallbackHandler` takes an optional `on_update` callable. It is called after every accounted call, outside the lock.
- **Writer.** `sessions.spend_recording.RunSpendRecorder` is built in `_agent_run` beside the handler, with the run's id and the running loop.
  - `on_update` only sets an `asyncio.Event` through `loop.call_soon_threadsafe`; nothing else happens in the callback.
  - One writer task waits on the event. Each wake snapshots the handler's ledger totals and writes them as absolute values: `aupdate_or_create` per `(run, provider_slug, model)` and one `Run` update of `input_tokens`, `output_tokens`, `total_tokens`, `cost_usd` and `usage_by_model`.
  - Bursts coalesce into one write.
- **End of run.** On exit from `_agent_run`, by any path including `CancelledError`, the recorder awaits one final shielded write and stops.
- **Errors.** A failed write logs at ERROR with the run id, and the run goes on (FR-002).

**Rationale**:
- **Callback thread.** LangChain runs a sync handler in a thread executor during async runs (`langchain_core/callbacks/manager.py:403-409`, `run_inline=False`). The hop to the loop keeps ORM work off that thread and its unmanaged DB connection.
- **What a killed worker loses.** Absolute snapshots make every write idempotent and order-free. A worker killed mid-run loses only the calls that finished after the last completed write: the call in flight, and at worst the calls whose write was still running. The writer re-checks the event after every write, so nothing waits behind a write that completed. This is the bound FR-001a states.
- **Cost.** At one to three small upserts per call, a 300-call run makes about 1,000 single-row writes over minutes.

**Single writer.** The recorder becomes the only writer of a run's usage fields. `sync_from_task_result` keeps its `input_tokens is None` guard, which already stops it from overwriting. `finalize_chat_run` gains the same guard. Both remain fallbacks for a run the recorder never wrote.

**Alternatives**:
- Writing from `on_llm_end` directly: sync ORM on an executor thread, with connections Django never closes.
- A timer flush every N seconds: a kill loses N seconds of calls, which FR-001a rules out.
- An async callback handler: the handler also runs in the sync `.invoke` of titling, where an async handler is bridged through a new loop.

### R3. Token kinds and how a cost was priced (FR-003)

**Decision**:
- **Tokens.** Each record stores `input_tokens` and `output_tokens` as LangChain reports them (input includes cached and cache-write tokens). Three subsets are stored beside them:
  - `cache_read_tokens` (`input_token_details.cache_read`);
  - `cache_write_tokens` (`cache_creation` + `ephemeral_5m_input_tokens` + `ephemeral_1h_input_tokens`, as `_calc_model_cost` already sums them);
  - `reasoning_tokens` (`output_token_details.reasoning`).

  The page shows uncached input as `input − cache read − cache write`.
- **Pricing.** The handler splits each model's cost three ways: `reported_cost_by_model`, `estimated_cost_by_model` and `unpriced_tokens_by_model` (the tokens of calls with no price). It stops making a model sticky-unpriced for the ledger. A record stores `cost_usd` (the priced part), `reported_cost_usd` (the part the provider reported; `NULL` when the split is unknown, which happens only in backfilled rows) and `unpriced_tokens`.
- **`Run` fields.** `build_usage_summary` keeps today's `Run.cost_usd` rule: `None` when any call is unpriced. Session pages, the API and MCP don't change.

**Rationale**: FR-010 needs reported, estimated and unpriced totals that add up. Keeping three additive columns makes each a plain `SUM`.

**Alternatives**: a per-record `pricing` enum. A model whose OpenRouter calls sometimes drop `usage.cost` mixes sources within one run, so a single label would be wrong.

### R4. Capturing background spend (FR-001)

**Decision**: `sessions.spend_recording.BackgroundSpend`, a context manager with sync and async forms. It opens its own `track_usage_metadata()`, and on exit, by any path, writes one `SpendRecord` per model the calls used. The call sites wrap their one model call:

| Task (`SpendTask`) | Call site | Session | Run | Repository | Person |
|---|---|---|---|---|---|
| `title` | `sessions.tasks.generate_title_task` | the session | — | `repo_id` | the session's |
| `title` (batch) | `sessions.tasks.generate_batch_title_task` | — | — | — | the batch's first run's |
| `run_classification` | `notifications.tasks.classify_run_task` (`classify_response_text`) | the run's | the run | the run's | the run's |
| `memory_extraction` | `memory.tasks.extract_observations_task` | the run's | the run | the run's | the run's |
| `memory_consolidation` | `memory.consolidation.run_consolidation_round` | — | — | `repo_id` | none (**DAIV**) |

A background record is always inserted, never upserted: a retried classification is a second charge.

**Rationale**:
- Each site already knows the run, session or repository it serves, and each is a single model call.
- `memory` and `notifications` sit above `sessions`, so importing `sessions.spend_recording` keeps the layering.
- The handler is a fresh `ContextVar` binding in a separate task, so no run's handler double-counts it.

**Alternatives**: one global handler for everything outside runs, which would lose attribution; recording inside `get_model`, which knows nothing of the session or run.

### R5. Which period a record counts in

**Decision**: `SpendRecord.occurred_at` is the run's `created_at` for run spend, and the call time for background spend.

**Rationale**:
- Run counts on **Insights** bucket by `Run.created_at`. Using the same instant keeps "spend in the period" and "runs in the period" the same set, so averages are honest.
- A run that crosses midnight counts entirely on its first day, which is the spec's edge case and the clarify session's open point.

### R6. Visibility (FR-012, FR-013, SC-006)

**Decision**:
- **Members.** `SpendRecord.objects.visible_to(user)` mirrors `RunManager.visible_to`. Admins see all rows. A member sees a row when any of these holds:
  - its person is them: `_started_by_q`, which reads `user`, `external_provider` and `external_uid`, field names the record copies on purpose;
  - its session belongs to a schedule they subscribe to;
  - its repository is one they can read, and its session has no repository they can't (`_readable_repos_q(user, "session_id")`). A session-less row passes on the repository alone.
- **Scope.** The page's **Mine** scope is `_started_by_q` alone.

**Rationale**: one predicate, built from the helpers that already define run visibility, so a member's spend view, export and API can't drift from the sessions list.
- A repository-wide background row (consolidation) is visible to whoever can read the repository.
- A batch title with no repository is visible only to its person and to admins.

### R7. Existing runs (backfill)

**Decision**: a data migration creates run-spend records from every `Run` with `usage_by_model`:
- one record per model, with `reported_cost_usd = NULL` (split unknown);
- `unpriced_tokens` = the model's tokens when its `cost_usd` is `null`;
- `occurred_at = run.created_at`;
- person, repository, trigger type and session copied from the run.

A run with totals but no per-model split gets one record under the model name `unknown`. The migration carries its own frozen copy of the JSON reading and model-name repair it needs, rather than importing app code. It runs in batches of 1,000 with `bulk_create`. There is no backfill of background spend.

**Rationale**: without it, the page is empty for every period before the deploy, and **All time** contradicts the session pages.

### R8. Aggregation and the 2-second target (SC-003)

**Decision**:
- **Where the queries live.** `dashboard.spend.SpendReport` (one class: the report for a user, window and scope) issues:
  - one totals query each for the window and the previous window, carrying the cache sums and the cache saving (R15);
  - one chart query, bucketed by `Window.trunc` and grouped by model;
  - one bucket query for the hover card's run count and cache hit rate (R18);
  - one query per breakdown, five of them, each with its cache sums;
  - one top-sessions query, ranked by run spend in the window;
  - one coverage query: runs in the window without records, and unpriced models.

  That makes eleven queries.
- **Indexes on `SpendRecord`.** `(occurred_at)`; `(user, occurred_at)`; `(repo_id, occurred_at)`; `(session, occurred_at)`; and the partial unique `(run, provider_slug, model) WHERE task = ''`.
- **Chart.** The top five models in the window get their own segments and the rest fold into **Other**, using `insights.stacked_chart`.
- **Breakdown size.** Each breakdown query returns at most 101 rows: the first 100 for **Show all** (R19), and one more only to tell whether there are more. The **Rest** row of the folded view is the total minus the top rows, so it needs no query of its own.

**Measuring it.** A unit test pins the query count (`django_assert_max_num_queries`). SC-003's timing is checked in quickstart step 9 on a seeded database of 100,000 runs, about 500,000 records. That timing check is not part of `make test`.

**Alternatives**: a daily rollup table. Rejected for now: 500,000 rows aggregate in well under a second on PostgreSQL with these indexes, and a rollup would add a second write path to keep correct.

### R9. Drill-down and the sessions list (FR-008, FR-014, FR-015)

**Decision**:
- **Spend filters.** `SessionFilter` gains `spend_from`, `spend_to`, `spend_person`, `spend_repo`, `spend_trigger`, `spend_task`, `spend_model` and `spend_provider`. When any is present, they combine into one `Exists(SpendRecord…)` on the session, so the list shows exactly the sessions with records counted in the figure that linked there.
- **Person keys.** `spend_person` takes `u<id>`, `x<provider>:<uid>` or `daiv`.
- **Links.** Spend links add `scope=all`, as `DEEP_LINK_PARAMS` does for the existing deep links.
- **Person, model and provider widgets.** They set `spend_person`, `spend_model` and `spend_provider` with no dates. Their choices are the people, models and providers in the records the viewer can see.
- **Session cost** (FR-009, FR-014). One figure everywhere: the session's run spend, the `cost_usd` sum of its `SpendRecord` rows with `task=""`.
  - **Its period**: within `spend_from`/`spend_to` when the list carries them, and all time otherwise.
  - **The list**: `sort=cost` orders by it, descending, with sessions that have no priced spend last. Every row shows it, whatever the sort, replacing `session_tags.session_cost`'s sum of `Run.cost_usd`.
  - **The page**: the top-sessions list ranks by it within the window. Its **Sort sessions by cost** link carries `sort=cost`, `scope=all` and the window's `spend_from`/`spend_to`, so both lists agree.
  - **Why run spend only**: it matches the session's own **Spend** section, which counts only its runs. Unlike `Run.cost_usd`, it keeps the priced part of a run with some unpriced calls. The default order is unchanged.

**Rationale**: separate filters, each with its own `Exists` on runs, would match a session where one run is in the window and a different run is on the model. That breaks FR-008: the list must hold exactly the sessions with spend counted in the figure.

### R10. Export and API (FR-016, FR-017)

**Decision**: one row source, `sessions.spend_ledger.SpendLedger.rows(user, scope, start, end)`, ordered by `(occurred_at, id)`.
- **CSV**: `dashboard.views.SpendExportView` (signed-in, any user) streams CSV with `StreamingHttpResponse` for the page's period and scope.
- **API**: `GET /api/spend/records`, a django-ninja router in `sessions/api/views.py` mounted at `/spend`, with `auth=[AuthBearer(), django_auth]` as `sessions_router` has.
  - Takes `from`, `to` (dates, at most 92 days apart) and `scope`.
  - Keyset pagination with an opaque `cursor`, 500 rows per page, under a new `SpendRateThrottle` fixed at 60 requests a minute per user. A site setting would add a configuration field, a form row and a reference entry for a read that starts no agent work.

The columns are in [contracts/spend-export-api.md](contracts/spend-export-api.md).

**Rationale**:
- Layering rule 5: a JSON endpoint is a ninja router and a download is a view.
- Keyset pagination stays correct while runs keep writing records mid-run.
- The 92-day bound keeps one request's scan near the page's largest period.

### R11. The page

**Decision**:
- **URL and module.** `/dashboard/insights/spend/` (`InsightsSpendView`, name `insights_spend`) sits beside **Insights** under a segmented nav, **Activity** and **Spend** (SR5). Its numbers come from `dashboard/spend.py`.
- **Reused from Insights.** `PERIOD_CHOICES`, `Window` and `_change`, and `_stacked_bars.html` for the chart.
- **Layout.** The [Spend page mockup](https://claude.ai/artifact/EYxfkU58T7b3FodUYzw5Sd), agreed on 2026-11-18, is the reference. The [page contract](contracts/spend-page.md) lists every section.
- **Sections.**
  - Six summary tiles, three per row, each with a better/worse change badge (R16).
  - **Behind the totals**: tokens by kind, and cost by how it was worked out (R17).
  - The chart, with a hover card per bucket (R18).
  - Five ranked tables, each with a share bar, a cache hit rate tag per row, and **Show all** (R19).
  - The ten most expensive sessions.
  - A coverage note naming unpriced models and runs that recorded nothing.
- **Scope switch.** **Mine** / **Everyone**, the sessions list's words. The default is **Everyone** for admins and **Mine** for members (FR-011).
- **Default period.** **30 days**, where **Activity** defaults to 7. Spend is read against monthly bills, and the spec's main story (User Story 1) uses 30 days.

### R12. What does not change

- `build_session_spend`, the session **Spend** section, and the API and MCP run payloads keep reading `Run` fields. They now fill in during the run rather than at its end. The sessions-list cost moves to the ledger (R9) so that it matches the cost sort.
- No agent prompt, tool, skill or middleware that shapes the model request changes, so principle V's evals do not apply. The handler change is accounting only.
- Nothing calls the git platform (principle III holds without platform work).

### R13. Docs and changelog

- **New page** `docs/features/spend.md`: what the page shows, how to read the coverage note, the export, and the API.
- **Updates.** `docs/reference/monitoring.md` *Usage and cost* is the one home for what each figure includes, now with background spend and spend recorded as calls finish. `docs/features/sessions.md` gets the Insights tabs, the new filters and the cost sort.
- **Changelog.** One `Added` line.
- **mkdocs.** The new page goes in the `mkdocs.yml` `nav` and `llmstxt` with a `description`.

## Decisions from the mockup review (2026-11-18)

These come from the review of the [Spend page mockup](https://claude.ai/artifact/EYxfkU58T7b3FodUYzw5Sd) and are recorded in the spec's FR-003a, FR-005, FR-005a, FR-006, FR-007 and FR-015.

### R14. Which provider served a call (FR-003a, FR-007)

**Decision**:
- **Tagging the model.** `automation.agent.base.get_model` sets `metadata={"daiv_provider_slug": …, "daiv_provider_name": …}` on every chat model it builds, from the `Provider` row `parse_model_spec` resolved. LangChain passes a model's `metadata` to the callbacks on `on_chat_model_start`.
- **Reading the tag.** `CostAwareUsageMetadataCallbackHandler` keeps a `run_id → (slug, name)` map, filled on start and popped on end. The ledger side of the handler is keyed by `(provider_slug, model)`.
- **Storing it.** A record stores `provider_slug` and `provider_name` as they were at write time, so renaming or deleting a `Provider` row never rewrites history. The by-provider breakdown and `spend_provider` group by slug and show the stored name.
- **Unchanged.** `Run.usage_by_model` stays keyed by model, as every existing reader expects.
- **Backfilled rows.** The provider is the slug in the run's `agent_model` spec when the row's model is that spec's model, and empty otherwise. An empty provider shows as **Unknown provider**.

**Rationale**:
- Every DAIV model call goes through `get_model`, fallbacks and background tasks included, so one tag covers every path.
- The response itself says nothing reliable about the provider. An OpenRouter model name has a slash, but two OpenAI-compatible providers can serve the same name.

**Alternatives**: inferring the provider from the model name, as `infer_provider_id` does for pricing. Rejected: it can't tell a self-hosted OpenAI-compatible server from OpenAI.

### R15. Prompt cache hit rate and cache saving (FR-003a, FR-005, FR-007)

**Decision**:
- **Hit rate.** Cache-read tokens over all input tokens, summed over a scope, so the rate is weighted by tokens.
- **Rate not reported.** A record carries `cache_reported`: true when any of its calls' usage had a `cache_read` entry. Rows and totals compute the rate only over reported records. A scope with none shows **not reported**, never 0%.
- **Saving.** The handler prices each call's saving as `cache_read × (input rate − cache-read rate)`, using genai-prices' rates for the model at the call's input size, the same tier lookup `_resolve_mtok_rate` already does. The saving is stored on the record as `cache_saving_usd`.
- **Saving not computable.** Cache-read tokens whose model lacks either rate go to `cache_unpriced_tokens`, and any total with some marks the saving as a floor.
- **No saving for OpenRouter's charge.** The saving is computed from list prices even where OpenRouter reported the charge, because the charge says nothing about what an uncached call would cost.
- **Colour thresholds.** 85% or more is good, 70–84% neutral, under 70% needs attention. They are constants in `dashboard/spend.py`, not settings.

**Rationale**: storing the saving at write time freezes the price used, as `cost_usd` already does. A later price change can't move past figures.

### R16. Change badges that know which way is better (FR-005)

**Decision**: `insights._change` gains `better: "up" | "down" | None`, and with it returns `tone`: `good`, `bad` or `neutral`.
- **Badge.** A new `_change_badge.html` renders the tone as a pill in `status-clear`, `status-fail` or `text-muted` on a weak tint. The Spend tiles use it.
- **Activity.** Its tiles and its sessions by origin use the same badge: runs, success rate, runs that changed code and session counts are better when they go up; average duration is neutral.
- **Direction per tile.** Cost, tokens and average cost per run are worse when they go up. Cost per run that changed code is better when it goes down. The cache hit rate is better when it goes up. Runs with spend are neutral.

### R17. "Behind the totals" (FR-005a)

**Decision**: two panels under the tiles, from the same totals query.
- **Shape.** Each panel has a split bar, then one row per part with its swatch, label, amount, share and a one-line meaning.
- **Shades.** The three kinds of input share one hue in different shades, and output has a second hue.
- **Unpriced note.** The cost panel adds an amber note naming the unpriced tokens and their models, which explains the `+` on the total.

The panels need no extra query: every figure is a sum the totals query already returns.

### R18. Chart hover card (FR-006)

**Decision**:
- **Rendering.** Each column's card is in the server-rendered HTML. An Alpine `x-data` on the chart shows a column's card on `mouseenter` and `focus`, and hides it on `mouseleave` and `blur`. The column stays the link to its sessions.
- **Content.** The bucket's total, its cost and share per model in legend order, its run count and its cache hit rate. The run count and hit rate come from one bucket query grouped by `Window.trunc`, with `Count("run", distinct=True)` and the cache sums.
- **Placement.** The card opens on the side with room: right of the first half of the columns, left of the rest. `role="tooltip"`, with `aria-describedby` on the column link.

**Rationale**: a server-rendered card is readable without JavaScript and by screen readers, and needs no API call. DESIGN.md keeps Alpine for small behaviours like this.

### R19. Show all (FR-007)

**Decision**:
- **Folded.** Each breakdown shows its top 5 rows and a **Rest** row.
- **Expanded.** **Show all N {people|repositories|models|providers}** reveals rows 6–100, server-rendered and hidden until asked, with Alpine. **Show the top 5 only** folds it back.
- **More than 100 rows.** The expanded list ends with "N more not shown". Once the export exists (US5), that line reads "N more in the CSV export" and links to it.
- **Started by.** Its rows are trigger types plus the background tasks, a fixed and short list, so it always shows in full.

**Rationale**: in-place expansion keeps the period, scope and the other tables in view. Most sites have tens of people and repositories, so 100 rows render cheaply, and the CSV covers the rest.

**Alternatives**: a separate page per breakdown, with sorting and search. That is a better fit for sites with hundreds of repositories; it can come later on top of the same query.
