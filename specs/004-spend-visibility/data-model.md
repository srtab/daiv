# Data Model: Spend Visibility

Decisions behind each field are in [research.md](research.md) (R1–R7).

## `SpendRecord` (new, `sessions` app, table `agent_sessions_spendrecord`)

One model's spend for one run, or for one background task call.

| Field | Type | Notes |
|---|---|---|
| `id` | `BigAutoField` | |
| `occurred_at` | `DateTimeField`, indexed | The run's `created_at` for run spend; the call's time for background spend (R5) |
| `run` | FK `Run`, `SET_NULL`, null, `related_name="spend_records"` | The run that spent it, or that the background task served |
| `session` | FK `Session`, `SET_NULL`, null, `related_name="spend_records"` | The session it spent in or served; null for repository-wide and batch spend |
| `repo_id` | `CharField(255)`, blank | The run's repository, or the one served; blank for a batch title |
| `trigger_type` | `CharField(20)`, `SessionOrigin` choices, blank | The run's trigger type; blank for background spend |
| `task` | `CharField(24)`, `SpendTask` choices, blank | Blank for run spend; the background task otherwise |
| `user` | FK `User`, `SET_NULL`, null, `related_name="spend_records"` | The person (see below) |
| `external_provider` | `CharField(32)`, blank | As on `Run`: the git platform account behind a webhook |
| `external_uid` | `CharField(191)`, blank | |
| `external_username` | `CharField(255)`, blank | Display name for an external person |
| `person_name` | `CharField(255)`, blank | Snapshot of the DAIV user's name at write time, shown when the user is later deleted |
| `model` | `CharField(255)` | Repaired with `collapse_repeated_model_name`; `unknown` only in backfilled rows that had no per-model split |
| `provider_slug` | `CharField(32)`, blank | The `Provider` row's slug when the call was made (R14). Blank = unknown, only in backfilled rows |
| `provider_name` | `CharField(64)`, blank | That row's display name then; what the page shows |
| `input_tokens` | `PositiveBigIntegerField`, default 0 | All input, cached and cache-write included, as LangChain reports it |
| `cache_read_tokens` | `PositiveBigIntegerField`, default 0 | Subset of input |
| `cache_write_tokens` | `PositiveBigIntegerField`, default 0 | Subset of input: cache creation plus 5-minute and 1-hour writes |
| `output_tokens` | `PositiveBigIntegerField`, default 0 | All output, reasoning included |
| `reasoning_tokens` | `PositiveBigIntegerField`, default 0 | Subset of output |
| `total_tokens` | `PositiveBigIntegerField`, default 0 | Input plus output |
| `cost_usd` | `DecimalField(12, 6)`, default 0 | The priced part: reported plus estimated |
| `reported_cost_usd` | `DecimalField(12, 6)`, null | The part the provider reported. `NULL` = split unknown (backfilled rows only) |
| `cost_reported` | `BooleanField`, default False | True when any of the row's calls carried a reported charge, $0 included |
| `unpriced_tokens` | `PositiveBigIntegerField`, default 0 | Tokens of calls with no price; they add nothing to `cost_usd` |
| `cache_reported` | `BooleanField`, default False | True when any of the row's calls reported a cache-read count, zero included (R15) |
| `cache_saving_usd` | `DecimalField(12, 6)`, default 0 | Estimated cost the cache hits avoided, from list prices at write time (R15) |
| `cache_unpriced_tokens` | `PositiveBigIntegerField`, default 0 | Cache-read tokens whose saving could not be priced; any above 0 makes a saving total a floor |
| `updated_at` | `DateTimeField(auto_now=True)` | Last write; run-spend rows are rewritten as calls finish |

**Derived figures** (never stored):
- uncached input = `input_tokens − cache_read_tokens − cache_write_tokens`;
- estimated cost = `cost_usd − reported_cost_usd`, for rows where the split is known;
- cost of unknown source = `cost_usd`, for rows where `reported_cost_usd IS NULL`;
- prompt cache hit rate of a scope = `Σ cache_read_tokens / Σ input_tokens`, over its rows with `cache_reported`; with no such row the rate is **not reported**.

**Who the person is**, in order:
1. `user` set → that DAIV user.
2. `external_provider` and `external_uid` set → that git platform account, marked external.
3. Neither → **DAIV**: work no person started, i.e. repository-wide background spend and runs created for no one (SR2).

`person_name` is only a label for a deleted user: the row then groups by its snapshot name, marked as deleted. It never merges with another person.

### Constraints and indexes

| Name | Definition | Purpose |
|---|---|---|
| `spend_run_model_uniq` | `UniqueConstraint(run, provider_slug, model, condition=Q(task="", run__isnull=False))` | One run-spend row per provider and model per run, so the recorder upserts it. Background rows are never unique: a retried classification is a second charge |
| `spend_occurred_idx` | `(occurred_at)` | Every windowed query, the admin path |
| `spend_user_occurred_idx` | `(user, occurred_at)` | The **Mine** scope and the person breakdown |
| `spend_repo_occurred_idx` | `(repo_id, occurred_at)` | The member visibility predicate and the repository breakdown |
| `spend_session_occurred_idx` | `(session, occurred_at)` | Top sessions, and the sessions-list `Exists` |
| check `spend_kind_check` | `Q(task="") ^ Q(trigger_type="")` | A row is run spend (trigger type, no task) or background spend (task, no trigger type), never both or neither. `Run.trigger_type` is a required field, so every run row has one |

### Manager / queryset (`SpendRecordQuerySet`)

| Method | Meaning |
|---|---|
| `visible_to(user)` | All rows for admins; otherwise the run-visibility predicate (R6) |
| `started_by(user)` | `_started_by_q(user)`: the **Mine** scope |
| `in_window(window)` | `window.date_q("occurred_at")` |
| `run_spend()` / `background()` | `task=""` / `task≠""` |

## `SpendTask` (new `TextChoices`, `sessions.models`)

| Value | Label | Written by |
|---|---|---|
| `title` | Session titles | `generate_title_task`, `generate_batch_title_task` |
| `run_classification` | Run classification | `classify_run_task` |
| `memory_extraction` | Memory extraction | `extract_observations_task` |
| `memory_consolidation` | Memory consolidation | `run_consolidation_round` |

## `Run` (existing, unchanged schema)

- `input_tokens`, `output_tokens`, `total_tokens`, `cost_usd` and `usage_by_model` are now written by `RunSpendRecorder` as calls finish, not once at the end (R2).
- Their meaning doesn't change: `cost_usd` is still `None` when any call was unpriced.
- `sync_from_task_result` and `finalize_chat_run` write them only while `input_tokens` is still null.

## In-memory accounting (`automation.agent.usage_tracking`)

`CostAwareUsageMetadataCallbackHandler` gains:
- a ledger keyed by `(provider_slug, model)`. Each entry holds the provider's name, tokens by kind, reported and estimated cost, unpriced tokens, `cache_reported`, the cache saving and the cache-unpriced tokens;
- a `run_id → (provider_slug, provider_name)` map, filled from the model's `metadata` in `on_chat_model_start` and popped in `on_llm_end` (R14);
- an optional `on_update: Callable[[], None]`, called after each accounted call.

`usage_metadata`, `cost_by_model` and `priced_by_model` keep feeding `build_usage_summary`, so the `Run` fields keep their rule. `automation.agent.base.get_model` puts `daiv_provider_slug` and `daiv_provider_name` in each model's `metadata`.

## Lifecycle of a run-spend row

```text
first accounted call ─▶ created (aupdate_or_create on run, provider_slug, model)
each later call       ─▶ rewritten with the new absolute totals
run ends (any path)   ─▶ final rewrite, then never touched again
run or session deleted ─▶ run/session set NULL; row and its person, repository and figures stay
```

Background rows are written once, when their task's call returns or raises.

## Migrations

1. **Schema** (`sessions` 0037): `SpendRecord`, its constraints and indexes.
2. **Data** (`sessions` 0038): backfills run-spend rows from `Run.usage_by_model` (R7).
   - Batched in 1,000s, with `reported_cost_usd = NULL`.
   - The provider comes from the run's `agent_model` spec when the row's model is that spec's model, and is blank otherwise (R14).
   - `cache_reported` is true when the model's `input_token_details` has a `cache_read` key. No saving is computed: every cache-read token goes to `cache_unpriced_tokens`, so totals over backfilled periods show the saving as a floor.
   - Uses its own frozen copy of the JSON reading and model-name repair.
   - The reverse migration deletes rows with `task = ""` and `reported_cost_usd IS NULL`.
