# Contract: `sessions.inbox` (Python interface)

The only way code outside `sessions.inbox` posts to or drains a session's inbox. Callers are the trigger apps (`chat`, `jobs`, `mcp_api`, `webhooks`), `sessions.pipeline_watch`, `sessions.delegation`, `sessions.executor` and `sessions.signals`. Async functions are the primary interface. A sync wrapper exists only where a sync caller already exists (signal receivers, crontask).

## `Inbox.apost`

```python
async def Inbox(thread_id).apost(
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
- **Drains on commit** with `transaction.on_commit(Drain(thread_id).start_logged)`, so the caller's own transaction commits first. The drain is skipped when the call does not wake the session.
- **Missing session.** Raises `Session.DoesNotExist` when the session doesn't exist. Callers that route through ownership use `Ownerships.afind_open` first.

`PostResult` is a frozen dataclass: `event: InboxEvent`, `created: bool`, `run_id: UUID | None`.

## `Drain.astart`

```python
async def Drain(thread_id).astart() -> Run | None
```

- **Locking.** Locks the session row (`select_for_update`). If the slot is held and fresh, it returns `None`.
- **Otherwise, in one transaction:**
  1. supersedes older pending events per subject;
  2. picks the branch of the oldest surviving waking event (R4);
  3. turns waking events that the policy blocks (the consecutive-run limit) into remembered ones (`wake=False`, `reason` set). If no waking event survives, it deletes the QUEUED next run for that branch, detaching its events, and returns `None`;
  4. checks the owner's repository access, marking events `failed/access_denied` if it is denied;
  5. promotes the QUEUED next run for that branch (or creates a READY one), sets `trigger_type` by precedence, and claims `Session.active_run_id = str(run.pk)`;
  6. marks the taken events `taken` with `run=run`;
  7. for a waking `ci_failed`, sets the ownership's `last_examined_sha` and increments `attempts`.
- **After commit**, it enqueues `run_job_task` through `sessions.queue.aenqueue_run`. An enqueue failure releases the slot and returns the events to `pending` (`patterns/errors.md` rule 1: `logger.error`).
- **Return value**: the dispatched run, or `None`.

## `ChatClaim.atake`

```python
async def ChatClaim(thread_id, holder_id).atake() -> list[InboxEvent]
```

Called by the chat view right after it claims the slot with `try_claim` (R9). It supersedes older events per subject, then marks the pending **remembered** events for the session's current branch `taken` with `claim_token=holder_id`. `ChatClaim.alink(run)` later sets their `run` once the chat `Run` row exists. When the turn ends before that, the events go back to `pending`.

Waking events are left `pending` for the drain, which starts their run once the chat turn ends: only the drain renders a waking event's request, charges a CI attempt, and serves one requester per run. This narrows FR-010's "every pending event" for an idle chat turn; the waking events still reach the agent in the very next run.

## `RunInput.aof_run` and `RunInput.render`

```python
async def RunInput.aof_run(run: Run) -> RunInput
def RunInput.render(self, run: Run, *, fast_forward: FastForward | None = None) -> RenderedInput
```

`RunInput.aof_run(run)` loads the events the run took. `render` returns `RenderedInput(context: HumanMessage | None, request: HumanMessage)` per [agent-input.md](agent-input.md). The executor builds `RunSpec.input_messages` from it.

## Completion: `RunEnd.on_run_ended`

A `run_ended` receiver in `sessions/signals.py`, for every run, chat included.

| Run outcome | Events |
|---|---|
| SUCCESSFUL or WAITING_INPUT | `handled`; mention replies posted (R10) |
| Cancelled | `dismissed` |
| FAILED | Back to `pending` with `attempts += 1`, or `failed/retries_exhausted` at 3, with `logger.error` |

Then it calls `Drain(thread_id).start_logged`.

Mention replies are posted with `RepoClient.create_merge_request_comment(..., reply_to_id=...)`, or `create_review_comment_reply` on GitHub diff comments. A posting failure is logged with `logger.exception`, and the event stays `handled`: the run's work stands, and the operator can act.

## Ownership

```python
async def Ownerships.aclaim(*, run_id: UUID | str, merge_request: MergeRequest | dict, pushed_sha: str) -> MergeRequestOwnership | None
async def Ownerships(repo_id).afind_open(merge_request_iid: int, *, title: str = "") -> MergeRequestOwnership | None
async def Ownerships(repo_id).afor_branch(source_branch: str) -> MergeRequestOwnership | None
```

- `Ownerships.aclaim` applies the [claim rules](../data-model.md#claim-rules-fr-001-applied-after-a-run-publishes). It replaces `PipelineWatch.aarm_after_run` in the executor, and posts `foreign_publish` or `notice` events as those rules require.
- `Ownerships.afind_open` returns `None` for an unowned or closed merge request, else the ownership, whose `session` owns it. When the owning session was deleted, it creates a session for the same person, with origin `pipeline_webhook` and title from the merge request (FR-004).

## Sweeps (`sessions/tasks.py`)

| Task | Schedule | Does |
|---|---|---|
| `drain_inboxes_cron_task` | `* * * * *` | `Drain.start_logged` for sessions with pending waking events and a free or stale slot. Returns stale `taken` events to `pending` (R12, R13) |
| `classify_push_task` | Enqueued with `run_after=+60s` | Decides `branch_pushed` against recorded DAIV SHAs (R8) |
| `reconcile_pipeline_watches_cron_task` | `*/10`, existing | Re-reads the latest pipeline of each CI subject for owned, open, watched merge requests nothing was heard of for 30 min (no CI event, claim or re-read), at most 200 a tick, stalest first. The 6-hour expiry is removed |
