---

description: "Task list for spend visibility"
---

# Tasks: Spend Visibility

**Input**: Design documents from `specs/004-spend-visibility/`

**Prerequisites**: [plan.md](plan.md), [spec.md](spec.md), [research.md](research.md), [data-model.md](data-model.md), [contracts/](contracts/), [quickstart.md](quickstart.md). Layout reference: the [Spend page mockup](https://claude.ai/artifact/EYxfkU58T7b3FodUYzw5Sd).

**Tests**: Included. Constitution principle IV and `patterns/tests.md` require a test for every behaviour change, one that fails without the change. Tests stub every model: a LangChain fake chat model returns set `usage_metadata` and `response_metadata`. No real LLM or network call is made. An async test that writes uses `django_db(transaction=True)`. Principle V's evals do not apply (research R12).

**Organization**: Tasks are grouped by user story so each story can be built and tested as its own increment.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: the user story the task belongs to (US1–US5)

## Path Conventions

- Paths are relative to the repository root: `daiv/`, `tests/unit_tests/`, `docs/`.
- Tests mirror `daiv/` one file per module (`patterns/tests.md`), and import without the `daiv.` prefix.
- Names, limits, texts and thresholds quoted below come from the contracts and the data model. Use them verbatim.

---

## Phase 1: Setup

**Purpose**: approval, the refined spec, and a clean baseline.

- [X] T001 Record the maintainer's approval of plan.md, refinements SR1–SR6 and the mockup review in the **Status** line of specs/004-spend-visibility/plan.md (done 2026-11-19).
- [X] T002 Apply refinements SR1–SR6 from specs/004-spend-visibility/research.md to specs/004-spend-visibility/spec.md (done 2026-11-19).
- [X] T003 Confirm the next free migration numbers in daiv/sessions/migrations/ (0037 and 0038 when this was planned; renumber the data model's names if `main` has moved). Run `make test` once before any change and note pre-existing failures, so that later failures can be told apart.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: the ledger, how runs write to it as each call finishes, and the predicates every surface reads through. After this phase every run writes `SpendRecord` rows, but nothing shows them yet.

**⚠️ CRITICAL**: no user story work can begin until this phase is complete.

### The ledger

- [X] T004 Add `SpendTask(models.TextChoices)` to daiv/sessions/models.py: `TITLE = "title", _("Session titles")`, `RUN_CLASSIFICATION = "run_classification", _("Run classification")`, `MEMORY_EXTRACTION = "memory_extraction", _("Memory extraction")`, `MEMORY_CONSOLIDATION = "memory_consolidation", _("Memory consolidation")`.
- [X] T005 Add `SpendRecord` to daiv/sessions/models.py with every field of data-model.md, verbatim:
  - **Time and links**: `occurred_at` `DateTimeField` indexed; `run` FK `Run` `SET_NULL`, null, `related_name="spend_records"`; `session` FK `Session` `SET_NULL`, null, `related_name="spend_records"`.
  - **Dimensions**: `repo_id` `CharField(255)` blank; `trigger_type` `CharField(20)` with `SessionOrigin` choices, blank; `task` `CharField(24)` with `SpendTask` choices, blank.
  - **Person**: `user` FK `settings.AUTH_USER_MODEL` `SET_NULL`, null, `related_name="spend_records"`; `external_provider` `CharField(32)`; `external_uid` `CharField(191)`; `external_username` `CharField(255)`; `person_name` `CharField(255)`; all four blank.
  - **Model and provider**: `model` `CharField(255)`; `provider_slug` `CharField(32)` blank; `provider_name` `CharField(64)` blank.
  - **Tokens**: `input_tokens`, `cache_read_tokens`, `cache_write_tokens`, `output_tokens`, `reasoning_tokens`, `total_tokens`, each `PositiveBigIntegerField(default=0)`.
  - **Cost**: `cost_usd` `DecimalField(12, 6, default=0)`; `reported_cost_usd` `DecimalField(12, 6)` null; `unpriced_tokens` `PositiveBigIntegerField(default=0)`.
  - **Cache**: `cache_reported` `BooleanField(default=False)`; `cache_saving_usd` `DecimalField(12, 6, default=0)`; `cache_unpriced_tokens` `PositiveBigIntegerField(default=0)`.
  - **Timestamp**: `updated_at` `auto_now`.
  - **Meta**:
    - `UniqueConstraint(fields=["run", "provider_slug", "model"], condition=Q(task="", run__isnull=False), name="spend_run_model_uniq")`;
    - `CheckConstraint(condition=Q(task="") ^ Q(trigger_type=""), name="spend_kind_check")`;
    - indexes `spend_occurred_idx (occurred_at)`, `spend_user_occurred_idx (user, occurred_at)`, `spend_repo_occurred_idx (repo_id, occurred_at)`, `spend_session_occurred_idx (session, occurred_at)`.
- [X] T006 Add `SpendRecordQuerySet` to daiv/sessions/managers.py and attach it as `SpendRecord.objects`:
  - `visible_to(user)`: all rows for admins; otherwise `_started_by_q(user) | Q(session__scheduled_job__in=user.subscribed_schedules.values("pk")) | _readable_repos_q(user, "session_id")`, the run-visibility predicate (research R6), SYNC ONLY like its siblings;
  - `started_by(user)`: `_started_by_q(user)`;
  - `in_window(window)`: `window.date_q("occurred_at")`;
  - `run_spend()`: `task=""`;
  - `background()`: `task` not blank.

  Tests in tests/unit_tests/sessions/test_managers.py: a member sees their own rows, rows on a readable repository and rows of a subscribed schedule's session. They never see a row on an unreadable repository, nor a row whose session also has an unreadable extra repository. A session-less row on a readable repository is visible; a session-less row with no repository is visible to its person and admins only.
- [X] T007 Generate the schema migration daiv/sessions/migrations/0038_spendrecord.py with `uv run python daiv/manage.py makemigrations agent_sessions` (app label `agent_sessions`), and check it holds both constraints and all four indexes.
- [X] T008 Write the data migration daiv/sessions/migrations/0039_backfill_spendrecord.py (research R7, data-model "Migrations"). For every `Run` with `usage_by_model`, in `iterator(chunk_size=1000)` batches with `bulk_create`, create one record per model:
  - **Figures**: tokens from the model's entry (cache read = `input_token_details.cache_read`; cache write = `cache_creation + ephemeral_5m_input_tokens + ephemeral_1h_input_tokens`; reasoning = `output_token_details.reasoning`); `cost_usd` = the model's `cost_usd`, or 0; `reported_cost_usd = None`; `unpriced_tokens` = the model's `total_tokens` when its `cost_usd` is null.
  - **Cache**: `cache_reported` = whether `input_token_details` has a `cache_read` key; `cache_unpriced_tokens` = its cache-read tokens; `cache_saving_usd = 0`.
  - **Dimensions**: `occurred_at = run.created_at`; person, `repo_id`, `trigger_type` and `session` copied from the run; the provider slug parsed from `run.agent_model` (`slug:model`) when the record's model equals that spec's model, else blank.
  - **Runs with no per-model split**: one record with `model="unknown"` from the run's totals.

  Model names are repaired by the migration's own frozen copy of `collapse_repeated_model_name`, with no import from `automation`. The reverse deletes rows with `task=""` and `reported_cost_usd IS NULL`. Test the forward function in tests/unit_tests/sessions/test_migrations.py: a two-model run gives two rows; an unpriced model counts its tokens as unpriced; a doubled model name is repaired; the provider is set only for the main model.

### Accounting by provider, and recording as calls finish

- [X] T009 [P] In daiv/automation/agent/base.py, make `get_model` pass `metadata={"daiv_provider_slug": resolved.row.slug, "daiv_provider_name": resolved.row.display_name}` into the model kwargs, merged with any `metadata` the caller gave, for both `ChatOpenRouter` and `init_chat_model`. Test in tests/unit_tests/automation/agent/test_base.py that the built model's `metadata` carries both keys for an Anthropic and an OpenRouter `Provider` row.
- [X] T010 Extend `CostAwareUsageMetadataCallbackHandler` in daiv/automation/agent/usage_tracking.py (research R2, R3, R14, R15; data-model "In-memory accounting"):
  - **Provider of a call**: `on_chat_model_start` stores `run_id → (metadata["daiv_provider_slug"], metadata["daiv_provider_name"])`, falling back to `("", "")`. `on_llm_end` pops it.
  - **Ledger entry per `(provider_slug, model)`**: provider name; input, cache read, cache write, output, reasoning and total tokens; `reported_cost`; `estimated_cost`; `unpriced_tokens`; `cache_reported` (true once any call's `input_token_details` had a `cache_read` key); `cache_saving`; `cache_unpriced_tokens`.
  - **Cost per call**: the reported charge goes to `reported_cost`, a list-price cost to `estimated_cost`, and a call with neither adds its tokens to `unpriced_tokens`.
  - **Cache saving per call**: `cache_read × (input rate − cache-read rate) / 1e6`, from genai-prices' `input_mtok` and `cache_read_mtok` at the call's input size through `_resolve_mtok_rate`. It is computed for reported calls too. Cache-read tokens with either rate missing go to `cache_unpriced_tokens`.
  - **`on_update`**: an optional constructor argument, called once after each accounted call, outside the lock.
  - **Unchanged**: `usage_metadata`, `cost_by_model`, `priced_by_model` and `build_usage_summary`, so `Run` fields keep today's rule.
  - **`track_usage_metadata`**: takes and passes through `on_update`.

  Tests in tests/unit_tests/automation/agent/test_usage_tracking.py:
  - the same model through two providers makes two ledger entries;
  - reported, estimated and unpriced calls of one model split correctly;
  - the cache saving matches hand arithmetic for a priced Anthropic model;
  - a model without a cache-read rate fills `cache_unpriced_tokens`;
  - `cache_reported` stays false for calls with no `cache_read` key;
  - `on_update` fires once per call;
  - `build_usage_summary` output is unchanged.
- [X] T011 Write `RunSpendRecorder` in daiv/sessions/spend_recording.py (research R2). It is built with the run id and the handler, and opens with `async with`.
  - **On enter**: it captures the running loop, sets the handler's `on_update` to `loop.call_soon_threadsafe(event.set)`, and starts one writer task.
  - **Each write**: the writer waits on the event, clears it, and snapshots the ledger and `build_usage_summary`. In one transaction it `aupdate_or_create`s each `(run, provider_slug, model)` record with absolute values. Each record copies `occurred_at`, person, `repo_id`, `trigger_type` and `session` from the `Run` row, read once on enter. The same transaction updates the `Run` fields with `usage_field_updates`.
  - **On exit**, by any path including `CancelledError`: a final `asyncio.shield`ed write, then the writer stops.
  - **On a failed write**: `logger.exception` with the run id, and the run continues (FR-002).

  Tests in tests/unit_tests/sessions/test_spend_recording.py (`transaction=True`):
  - two calls give records equal to the handler's totals, and the `Run` fields filled before exit;
  - a burst of calls coalesces;
  - a block that raises after one call keeps that call's spend;
  - a cancelled block runs the final write;
  - a write that raises is logged at ERROR and not re-raised.
- [X] T012 In `_agent_run`, daiv/sessions/executor/run.py (line 485), pass the handler from `track_usage_metadata()` into `RunSpendRecorder(spec.run_id, usage)` and enter it inside the same `with`, so every exit path closes it. Skip the recorder when `spec.run_id` is None. Test in tests/unit_tests/sessions/executor/test_run.py: a run whose agent raises after two fake model calls ends FAILED with its `SpendRecord` rows and `Run.total_tokens` set.
- [X] T013 [P] In `finalize_chat_run`, daiv/chat/api/streaming.py, write the usage fields only while the run's `input_tokens` is null, as `Run.sync_from_task_result` already does. Test in tests/unit_tests/chat/api/test_streaming.py that finalizing a run the recorder already wrote leaves its figures unchanged.

### Shared reading helpers

- [X] T014 Write `SpendLedger` in daiv/sessions/spend_ledger.py:
  - **Person keys** (research R9): `person_key(record_or_values)` → `u<id>`, `x<provider>:<uid>` or `daiv`; `person_filter(key)` → the matching `Q`; `person_label(values)`, which returns a deleted user's `person_name` and marks it deleted.
  - **Rows**: `rows(user, scope, start, end)` returns `visible_to(user)` (narrowed by `started_by` for `scope="mine"`) between local midnight of `start` and local midnight after `end`, ordered by `("occurred_at", "id")`.
  - **Cursor**: `encode_cursor(occurred_at, id)` and `decode_cursor(text)`, opaque URL-safe base64 JSON. A malformed cursor raises `ValueError`.

  Tests in tests/unit_tests/sessions/test_spend_ledger.py: key round trips for all three kinds; an external key never matches a DAIV user of the same username; a deleted user's label; rows ordering and scope; cursor round trip and rejection.

**Checkpoint**: every run and chat run writes `SpendRecord` rows as calls finish; history is backfilled.

---

## Phase 3: User Story 1 – An administrator sees where the money went (Priority: P1) 🎯 MVP

**Goal**: the **Insights → Spend** page for admins, laid out as in the mockup and [contracts/spend-page.md](contracts/spend-page.md).

**Independent Test**: seed runs across three people, two repositories, three trigger types, two models and two providers over 60 days. Open the page for 30 days as an admin. Every figure equals the hand-computed sum, every breakdown adds up to the total, and every linked row opens the sessions behind it.

- [X] T015 [P] [US1] In daiv/dashboard/insights.py, give `_change` a keyword `better: Literal["up", "down"] | None = None` and add `tone` to its result: `"good"` when the move matches `better`, `"bad"` when it opposes it, `"neutral"` when flat or when `better` is None (research R16). Existing callers stay unchanged. Add daiv/dashboard/templates/dashboard/_change_badge.html: a pill with the arrow and amount coloured by tone (good `status-clear`, bad `status-fail`, neutral `text-muted`, each on a weak tint), then `· was …` in `text-faint`. Tests in tests/unit_tests/dashboard/test_insights.py cover each tone, and that `better=None` keeps today's output.
- [X] T016 [US1] Write `SpendReport` in daiv/dashboard/spend.py, built with `(user, window, previous, scope)` over `SpendRecord.objects.visible_to(user)`, narrowed for `scope="mine"`. Add `totals()` for the window and the previous window, in one aggregate query each. It returns:
  - cost; tokens by kind (uncached input = input − cache read − cache write);
  - the prompt cache hit rate over records with `cache_reported`, or None for "not reported";
  - the cache saving, plus a floor flag when `cache_unpriced_tokens > 0`;
  - runs with spend (distinct `run`);
  - average cost per run, and average cost per run that changed code (`Run.code_changes` on a SUCCESSFUL run). Each is run spend (`task=""`) divided by the runs that recorded spend; background spend is excluded from both (FR-005);
  - reported, estimated and unknown-source cost, and unpriced tokens.

  The changes use `_change(better=…)`: cost, tokens and both averages `"down"`, hit rate `"up"`, runs None. Tests in tests/unit_tests/dashboard/test_spend.py check every figure against seeded rows, the previous-window comparison, and the floor flags.
- [X] T017 [US1] Add `chart()` to `SpendReport`. One query groups the window by `window.trunc("occurred_at")` and model; the five costliest models in the window get segments and the rest becomes **Other**, built with `insights.stacked_chart`. A second query groups by bucket, with `Count("run", distinct=True)` and the cache sums, for each column's hover card (R18).
  - **Card content**: the label, the total, per-model lines in legend order (name, swatch, cost, share), runs, the hit rate, and `side` (`left` for columns in the first half, `right` for the rest).
  - **Today**: no chart.
  - **Links**: each column links with `spend_from`/`spend_to`.

  Tests: bucket totals add up to the window's cost; the top-five and **Other** folding; hover lines add up to the column; the side placement.
- [X] T018 [US1] Add `breakdowns()` to `SpendReport` (FR-007, R19). There are five of them: person (by person key), repository, **Started by** (`trigger_type`, then **Background** with one sub-row per `SpendTask`), model and provider (by `provider_slug`, showing the stored `provider_name`, with blank as **Unknown provider**).
  - **Query**: one per breakdown, ranked by cost, limited to 101 rows, with cache sums.
  - **Row**: cost, tokens, share, distinct runs, and the hit rate with its tone: `≥ 85` good, `70–84` neutral, `< 70` attention. These are module constants `CACHE_GOOD = 85` and `CACHE_FAIR = 70`. With no reported records the tone is "not reported".
  - **Folding**: the top 5 rows plus a **Rest** row equal to the total minus those five; rows 6–100 marked hidden; a `more_than_shown` count when a 101st row exists. **Started by** is never folded.
  - **Past 100 rows**: the expanded list ends with "N more not shown". T037 turns this line into a link to the CSV export.
  - **Links**: each row links with `scope=all`, the window and its `spend_*` param. **Rest**, the **DAIV** person row, the `memory_consolidation` sub-row, a batch-title row (a title record with no session) and **Unknown provider** get no link, and a `why` text.

  Tests: each breakdown sums to the total; the threshold boundaries 85 and 70; "not reported"; the **Rest** arithmetic; the 101-row cap; links and no-link reasons.
- [X] T019 [US1] Add `coverage()` to `SpendReport`: runs in the window with no `SpendRecord`, runs RUNNING now, and the names of models with `unpriced_tokens > 0`. Test it, then add a test that the whole report with all five breakdowns runs in at most 11 queries (`django_assert_max_num_queries(11)`, research R8).
- [X] T020 [US1] Add `InsightsSpendView(LoginRequiredMixin, TemplateView)` to daiv/dashboard/views.py and `path("insights/spend/", …, name="insights_spend")` to daiv/dashboard/urls.py.
  - **Period**: defaults to `30d` through `insights.PERIOD_DAYS`, falling back on an invalid value.
  - **Scope**: `all` for admins and `mine` for members by default, falling back on an invalid value.
  - **Nav**: in daiv/dashboard/context_processors.py, add `insights_spend` to the `insights` section of `_MAIN_SECTION_URL_NAMES`.

  Tests in tests/unit_tests/dashboard/test_views.py: anonymous users are redirected; the defaults per role; the invalid-param fallbacks; the section highlight.
- [X] T021 [US1] Templates in daiv/dashboard/templates/dashboard/, following DESIGN.md and the mockup:
  - **daiv/dashboard/templates/dashboard/_insights_nav.html**: a segmented nav, **Activity** (`insights`) and **Spend** (`insights_spend`), with `aria-current` on the current one. Include it in insights.html's header too.
  - **daiv/dashboard/templates/dashboard/insights_spend.html**: the header and nav; the toolbar (period and scope segmented navs); six tiles, three per row, using `_change_badge.html`; the floor `+`; the saving line under the hit rate; the comparison note; **Behind the totals**; the chart; five breakdowns; the cache-tag legend line; the coverage note. When the window is empty it shows "Nothing was spent in this period" alone.
  - **daiv/dashboard/templates/dashboard/_spend_split.html**: one **Behind the totals** panel, with its title, total, one-sentence explanation, split bar, and a row per part with swatch, label, amount, share and meaning. The cost panel adds its amber unpriced note.
  - **daiv/dashboard/templates/dashboard/_spend_breakdown.html**: one table with share bars and cache tags. Its **Show all N {people|repositories|models|providers}** / **Show the top 5 only** toggle is a full-width `<button>` driving Alpine `x-show` on the hidden rows.
  - **daiv/dashboard/templates/dashboard/_stacked_bars.html**: an optional hover card per column, rendered only when the column carries `lines`, so **Activity**'s chart output is unchanged. The card is server-rendered with `role="tooltip"` and `aria-describedby` on the column link, and is shown by Alpine on `mouseenter`/`focus` and hidden on `mouseleave`/`blur`.

  Use `{% comment %}` for multi-line template comments. Run `uv run --only-group=dev djade --target-version 6.0` on each new template.
- [X] T022 [P] [US1] Add reusable classes to daiv/static_src/css/input.css, not long Tailwind chains, using the semantic tokens: `.change-badge` with `--good`/`--bad`/`--neutral`; `.cache-tag` with `--good`/`--fair`/`--attention`/`--unreported`; `.split-bar` and `.spend-split__part`; `.chart-card`.
- [X] T023 [US1] Rendering tests in tests/unit_tests/dashboard/test_views.py for `insights_spend` as an admin:
  - the six tiles and their badge tones;
  - both panels;
  - the hover card markup on columns, and none on **Activity**;
  - the hidden rows and toggle for a breakdown with more than five rows, and no toggle on **Started by**;
  - no-link rows carry their reason;
  - the empty-window message.

**Checkpoint**: an admin answers who, where and on what from one page (SC-001).

---

## Phase 4: User Story 2 – A member sees what their own work costs (Priority: P1)

**Goal**: the same page, opening on the member's own spend and limited to what they may see (FR-011 to FR-013).

**Independent Test**: two members, one shared and one private repository. The first member opens on **Mine** with only their runs. **Everyone** adds the shared repository and nothing from the private one.

- [X] T024 [US2] In daiv/dashboard/templates/dashboard/insights_spend.html, under **Mine**, add a line saying the figures cover only the runs you started and the background work that served them (User Story 2, scenario 1). Rename the subtitle for **Everyone** to "What DAIV spent across the repositories you can see."
- [X] T025 [US2] Visibility tests in tests/unit_tests/dashboard/test_spend.py and tests/unit_tests/dashboard/test_views.py, as a member under both scopes. No figure, breakdown row, person name, repository name, hover-card line or coverage-note model comes from the private repository (FR-013, SC-006). **Mine** equals `started_by`, and **Everyone** equals `visible_to`.

**Checkpoint**: both P1 stories work; this is the MVP.

---

## Phase 5: User Story 3 – Anyone finds the most expensive sessions (Priority: P2)

**Goal**: the top-sessions list, exact drill-down filters, and a cost sort on the sessions list (FR-008, FR-009, FR-014, FR-015).

**Independent Test**: seeded sessions with known costs. The page's ten are the ten costliest visible sessions in order, and sorting the list by cost gives the same order.

- [X] T026 [US3] Add `top_sessions()` to `SpendReport`. One query groups the window's run-spend records (`task=""`, the visible set, session-less rows excluded) by session. It returns the ten largest by run spend (FR-009, research R9), with each session's title, origin, repository, owner, models and distinct runs in the window. Add the **Most expensive sessions** section to insights_spend.html. Each row links to `session_detail`, and "Sort sessions by cost" links to `session_list` with `sort=cost`, `scope=all` and the window's `spend_from`/`spend_to`. Tests in tests/unit_tests/dashboard/test_spend.py check the order, the window limit, the exclusion of background spend, and visibility. The query budget stays at 11 (T019).
- [X] T027 [P] [US3] In daiv/sessions/filters.py, add the `SessionFilter` filters `spend_from`, `spend_to` (DateFilter), `spend_person`, `spend_repo`, `spend_model`, `spend_provider` (CharFilter), `spend_trigger` (`SessionOrigin` choices) and `spend_task` (`SpendTask` choices).
  - **Combining them**: in `filter_queryset`, when any `spend_*` value is present, combine them into one `Exists(SpendRecord.objects.filter(session=OuterRef("pk"), …))`, with `spend_person` going through `SpendLedger.person_filter`.
  - **Scope**: add the eight names to `DEEP_LINK_PARAMS`.
  - **Sort**: add `sort` (`recent`, `cost`).

  Tests in tests/unit_tests/sessions/test_filters.py:
  - a session whose window-run uses model A and whose other run uses model B is excluded by `spend_from/to` plus `spend_model=B`;
  - each param alone;
  - an invalid value is dropped (`strict=False`);
  - a spend param makes the scope default to `all`.
- [X] T028 [US3] In `SessionListView`, daiv/sessions/views.py:
  - **Session cost**: always annotate `run_spend`, a `Subquery` summing `cost_usd` over the session's `SpendRecord` rows with `task=""`, limited to `spend_from`/`spend_to` when the list carries them (research R9).
    - Each row's cost shows `run_spend`. Replace `session_tags.session_cost`, which summed `Run.cost_usd`, in the row template, and delete the tag if nothing else uses it.
    - `sort=cost` orders by `F("run_spend").desc(nulls_last=True)`, then `-last_active_at`. `recent` keeps today's order.
  - **Context**: the distinct people, models and providers from `SpendRecord.objects.visible_to(user)`, for the widgets.
  - **Filter bar** (in daiv/sessions/templates/sessions/): **Person**, **Model** and **Provider** selects, a **Sort** control, and clearable chips for each `spend_*` param (person by name, repository by path, provider by stored name).
  - **Pagination** keeps `sort`.

  Tests in tests/unit_tests/sessions/test_views_list.py:
  - the cost order with nulls last;
  - with `spend_from`/`spend_to`, only spend in that period counts, and the order matches `SpendReport.top_sessions()` for the same window (US3 Independent Test);
  - a run with one unpriced call still adds its priced part;
  - background spend is not counted;
  - the options exclude invisible people, models and providers;
  - chips render;
  - pagination links keep `sort`.

**Checkpoint**: every breakdown row and chart column opens exactly the sessions behind its figure.

---

## Phase 6: User Story 4 – Spend outside runs is counted (Priority: P2)

**Goal**: background spend for the four tasks in research R4, attributed as its table says (FR-001, SC-004).

**Independent Test**: complete a session so its title, classification and memory extraction run. **Started by → Background** shows one entry per task, attributed to the session's repository and person. The session's own **Spend** section is unchanged.

- [X] T029 [US4] Add `BackgroundSpend` to daiv/sessions/spend_recording.py, a context manager with sync (`with`) and async (`async with`) forms. It is built with `task`, and optional `run`, `session`, `repo_id` and a person (user and external fields, or a `Run`/`Session` to copy them from).
  - **On enter**: it opens its own `track_usage_metadata()`.
  - **On exit**, by any path including an exception in the block: it inserts one `SpendRecord` per ledger entry, with `occurred_at=timezone.now()`, `trigger_type=""` and `task` set, and lets the block's exception propagate. It never upserts.
  - **On a failed write**: `logger.exception`, and the work continues (FR-002).

  Tests in tests/unit_tests/sessions/test_spend_recording.py: one record per provider and model; the block raising after one call still writes it; a write failure is logged and swallowed while the block's own exception still propagates.
- [X] T030 [P] [US4] Wrap the titling call in `generate_title_task` in daiv/sessions/tasks.py with `BackgroundSpend(task=SpendTask.TITLE, session=…, repo_id=repo_id)`, with the person copied from the session. Wrap `generate_batch_title_task` with the batch's first run's person, no session and a blank repository. Tests in tests/unit_tests/sessions/test_tasks.py.
- [X] T031 [P] [US4] Wrap `classify_response_text` in `classify_run_task`, daiv/notifications/tasks.py, with `BackgroundSpend(task=SpendTask.RUN_CLASSIFICATION, run=run)`, which copies the run's session, repository and person. Test in tests/unit_tests/notifications/test_tasks.py.
- [X] T032 [P] [US4] Wrap the extraction call in `extract_observations_task`, daiv/memory/tasks.py, with `BackgroundSpend(task=SpendTask.MEMORY_EXTRACTION, run=run)`. Test in tests/unit_tests/memory/test_extraction_task.py.
- [X] T033 [P] [US4] Wrap the model call in `run_consolidation_round`, daiv/memory/consolidation.py, with `BackgroundSpend(task=SpendTask.MEMORY_CONSOLIDATION, repo_id=repo_id)`, which has no person and so lands on the **DAIV** row. Test in tests/unit_tests/memory/test_consolidation.py.
- [X] T034 [US4] Page test in tests/unit_tests/dashboard/test_spend.py: background records appear under **Started by → Background** split by task, and in each person's row. Consolidation lands on **DAIV**. Every breakdown still adds up to the total. `build_session_spend` for the session is unchanged.

**Checkpoint**: DAIV's totals include every model call it makes.

---

## Phase 7: User Story 5 – Spend rows are exported (Priority: P3)

**Goal**: the CSV export and `GET /api/spend/records` from one row source ([contracts/spend-export-api.md](contracts/spend-export-api.md)).

**Independent Test**: export a seeded period: one row per record, and the `cost_usd` column sums to the page's total. The API with the same range and the same user gives the same rows.

- [X] T035 [P] [US5] Add `SpendRateThrottle(AuthRateThrottle)` to daiv/core/api/throttling.py, fixed at `"60/m"` with the bucket keyed on `user.pk`, as `JobsRateThrottle.get_cache_key` does. Test in tests/unit_tests/core/api/test_throttling.py, clearing the cache first.
- [X] T036 [US5] Add `SpendRowSchema` and `SpendPageSchema{items, next_cursor}` to daiv/sessions/api/schemas.py, with exactly the contract's keys:
  - **Identity**: `occurred_at`, `kind`, `task`, `trigger_type`, `person`, `person_kind`, `repo_id`, `session_id`, `run_id`, `schedule_id`, `model`, `provider`, `provider_name`.
  - **Tokens**: `input_tokens`, `cache_read_tokens`, `cache_write_tokens`, `output_tokens`, `reasoning_tokens`, `total_tokens`.
  - **Cost**: `cost_usd`, `reported_cost_usd`, `unpriced_tokens`, `cost_source`.
  - **Cache**: `cache_reported`, `cache_saving_usd`, `cache_unpriced_tokens`.

  `cost_source` is `reported`, `estimated`, `mixed`, `unknown` or `unpriced`.

  Add `spend_router = Router(auth=[AuthBearer(), django_auth], tags=["spend"])` to daiv/sessions/api/views.py, with `GET /records`, and mount it at `/spend` in daiv/daiv/api.py.
  - **Params**: `from`/`to` required (`to − from` ≤ 92 days), `scope` (default `all`), `cursor`.
  - **Pages**: 500 rows, keyset after the cursor, under `SpendRateThrottle`.
  - **Errors**: 400 with a short `detail` for missing or invalid dates, a reversed range, over 92 days, or a malformed cursor.
  - **Async**: the route is async. `SpendLedger.rows` builds its queryset through `visible_to`, which is SYNC ONLY, so build it inside `sync_to_async`. Then read the page with `[row async for row in qs[:501]]`, where row 501 only tells whether a next page exists.

  Tests in tests/unit_tests/sessions/test_api.py: 401 with no credentials; each 400 case; paging until `next_cursor` is null, stable while a record is rewritten; a member's key sees only visible rows.
- [X] T037 [US5] Add `SpendExportView(LoginRequiredMixin, View)` to daiv/dashboard/views.py, with `path("insights/spend/export.csv", …, name="insights_spend_export")`.
  - **Response**: `StreamingHttpResponse` over `SpendLedger.rows`, for the page's `period` and `scope`, with a header row in the contract's column order, `text/csv; charset=utf-8`, and `Content-Disposition: attachment; filename="daiv-spend-<start>-<end>.csv"`.
  - **Async streaming**: DAIV runs under ASGI, so stream from an async generator over the queryset's `aiterator(chunk_size=2000)`. Build the queryset inside `sync_to_async`, because `visible_to` is SYNC ONLY. A sync iterator would make Django buffer the response and warn.
  - **Page links**: in insights_spend.html, add **Download CSV** to the toolbar. Replace T018's "N more not shown" with "N more in the CSV export", linked here.

  Tests in tests/unit_tests/dashboard/test_views.py: the CSV's `cost_usd` sum equals `SpendReport.totals()` cost for the same params (SC-002); a member's CSV excludes the private repository; the header order.

**Checkpoint**: every story is independently usable.

---

## Phase 8: Polish & Cross-Cutting Concerns

**Docs ship with the change** (constitution VII, `patterns/user-docs.md` rule 1). T038–T041 below are done in parts: each part lands in the pull request of the delivery it describes, as listed under "Incremental delivery". With a single pull request, all parts land together.

- [X] T038 [P] Write docs/features/spend.md, with a front-matter `description`, and add it to the `mkdocs.yml` `nav` and `llmstxt` sections. Follow `patterns/user-docs.md`: second person, sentence-case headings, no dated words. The parts, by delivery:
  - **US1 + US2**: what the page shows, section by section; how to read the badges, cache tags and coverage note; **Mine** and **Everyone**; **Show all**.
  - **US3**: the most expensive sessions, and the link to the cost-sorted list.
  - **US4**: the **Background** row and its four tasks.
  - **US5**: the export, and `GET /api/spend/records` with its params, paging, limits and errors.
- [X] T039 [P] Update docs/reference/monitoring.md, *Usage and cost*, the one home for what figures mean. Keep the `#usage-and-cost` anchor that daiv/dashboard/landing.py links to. The parts, by delivery:
  - **Phase 2**: spend is recorded as each call finishes, so failed, cancelled and killed runs keep it; the provider is recorded with each call.
  - **US1 + US2**: the prompt cache hit rate (by tokens), the estimated cache saving, and how far estimates can be from a bill.
  - **US4**: background spend and its four tasks.
- [X] T040 [P] Update docs/features/sessions.md. Its *Insights* section links to docs/features/spend.md rather than repeating it. The parts, by delivery:
  - **US1 + US2**: **Insights** has **Activity** and **Spend** tabs.
  - **US3**: the sessions list gains the **Person**, **Model** and **Provider** filters, the spend deep-link params, and the cost sort. Each row's cost is now the session's run spend (research R9).
- [X] T041 Add one line per delivery under `Unreleased` in CHANGELOG.md, in Common Changelog form, each linking its pull request:
  - **Phase 2**: Fixed, failed and cancelled runs keep their spend.
  - **US1 + US2**: Added, the Spend page.
  - **US3, US4, US5**: each amends that Added line, since the page was added since the last release.
- [X] T042 [P] Add **Spend record**, **Run spend**, **Background spend**, **Prompt cache hit rate** and **Cache saving** to constitutions/glossary/README.md, one row each, using the spec's Terms. This is a MINOR amendment to a companion document, so state it in the pull request with its impact line for the maintainer's approval (constitution, Governance). Land it with Phase 2, the first delivery to use the words.
- [X] T043 Hand-add the new strings to daiv/dashboard/locale/pt/LC_MESSAGES/django.po and daiv/sessions/locale/pt/LC_MESSAGES/django.po. If `makemessages` is used, revert its changes to other apps' `.po` files.
- [X] T044 Run the gates and record the commands and results:
  - `uv run --only-group=dev ruff check --fix` and `ruff format`, scoped to the changed paths (repo-wide `make lint-fix` can mangle untracked work);
  - djade on the new templates;
  - `make lint`, `make lint-typing` and `make test`;
  - `uv run mkdocs build --strict`.
- [ ] T045 Run quickstart.md steps 2–9 against this worktree's `make up` stack, comparing the page with the mockup. Record the SC-003 timing from step 9, and any step not run, for the pull request.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: none.
- **Foundational (Phase 2)**: needs Setup.
  - T004 → T005 → T006 → T007 → T008.
  - T009 is independent; T010 needs T009's metadata keys.
  - T011 needs T005 and T010; T012 needs T011; T013 is independent.
  - T014 needs T006.
- **User stories (Phase 3+)**: all need Phase 2.
- **Polish (Phase 8)**: T038–T041 are split by delivery (see Implementation Strategy). T042 lands with Phase 2. T043–T045 run before each pull request.

### User Story Dependencies

- **US1 (P1)**: after Phase 2. Within it: T015 and T022 can start at once; T016 → T017 → T018 → T019; T020 → T021 → T023.
- **US2 (P1)**: needs US1's page (T020, T021). It adds scope wording and visibility tests.
- **US3 (P2)**: T026 needs US1's report. T027 and T028 need only Phase 2 and can run beside US1.
- **US4 (P2)**: needs only Phase 2 to write records. T034 needs US1's breakdowns.
- **US5 (P3)**: T035 and T036 need only Phase 2 (T014). T037 needs US1's view and report.

### Within Each Story

Write each test first and see it fail, then implement. Do the model and queryset before the report, the report before the view, and the view before the templates.

---

## Parallel Examples

```text
Phase 2, after T008:
  T009 get_model provider tag (automation/agent/base.py)
  T013 finalize_chat_run guard (chat/api/streaming.py)
  T014 SpendLedger (sessions/spend_ledger.py)

US1, at start:
  T015 _change tone + _change_badge.html (dashboard/insights.py)
  T022 CSS classes (static_src/css/input.css)
  T016 SpendReport.totals (dashboard/spend.py)

US4, after T029:
  T030 titling (sessions/tasks.py)
  T031 run classification (notifications/tasks.py)
  T032 memory extraction (memory/tasks.py)
  T033 memory consolidation (memory/consolidation.py)

Beside US1, after Phase 2:
  T027 SessionFilter spend_* (sessions/filters.py)
  T035 SpendRateThrottle (core/api/throttling.py)
  T036 spend API (sessions/api/)
```

---

## Implementation Strategy

### MVP first (both P1 stories)

1. Phases 1 and 2. Every run now records spend as it goes, which already fixes the lost spend of failed runs on the existing session pages.
2. Phase 3 (US1), then Phase 4 (US2).
3. **Stop and validate**: quickstart steps 2, 4, 5 and 6, and the docs for the page (T038–T040 for what shipped).

### Incremental delivery

Each delivery is one pull request carrying its own docs and changelog line (constitution VII). The parts of T038–T041 are named by delivery:

1. **Phase 2 alone**, with T039 (Phase 2 part), T041 (Fixed) and T042. No page yet, but runs that fail or are cancelled keep their spend on the existing pages.
2. **US1 + US2**: the Spend page, with T038, T039 and T040 (their US1 + US2 parts) and T041 (Added).
3. **US3**: the top sessions, drill-down filters and cost sort, with T038 and T040 (their US3 parts).
4. **US4**: background spend, with T038 and T039 (their US4 parts).
5. **US5**: export and API, with T038 (its US5 part).

T043–T045 run in every delivery, on the files it touches.
