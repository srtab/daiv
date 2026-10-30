# Data Model: Session Inbox

All models live in `daiv/sessions/models.py` (R1). Field types are Django field types. Every enum is a `TextChoices` backed by a `CheckConstraint`, as the existing `Run` constraints are.

## InboxEvent (new)

Something that happened which concerns one session.

| Field | Type | Notes |
|---|---|---|
| `id` | UUID, primary key | |
| `session` | FK → `Session`, CASCADE, `related_name="inbox_events"` | The session it is delivered to |
| `kind` | Char(32), `EventKind` | See [Kinds](#kinds) |
| `source_key` | Char(255) | Unique per source, e.g. `pipeline:830724`, `note:5521`, `batch:<uuid>`, `chat:<agui message id>`, `job:<uuid>`, `push:<after sha>` |
| `subject` | Char(255), blank | Key for replacing older events, e.g. `ci:9`, `mr-state:9`, `branch:chore/x`. Blank means never replaced |
| `ref` | Char(255), blank | The branch the event concerns. Blank means any run may take it (R4) |
| `merge_request_iid` | PositiveInteger, null | |
| `wake` | Bool | Fixed when the event is posted |
| `reason` | Char(32), `QuietReason`, blank | Why a CI failure was only remembered: `not_daiv_commit`, `already_examined`, `budget_spent`, `watch_off`, `muted`, `run_limit` |
| `payload` | JSON | Kind-specific, validated by `inbox.kinds` (see [Payloads](#payloads)) |
| `author` | FK → User, SET_NULL, null | Who caused it, for attribution. Runs always act as the session owner |
| `author_username` | Char(255), blank | The platform username when there is no DAIV user |
| `state` | Char(16), `EventState` | See [States](#states) |
| `run` | FK → `Run`, SET_NULL, null, `related_name="inbox_events"` | The queued next run it joined, or the run that took it |
| `claim_token` | Char(64), blank | The chat holder id between taking the event and the chat `Run` row existing (R9) |
| `attempts` | PositiveSmallInteger, default 0 | Runs that took it and failed |
| `failure` | Char(32), `EventFailure`, blank | `access_denied`, `retries_exhausted`, `session_gone`. A label, never exception text |
| `created_at` | DateTime, default now | Arrival time; also the ordering |
| `updated_at` | DateTime, auto_now | |

**Constraints**:
- `UniqueConstraint(session, source_key)`: the de-duplication of FR-005, enforced on insert with `ON CONFLICT DO NOTHING` (`bulk_create(ignore_conflicts=True)`).
- `CheckConstraint` on `kind`, `state`, `reason` and `failure`.

**Indexes**:
- `(session, state, created_at)`.
- A partial index on `(session)` where `state = 'pending' AND wake`, for the drain sweep (R13).
- A partial index on `(run)` where `run IS NOT NULL`.

### Kinds

| Kind | Wakes | Subject | Precedence (R6) | Run's trigger type |
|---|---|---|---|---|
| `chat_message` | yes | — | 1 | `chat` |
| `mention` | yes | — | 2 | `mr_webhook` |
| `follow_up_job` | yes | — | 3 | `api_job` / `mcp_job` (from payload) |
| `delegation_finished` | yes | — | 4 | `delegation_resume` |
| `ci_failed` | per R7 | `ci:<iid>` | 5 | `pipeline_webhook` |
| `ci_passed` | no | `ci:<iid>` | — | — |
| `ci_stopped` | no | `ci:<iid>` | — | — (status needs a person: blocked, manual, canceled, skipped with jobs) |
| `branch_pushed` | no | `branch:<ref>` | — | — |
| `foreign_publish` | no | `branch:<ref>` | — | — |
| `mr_merged` / `mr_closed` | no | `mr-state:<iid>` | — | — |
| `comment` | no | — | — | — (comment on an owned merge request without a mention) |
| `delegated_ci_outcome` | no | `child-ci:<child thread>:<iid>` | — | — |
| `notice` | no | — | — | — (system notes: ownership moved, run limit reached) |

### Payloads

Each kind has a `TypedDict`, validated in `inbox.kinds`. Payloads hold what the renderer and the timeline need, and never credentials.

- `ci_*`: `pipeline_id`, `sha`, `status`, `web_url`, `failed_jobs`, a list of names.
- `mention`, `comment`: `note_id`, `discussion_id` (GitLab), `review_comment_id` (GitHub, diff), `body`, `web_url`, `is_diff`.
- `branch_pushed`: `before`, `after`, `commit_count`, `pusher_username`.
- `foreign_publish`: `thread_id`, `owner_username`, `sha`.
- `mr_merged` / `mr_closed`: `target_branch`, `by_username`.
- `follow_up_job`: `prompt`, `trigger_type`, `ref`, `agent_model`, `agent_thinking_level`, `references`.
- `chat_message`: `text`, `agui_message_id`, `agent_model`, `agent_thinking_level`, attachment references (no file content).
- `delegation_finished`: `batch_id`, `summary` (the existing `render_batch_summary`).
- `delegated_ci_outcome`: `child_thread_id`, `merge_request_iid`, `outcome` (`passing`, `stopped_after_attempts`, `no_push`), `web_url`.

### States

```text
pending ──take──▶ taken ──run SUCCESSFUL / WAITING_INPUT──▶ handled
   │                │ ──run cancelled──────────────────────▶ dismissed
   │                │ ──run FAILED, attempts < 3──▶ pending (attempts + 1)
   │                │ ──run FAILED, attempts = 3──▶ failed (retries_exhausted, logger.error)
   │                └─slot stale, run never ended (sweep)──▶ pending (attempts + 1)
   ├──newer event, same subject, at take time──▶ superseded
   └──owner lost repository access at take time──▶ failed (access_denied)
```

- `pending` events with `run` set have joined a QUEUED next run (R3). Taking them keeps the same `run`.
- Remembered events (`wake=False`) follow the same path. They are taken by whichever run starts next for their branch.

## MergeRequestOwnership (new)

Which session owns a merge request DAIV published, and that merge request's CI watch state (R7, R15).

| Field | Type | Notes |
|---|---|---|
| `id` | BigAuto, primary key | |
| `repo_id` | Char(255) | |
| `merge_request_iid` | PositiveInteger | |
| `session` | FK → `Session`, SET_NULL, null, `related_name="owned_merge_requests"` | Null once the session is deleted (FR-004) |
| `owner` | FK → User, SET_NULL, null | The owning person. Null for an ownerless session |
| `source_branch` | Char(255) | Updated from the merge request on each claim |
| `watch_enabled` | Bool, default True | The per-merge-request toggle (`ci-watch` endpoint) |
| `attempts` | PositiveSmallInteger, default 0 | CI-woken runs charged since the last reset (FR-016) |
| `last_daiv_sha` | Char(64), blank | The head DAIV last pushed, by any session |
| `last_examined_sha` | Char(64), blank | The commit the last CI-woken run examined |
| `last_pipeline_id` | BigInteger, null | The last pipeline judged, so a redelivery is skipped |
| `closed_at` | DateTime, null | Set on merge or close; the watch stops observing |
| `created_at`, `updated_at` | DateTime | |

**Constraints**: `UniqueConstraint(repo_id, merge_request_iid)`.

**Index**: on `(repo_id, source_branch)` where `closed_at IS NULL`, for pipeline and push lookups by branch.

### Claim rules (FR-001), applied after a run publishes

| Current state | Publishing session | Result |
|---|---|---|
| No row | any | Create the row: owner = the publishing session and its user |
| Owned, same person (or both ownerless) | any session of that person | Ownership moves to the publishing session |
| Owner session has no user | session with a user | Ownership moves; a `notice` event goes to the old session |
| Owned by person A | a session of person B | Ownership doesn't move; a `foreign_publish` event goes to A's session |
| Owner session deleted (`session` null) | any | Treated as "no row" if it is the same person; otherwise as person B |

Every claim sets `last_daiv_sha = Run.pushed_sha`. A claim by a run started by a person (`chat`, `mr_webhook`, `api_job`, `mcp_job`, `ui_job`, `issue_webhook`, `schedule`) resets `attempts` to 0.

### CI wake rule (FR-015), evaluated when a `ci_failed` event is posted

`wake` is true only when all of these hold:
1. `watch_enabled`, the site switch, the repository config, and `closed_at IS NULL`;
2. `not session.inbox_muted`;
3. the pipeline is the merge request's head pipeline, and `pipeline.sha == last_daiv_sha`. A blank `last_daiv_sha` only occurs on a row migrated from an open watch, and counts as a match;
4. `pipeline.sha != last_examined_sha`;
5. `attempts < max_attempts`, the existing site/repository cap;
6. the consecutive-run limit is not reached (FR-033).

The first condition that fails becomes the event's `reason`.

When the drainer dispatches a run that takes a waking `ci_failed`, it sets `last_examined_sha` and increments `attempts` in the same transaction.

## Session (changed)

| Field | Change |
|---|---|
| `inbox_muted` | New Bool, default False. Mutes system-started runs (FR-031) |
| `watch_state`, `watch_attempts`, `watch_pipeline_id`, `watch_armed_at` | No longer written. Dropped in the next release (Complexity Tracking) |

## Run (changed)

| Field | Change |
|---|---|
| `pushed_sha` | New Char(64), blank. The head DAIV pushed in this run's publish (R7) |
| `trigger_type` | Unchanged values. Set from event precedence when an inbox run is dispatched (R6) |
| `continuation_of_batch_id` | No longer written for new continuations (R19). The column and its constraint stay for history |

The QUEUED "next run" (R3) is a normal `Run` with `status=QUEUED` and `task_result=None`. Add `UniqueConstraint(session)` on `status='QUEUED' AND trigger_type IN <inbox triggers>`, so a session has at most one next run.

Delegated job legs, which are QUEUED on their own new sessions with `waits_for`, use `trigger_type=delegated_job`. That is outside the inbox set, so the constraint doesn't apply to them.

## Migrations

1. **Schema**: create `InboxEvent` and `MergeRequestOwnership`; add `Session.inbox_muted`, `Run.pushed_sha` and the next-run constraint.
2. **Data**: for each session with `watch_state IN ('watching', 'fixing')` and a `merge_request_iid`, create an ownership row:
   - `session` = that merge request's own thread session;
   - `owner` = its `user`;
   - `attempts` = `watch_attempts`;
   - `source_branch` = `ref`;
   - `last_pipeline_id` = `watch_pipeline_id`.

   `last_daiv_sha` is left blank: a migration can't call the platform, and the wake rule treats a blank value as a match, so in-flight watches keep waking until DAIV next publishes. The migration is reversible as a no-op: the old columns are kept.
3. **Next release**: drop `Session.watch_*`, and delete `sessions/pipeline_watch/store.py`'s remaining readers.

## Glossary additions

These go in `constitutions/glossary/README.md`:

| Term | Meaning | Not |
|---|---|---|
| Inbox event | Something that happened which concerns a Session: a CI result, a mention, a queued chat message, a follow-up job, a delegated batch finishing (`InboxEvent`). It wakes the agent or is only remembered | message, notification |
| Owning session | The Session that most recently published a merge request, among the sessions of the person who first published it (`MergeRequestOwnership`) | MR thread |
| Next run | The one QUEUED Run that waking events join while their Session is busy | queued job |
