# Contract: `sessions.inbox` (Python interface)

The only way code outside `sessions.inbox` posts to or drains a session's inbox. Callers are the trigger apps (`chat`, `jobs`, `mcp_api`, `webhooks`), `sessions.pipeline_watch`, `sessions.delegation`, `sessions.executor` and `sessions.signals`. Async functions are the primary interface. A sync wrapper exists only where a sync caller already exists (signal receivers, crontask).

## `apost_event`

```python
async def apost_event(
    thread_id: str,
    *,
    kind: EventKind,
    source_key: str,
    payload: Mapping[str, Any],
    wake: bool,
    subject: str = "",
    ref: str = "",
    merge_request_iid: int | None = None,
    reason: QuietReason | None = None,
    author: User | None = None,
    author_username: str = "",
) -> PostResult
```

- **Idempotent.** A second call with the same `(thread_id, source_key)` inserts nothing and returns `PostResult(event=<existing>, created=False, run_id=<its run id or None>)` (FR-005).
- **Validates** `payload` against the kind's `TypedDict`. An invalid payload raises `ValueError`; the caller has a bug.
- **Busy session.** When `wake` is true and the session is busy, the event joins the session's next run for the event's `ref`: the existing QUEUED run for that branch, or one created with the kind's provisional trigger type. `run_id` is that run's id (R3).
- **Drains on commit** with `transaction.on_commit(adrain)`, so the caller's own transaction commits first. The drain is skipped when the call does not wake the session.
- **Missing session.** Raises `Session.DoesNotExist` when the session doesn't exist. Callers that route through ownership use `aowning_session` first.

`PostResult` is a frozen dataclass: `event: InboxEvent`, `created: bool`, `run_id: UUID | None`.

## `adrain`

```python
async def adrain(thread_id: str) -> Run | None
```

- **Locking.** Locks the session row (`select_for_update`). If the slot is held and fresh, it returns `None`.
- **Otherwise, in one transaction:**
  1. supersedes older pending events per subject;
  2. picks the branch of the oldest surviving waking event (R4);
  3. turns waking events that the policy blocks (mute, consecutive-run limit) into remembered ones (`wake=False`, `reason` set). If no waking event survives, it deletes the QUEUED next run for that branch, detaching its events, and returns `None`;
  4. checks the owner's repository access, marking events `failed/access_denied` if it is denied;
  5. promotes the QUEUED next run for that branch (or creates a READY one), sets `trigger_type` by precedence, and claims `Session.active_run_id = str(run.pk)`;
  6. marks the taken events `taken` with `run=run`;
  7. for a waking `ci_failed`, sets the ownership's `last_examined_sha` and increments `attempts`.
- **After commit**, it enqueues `run_job_task` through `sessions.queue.aenqueue_run`. An enqueue failure releases the slot and returns the events to `pending` (`patterns/errors.md` rule 1: `logger.error`).
- **Return value**: the dispatched run, or `None`.

## `atake_for_chat`

```python
async def atake_for_chat(thread_id: str, *, holder_id: str) -> list[InboxEvent]
```

Called by the chat view right after it claims the slot with `try_claim` (R9). It marks every pending event for the session's current branch `taken` with `claim_token=holder_id`. `alink_chat_run(holder_id, run)` later sets their `run` once the chat `Run` row exists.

## `arender_input`

```python
async def arender_input(run: Run) -> RenderedInput
```

Returns `RenderedInput(context: HumanMessage | None, request: HumanMessage)` per [agent-input.md](agent-input.md). The executor builds `RunSpec.input_messages` from it.

## Completion: `on_run_ended`

A `run_ended` receiver in `sessions/signals.py`, for every run, chat included.

| Run outcome | Events |
|---|---|
| SUCCESSFUL or WAITING_INPUT | `handled`; mention replies posted (R10) |
| Cancelled | `dismissed` |
| FAILED | Back to `pending` with `attempts += 1`, or `failed/retries_exhausted` at 3, with `logger.error` |

Then it calls `adrain(thread_id)`.

Mention replies are posted with `RepoClient.create_merge_request_comment(..., reply_to_id=...)`, or `create_review_comment_reply` on GitHub diff comments. A posting failure is logged with `logger.exception`, and the event stays `handled`: the run's work stands, and the operator can act.

## Ownership

```python
async def aclaim_ownership(*, run: Run, merge_request: MergeRequest | dict, pushed_sha: str) -> MergeRequestOwnership
async def aowning_session(repo_id: str, merge_request_iid: int) -> Session | None
async def aownership_for_branch(repo_id: str, source_branch: str) -> MergeRequestOwnership | None
```

- `aclaim_ownership` applies the [claim rules](../data-model.md#claim-rules-fr-001-applied-after-a-run-publishes). It replaces `PipelineWatch.aarm_after_run` in the executor, and posts `foreign_publish` or `notice` events as those rules require.
- `aowning_session` returns `None` for an unowned or closed merge request. When the owning session was deleted, it creates a session for the same person, with origin `pipeline_webhook` and title from the merge request (FR-004).

## Sweeps (`sessions/tasks.py`)

| Task | Schedule | Does |
|---|---|---|
| `drain_inboxes_cron_task` | `* * * * *` | `adrain` for sessions with pending waking events and a free or stale slot. Returns stale `taken` events to `pending` (R12, R13) |
| `classify_push_task` | Enqueued with `run_after=+60s` | Decides `branch_pushed` against recorded DAIV SHAs (R8) |
| `reconcile_pipeline_watches_cron_task` | `*/10`, existing | Re-reads the latest pipeline for owned, open merge requests with no CI event for 30 min. The 6-hour expiry is removed |
