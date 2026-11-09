# Contract: `sessions` interfaces

These are the Python interfaces this feature adds or changes. Every one sits at or below `sessions`, so the
`.importlinter` contracts are unchanged. Signatures are the intended shape, not code to paste.

## `sessions/legs.py` (new): `Leg`

A leg as its coordinator sees it (data-model "Leg"). It is SYNC ONLY, built from a `Session`, with async wrappers
where an async caller needs one. It imports only `sessions.models`. `sessions.queue` imports `Leg`, so `legs.py`
cannot import `sessions.queue`: `LINK_FAILED_PREFIX` moves from `sessions/queue.py` to `sessions/models.py`, and
`queue.py` and `delegation.py` import it from there.

```python
class Leg:
    def __init__(self, session: Session): ...
    @classmethod
    def of(cls, thread_id: str, *, coordinator: str) -> Leg | None   # None unless belongs_to(coordinator)
    base_ref: str                                  # ref of the first DELEGATED_JOB run
    def has_own_branch(self) -> bool               # session.ref != base_ref
    def merge_request(self) -> LegMergeRequest | None   # (repo_id, iid, web_url) of the newest one its runs published
    def merge_request_state(self) -> MergeRequestState  # open | merged | closed; no ownership row → open
    def is_busy(self) -> bool                      # a DELEGATED_JOB run on the session is not terminal
    def is_lost(self) -> bool                      # latest DELEGATED_JOB run FAILED with LINK_FAILED_PREFIX
    def archived_by(self, user: User) -> bool      # HiddenSession(session, user) exists
    async def ahas_own_branch(self) -> bool        # for sessions.queue
```

Callers:
- `sessions.queue.aenqueue_run` (R9);
- `sessions.inbox.leg_follow_ups` (R5);
- `sessions.delegation` (R10, R14).

## `sessions/inbox/leg_follow_ups.py` (new): `LegFollowUps`

```python
class LegFollowUps:
    def __init__(self, leg: Leg): ...

    def post(self, *, prompt: str, batch_id: uuid.UUID, user: User, muted: bool | None,
             references: list[dict], drain: bool = True) -> Run
        """Create the leg_follow_up event and its own QUEUED DELEGATED_JOB run in one transaction, with the leg's
        session row locked, and link them. Re-checks Leg.is_busy() and the merge request state under that lock,
        raising LegFollowUpRefused(reason) when either now refuses. With drain=True the session is drained once
        the transaction commits. SYNC ONLY; apost wraps it."""

    @staticmethod
    def stop_reason(event: InboxEvent, session: Session) -> EventFailure | None
        """For the drain: MR_MERGED / MR_CLOSED when the leg's merge request is merged or closed, else None."""

    @staticmethod
    def stop(event: InboxEvent, failure: EventFailure) -> None
        """Fail the event with ``failure`` and finish its run FAILED, never_started, with the matching NOT_RUN label.
        The finish emits run_finished, which resumes the batch when it was the batch's last run."""

    @staticmethod
    def end_with_run(run: Run) -> None
        """For RunEnd.settle_events: settle the leg follow-up linked to ``run``, whether pending or taken.
        Cancelled → dismissed; FAILED → failed (run_failed); SUCCESSFUL → handled (the shared path)."""
```

`LegFollowUpRefused(reason: str)` is caught by `SessionJobDelegator`, which turns it into a `FailedLeg` with the
matching refusal text.

## `sessions/inbox` changes

| Where | Change |
|---|---|
| `kinds.py` | `LegFollowUpPayload`; `PAYLOADS`, `PRECEDENCE` (ranked with `FOLLOW_UP_JOB`; it never shares a run, so its rank never decides); not in `GATED_KINDS` or `PERSON_KINDS`; `follows_session()` true; `run_trigger_type()` → `DELEGATED_JOB`; `SourceKey.leg_follow_up(batch_id)` |
| `splits.py` | `RequestSplits.runs_alone()` is true for a leg follow-up; new `RequestSplits.takes_context(event)`, false only for a slash command |
| `drain.py` (`_Take.take`) | when the first split is a leg follow-up: check `LegFollowUps.stop_reason` and stop it if set; take it with the branch's remembered events; promote the event's own run (`ref`, `status=READY`, requester fields, `prompt`, `agent_model` pinned, sandbox env). `_fail` also finishes the follow-up's own run FAILED with the `access_denied` label. `_merge_references` merges a taken leg follow-up's `references` too |
| `render.py` | `RunInput.request()` and the HumanMessage use `payload["prompt"]` for a leg follow-up |
| `complete.py` | `RunEnd.settle_events` calls `LegFollowUps.end_with_run(run)` before the shared paths, and leg follow-ups are excluded from `FailedAttempt`; `FailedAttempt.record` fails a leg follow-up on its first attempt with `run_failed` |
| `policy.py` | `WakePolicy`'s "DAIV-started run" expression: `daiv_authored()` triggers, or a `DELEGATED_JOB` run with a `leg_follow_up` event linked; used by `system_runs_in_a_row` and `_last_person_run_at` |
| `__init__.py` docstring | the rules gain: "A leg follow-up has its own run from the moment it is posted, runs alone with the branch's remembered events, is never retried, and stops at its start when its leg's merge request was merged or closed." |

`Inbox.post` refuses `kind=LEG_FOLLOW_UP` with `ValueError` ("use LegFollowUps.post"). Otherwise
`NextRuns.join` would attach the event to the requester's next run.

## `sessions/queue.py`

```python
async def aenqueue_run(run, *, log_context, skip_dispatch=False, inbox=False) -> EnqueueFailure | None
    # fork_branch = run.trigger_type == DELEGATED_JOB and not await Leg(run.session).ahas_own_branch()
```

The first leg run always forks: its session's `ref` equals its own `ref`.

## `sessions/services.py`

```python
async def asubmit_batch_runs(*, ..., batch_id: uuid.UUID | None = None) -> BatchSubmitResult
    # batch_id: the batch these runs join, minted by the caller; a fresh one when None (today's behaviour)
```

## `sessions/delegation.py`

| Name | Change |
|---|---|
| `SessionJobDelegator.adelegate` | splits targets; FR-010 check; refusal order per [the tool contract](delegate-jobs-tool.md#refusals); mints `batch_id`; validates new legs' admission; posts follow-ups with `drain=False`; submits new legs with `batch_id`; drains the follow-up sessions; returns `StartedLeg(leg_follow_up=True)` and `FailedLeg(thread_id=…)` |
| `LEG_REPLY_MAX_CHARS` | 4000 |
| `LEG_OVERVIEW_REPLY_MAX_CHARS` | new, 1000; used by `_status(batch_id=None)` |
| `_leg_status(leg, *, reply_limit)` | `ref` = `Leg.base_ref`; `branch` when `Leg.has_own_branch()`; `merge_request_url` = `Leg.merge_request()`; `reply` from `response_text`, cut with the marker; `leg_follow_up` |
| `_legs()` | `select_related("session", "task_result")`, annotated `is_leg_follow_up` (an `Exists` on `leg_follow_up` events) |
| `_resumes(legs)` | a leg follow-up's run counts as started |
| `render_batch_summary` | session id line, `(leg follow-up)` marker, not-run "Next" line, new closing paragraph ([contract](delegate-jobs-tool.md#resume-summary)) |
| `leg_state` / `_OUTCOMES` | `not_run_merged`, `not_run_closed` from the `NOT_RUN_*` labels |
| `_leg_payload` | adds `leg_follow_up`, and `thread_id` when the session is visible |
| module docstring | rewritten: "a leg's first run is its whole answer" no longer holds; it says how follow-ups join a batch |

## `sessions/header.py`

`delegations_for(...)["total"]` is the number of distinct `session_id`s across all batches (FR-019).

## `sessions/transcript.py`

`_marker(run)`: a delegated run whose `error_message` is a `NOT_RUN_*` label gets the neutral "aborted" marker with
that label.

## `notifications/tasks.py`

`classify_run_task`: before its FAILED gate, a run whose `error_message` is `NOT_RUN_MERGED` or `NOT_RUN_CLOSED`
(`sessions.legs`) gets an `all-clear` envelope with that label as its summary, no actionable items and no model call.
Every other run is classified as today.

## Signals

There are no new receivers:

- `run_finished` → `resume_coordinator` fires for a follow-up's `DELEGATED_JOB` run;
- `run_ended` → `settle_inbox_events` settles its event.
