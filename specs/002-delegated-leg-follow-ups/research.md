# Research: Delegated Leg Follow-ups

Decisions R1–R18 below resolve how the [spec](spec.md) maps onto the code as of `1fb9ccf2`. Each names what was
chosen, why, and what was rejected. The Technical Context had no open unknowns; the questions here are design
choices the spec leaves to the plan.

## R1. Where the code goes

**Decision**: Four places, all at or below `sessions`:

- `daiv/sessions/legs.py` (new) holds `Leg`: one delegated session as its coordinator sees it. It answers where the
  leg started, whether it has a branch of its own, which merge request it published, whether DAIV lost track of it,
  and whether delegated work on it is unfinished.
- `daiv/sessions/inbox/leg_follow_ups.py` (new) holds `LegFollowUps`: the inbox side of the new event kind. It posts
  the event with its own run, stops it at take time, and ends it with its run.
- `daiv/sessions/delegation.py` keeps the coordinator side: refusals, batch assembly, status, the resume summary and
  cancel.
- The agent side changes only data and text: `automation/agent/delegation.py` (dataclasses) and
  `automation/agent/middlewares/delegate_jobs.py` (schema, descriptions, system prompt).

**Rationale**: `sessions.delegation` imports `sessions.queue` and `sessions.inbox`. Both need leg facts (`queue` for
the branch rule in R9, the drain for R5), so those facts cannot live in `delegation` without an import cycle. `Leg` is
one concern with one owner (`patterns/code-organization.md`). No app boundary moves and no `ignore_imports` is added.

**Alternatives considered**: Methods on `Session`. Rejected because the model module would grow delegation rules
that only delegation reads. Putting it all in `delegation.py` was also rejected: the drain and the queue would have
to import it, which is a cycle.

## R2. A new inbox event kind, not a follow-up job

**Decision**: Add `EventKind.LEG_FOLLOW_UP = "leg_follow_up"`, labelled "Leg follow-up". Its payload is
`LegFollowUpPayload{prompt, batch_id, references?, muted?}` and its source key is `leg-follow-up:<batch_id>`, one
per leg per batch. Its rules:

| Rule | Value | Why |
|---|---|---|
| Wakes the agent | yes | FR-004 |
| `GATED_KINDS` (run limit) | no | FR-004a |
| `PERSON_KINDS` | no | A coordinator wrote it. It is neither split as a slash command nor used for the agent-model override |
| `follows_session()` | yes | It concerns whichever branch the leg works on now |
| Run trigger type | `DELEGATED_JOB` | Unattended (`SessionOrigin.unattended`), one active per session, found by `_delegated_legs`, resumes its batch on `run_finished` |
| `RunInput.request()` | the payload's `prompt` | The leg reads it as it read its first request (spec Assumption) |
| Requester | the event's `author`, the coordinator run's user | FR-005; `Requester.of_event` already prefers the author |

**Rationale**: A follow-up job is a person's request. It is retried three times, gated by nothing, joins its
requester's next run and may carry a branch; a leg follow-up does none of these (FR-004, FR-008, FR-011a). One kind
with two behaviours would mean branching on the payload everywhere, and the glossary keeps them apart (FR-022).

**Alternatives considered**: A `FOLLOW_UP_JOB` with `trigger_type=delegated_job`. Rejected because it would join
the person's next run, which breaks FR-004. Adding `DELEGATED_JOB` to `INBOX_TRIGGERS` was also rejected: those
literals feed `run_one_next_run_per_user`, which would make the leg follow-up and the person's own chat compete for
one QUEUED row.

## R3. A leg follow-up has its own run from the moment it is sent

**Decision**: `LegFollowUps.post` creates the event and a QUEUED `Run` in one transaction, with the leg session's
row locked, and links the two (`InboxEvent.run`). The run carries:

- `trigger_type=DELEGATED_JOB` and the batch's `batch_id`;
- `user` set to the coordinator run's user, and `muted` set to the coordinator's effective mute;
- the prompt, with `ref=session.ref` and the session's sandbox environment.

It is not a "next run": its trigger type is outside `INBOX_TRIGGERS`, so `NextRuns` never joins, describes or drops
it. `sessions.queue` already skips QUEUED runs that have inbox events linked.

**Rationale**:
- FR-011: the batch is `Run.objects.by_batch(batch_id)`, so a non-terminal run keeps it open while the follow-up waits.
- FR-007: the call can return the session id and link at once.
- FR-013: cancel and status work on runs, so they cover follow-ups as they are.

**Alternatives considered**: Creating the run when the drain takes the event. Rejected because the batch would look
finished while the event waits, and the coordinator would be resumed early.

## R4. The drain takes a leg follow-up alone, with remembered context

**Decision**: `RequestSplits` gains a second "runs alone" rule. A leg follow-up is a split of its own, as a builtin
slash command is. Unlike a command, its take still includes every remembered event for the branch (FR-004). When
the first split is a leg follow-up, `_Take` promotes that event's own run instead of the requester's next run:

- `status=READY`, with `ref` set to the branch the take chose;
- the requester fields and `prompt=RunInput(taken).request()`;
- the session's pinned agent model and the sandbox environment, as for any inbox run.

Other waking events, including the same person's chat or a CI failure, stay for runs of their own, in the order the
inbox already serves them.

**Rationale**: This reuses the split machinery that already keeps commands out of shared runs, and changes only what
the split takes and which run it starts.

**Alternatives considered**: A separate drain path for leg follow-ups. Rejected because it would be a second
mechanism for "one run per session" and would duplicate the slot claim and the access check.

## R5. A merged or closed merge request stops the follow-up when it starts

**Decision**: When a take's first split is a leg follow-up, it checks the leg's merge request before promoting
(`Leg.merge_request_state()`, R1). That merge request is the newest one the session's runs published on its main
repository, as recorded in `MergeRequestOwnership`; `merged` means merged, and `closed_at` set without `merged` means
closed. When it is merged or closed:

- the run finishes `FAILED` with `never_started=True`, and its `error_message` is a fixed label:
  `NOT_RUN_MERGED` = "Not run: its merge request was merged", or `NOT_RUN_CLOSED` = "…was closed";
- the event becomes `failed`, with `EventFailure.MR_MERGED` or `EventFailure.MR_CLOSED`;
- the drain runs again on commit.

`leg_state()` reads the label, as it reads `LINK_FAILED_PREFIX` for "lost", and returns `not_run_merged` or
`not_run_closed`. The coordinator sees "not run: its merge request was merged" (or "closed"), followed by "delegate a
new leg for further work".

**Rationale**: FR-008a needs an outcome of its own without a new `RunStatus`. `RunStatus.terminal()` and every reader
of FAILED stay as they are, and "lost" already works this way. A run without an ownership row (published before the
inbox existed) counts as open, and the agent's own push then reports what happened.

**Alternatives considered**:
- A new `RunStatus.SKIPPED`. Rejected because every consumer of terminal statuses (queue, recovery, notifications,
  filters, badges) would need it, for one rare outcome.
- Asking the git platform at take time. Rejected because it is a network call under the session row lock, and the
  ownership row already follows the merge and close webhooks.

## R6. A start refused for access fails the follow-up's own run

**Decision**: When the drain's access check fails a leg follow-up's requester, `_Take._fail` also finishes that
follow-up's own run `FAILED` (`never_started`, error label `EventFailure.ACCESS_DENIED`). Today it finishes only the
requester's QUEUED next run. The batch then finishes, and the summary reports the leg as "failed" with no reply.

**Rationale**: FR-005a. The label is the event-failure text the inbox already shows, so no internal error reaches the
coordinator.

## R7. A leg follow-up ends with its run, never retried

**Decision**: One rule covers every way the run can end:

- **Ends `SUCCESSFUL`**: the taken event becomes `handled`, as today.
- **Ends `FAILED`, not cancelled**: the taken leg follow-up becomes `failed`, with a new `EventFailure.RUN_FAILED`
  ("The run that took it failed"). Other events the run took, which are remembered context, still go through
  `FailedAttempt` as today.
- **Cancelled**: the event becomes `dismissed`, as today.
- **Ends before the take** (cancelled while waiting, or access denied): `RunEnd.settle_events` also settles the
  *pending* leg follow-up linked to the run. It is dismissed when the run was cancelled, otherwise failed.
- **The sweep recovers a stale run's events** (`FailedAttempt.record`): a leg follow-up counts as out of attempts
  after one.

**Rationale**:
- FR-011a: a retry would start a second run after the first run's FAILED had already finished the batch.
- FR-013: only the follow-up's own event is linked to its run, so cancelling it leaves every other waiting request
  in place (SC-006).

**Alternatives considered**: `MAX_EVENT_ATTEMPTS` per kind, set to 1. Rejected because it would label the failure
`retries_exhausted`, which is untrue for an event never retried (`patterns/errors.md` rule 4).

## R8. The wake limit: a follow-up always wakes, and its run counts as DAIV-started

**Decision**: `LEG_FOLLOW_UP` is not in `GATED_KINDS`, so it always wakes (FR-004a). `WakePolicy` gets one query
expression for "a run DAIV started": a `daiv_authored()` trigger, or a `DELEGATED_JOB` run that has a
`LEG_FOLLOW_UP` event linked. Both `system_runs_in_a_row` and `_last_person_run_at` use it. `SessionOrigin.daiv_authored()`
itself does not change.

**Rationale**:
- Today a leg's first run (`DELEGATED_JOB`) does not count toward the streak, so a leg gets three CI fixes before the
  limit. Counting every delegated run would cut that to two, a regression nobody asked for.
- `daiv_authored()` also decides transcript authorship and classification, which must not change.
- The spec's edge-case example was corrected to match: the limit is reached after three CI fixes in a row
  ([refinements](#spec-refinements-for-approval)).

**Alternatives considered**: Adding `DELEGATED_JOB` to `daiv_authored()`. Rejected for the two effects above.

## R9. Which branch a follow-up publishes to

**Decision**: `aenqueue_run` forks a new branch for a `DELEGATED_JOB` run only when its leg has no branch of its own
yet (`not Leg.has_own_branch()`). A leg has a branch of its own when `Session.ref` differs from the ref of its first
delegated run, which is the ref a waiting leg was rebased onto. So:

- **A leg that published to its own branch**: no fork. The git middleware looks up the open merge request on the
  checked-out branch (the leg's own) and pushes onto it (FR-003, SC-004).
- **A leg that published nothing**: a fork, as for a new leg, so a leg that started on the coordinator's branch never
  pushes to the coordinator's merge request (US1 scenario 3).
- **A first leg run**: always a fork, as today.

**Rationale**: Today's `fork_branch=trigger_type == DELEGATED_JOB` would open a second merge request whenever the
leg's checkpoint lost its merge request, for instance when the earlier conversation is unavailable (edge case). The
session's branch is the durable fact.

**Alternatives considered**: Keeping the fork and relying on the checkpoint's merge request. Rejected for the reason
above. Recording the decision when the follow-up is sent was also rejected: the leg's session can publish between
the send and the start, for example on a person's chat.

## R10. Refusals are checked when the call is made, under the leg's row lock

**Decision**: `SessionJobDelegator.adelegate` sorts targets into new legs and leg follow-ups (a target with
`thread_id`). The call is refused as a whole (`DelegationError`) when one leg is named twice (FR-010). Each follow-up
is then checked in this order, the first failing check deciding its reason:

1. it is not a leg of this coordinator;
2. it names a branch;
3. it names a repository other than the leg's;
4. the person it runs as archived it;
5. delegated work on it is unfinished;
6. DAIV lost track of its last run;
7. its merge request is merged or closed.

Checks 5 and 7 run again inside `LegFollowUps.post`, with the leg's row locked, so two calls cannot both pass. Each
refusal is a `FailedLeg` that names the `thread_id`, with a fixed reason
([contract](contracts/delegate-jobs-tool.md#refusals)). Repository access is not checked for follow-ups (FR-005a);
new legs keep today's check.

**Rationale**: The cheap, deterministic checks come first, so the reason the coordinator reads is the one it can act
on. Check 5 covers "earlier work unfinished", "a follow-up outstanding" and "waiting to start after the coordinator's
turn", because every one of them is a non-terminal `DELEGATED_JOB` run on the leg's session.

## R11. Assembling one batch from new legs and leg follow-ups

**Decision**: The delegator mints the `batch_id` and gives it to `asubmit_batch_runs`, which gains an optional
`batch_id` parameter. The order is:

1. validate the whole call (R10, plus the new legs' admission checks);
2. post the leg follow-ups with `drain=False`;
3. submit the new legs;
4. drain each follow-up's leg session.

`_resumes` treats a leg follow-up's run as started, so a batch whose only follow-up was refused access at take time,
or stopped as not run, still resumes the coordinator. A batch whose every run was cancelled still does not.

**Rationale**: No run of the batch can finish before every row of the batch exists. A follow-up therefore cannot
resume the coordinator while a new leg is still being created, nor the reverse. A batch with only follow-ups never
calls `asubmit_batch_runs`.

**Alternatives considered**: Posting the follow-ups after the new legs, with the drain on commit. Rejected because a
fast-failing follow-up (access denied) could resume the coordinator before a new leg's row exists, and the order of
the two steps would decide which half of the batch the summary covers.

## R12. Replies up to 4,000 characters, read from the full answer

**Decision**:
- `LEG_REPLY_MAX_CHARS = 4000` applies to the resume summary and to the status of one named batch.
- `LEG_OVERVIEW_REPLY_MAX_CHARS = 1000` applies to the overview of the latest batches (FR-017).
- The reply is read from `Run.response_text`, which is the task result's full `response`, else the stored
  `result_summary`. The leg queries `select_related("task_result")`.
- A reply cut at the limit ends with
  `[cut at 4,000 characters: the rest is in the leg's session, <session_url>]`.
- The stored `result_summary` keeps its 2,000 cap (spec Assumption).

**Rationale**:
- 4,000 is the spec's figure. Ten legs bound a resume to about 40,000 characters.
- The eval case whose finding sits past character 2,000 (about 2,600 long) passes with room to spare (SC-003).
- `response_text` already exists and holds the uncut answer for every task-backed run, and every leg run is one.

**Alternatives considered**:
- Raising the `result_summary` cap. Rejected because notifications and the chat stream read it, and the spec keeps it.
- 8,000. Rejected because ten legs would put 80,000 characters into one resume input.

## R13. What the coordinator is told

**Decision**:
- **Resume summary**: each leg's section adds `- Session id: <thread_id>` (FR-015) and marks a leg follow-up
  (`(leg follow-up)` after the outcome). The closing paragraph is replaced by the FR-018 guidance
  ([contract](contracts/delegate-jobs-tool.md#resume-summary)).
- **`delegate_jobs` tool**: its description and the delegation system prompt gain the `thread_id` target option and
  the rule "follow up on a leg to fix or extend its own work; delegate a new leg for separate work".
- **`check_delegated_jobs`**: its description lists the two "not run" outcomes.

**Rationale**: FR-015 and FR-018. Measured by the `delegation` eval suite (R17).

## R14. Status and the branch a leg reports

**Decision**:
- `LegStatus.branch` is the leg's own branch whenever it has one (`Leg.has_own_branch()`). It is no longer "the
  session's ref when it differs from this run's ref", which hid the branch for a follow-up that started on it
  (FR-014).
- `LegStatus.ref` is the ref the leg started from.
- `merge_request_url` is the leg's merge request, not this run's, so a follow-up that pushed nothing still shows it.
- `LegStatus` gains `follow_up: bool`.

## R15. The coordinator's session page

**Decision**:
- `delegations_for` returns `total` as the number of distinct leg sessions, with batches still listing per-batch legs
  (FR-019).
- `_leg_payload` adds `follow_up` and, for a session the viewer may see, `thread_id`.
- On the call card, `tool-renderers.js` (`mergedLegs`, `delegatedTable`) matches a target that has a `thread_id` to
  the leg with that `thread_id` before trying repository and branch, and labels the row "leg follow-up" (FR-020).
- `StartedLeg` and `FailedLeg` carry `thread_id` and `follow_up` in the tool result, so the card can match before the
  server legs arrive.
- New `transcript_labels` entries: `leg_not_run_merged`, `leg_not_run_closed`, `leg_follow_up`.

**Rationale**: The top bar no longer shows delegations (commits 556c8f71 and 5bb33d18). The details panel's "Delegated
jobs" count is the only top-level surface left, so "listed once" applies to that count
([refinements](#spec-refinements-for-approval)).

## R16. How the leg and people see it

**Decision**:
- **Leg transcript**: the follow-up's run shows its prompt as a user turn, as the leg's first run does, with no change.
  The inbox panel shows the event under its label "Leg follow-up", with the prompt as its text (`inbox_display.bubble`).
- **Run marker**: a not-run follow-up's marker (`transcript._marker`) is the neutral "aborted" marker with
  its label, as a cancelled leg's is.
- **Notifications**: unchanged. Every terminal non-chat run is classified, and a FAILED one gets a failed envelope whose
  summary is the first line of `error_message`. A not-run follow-up's notification therefore reads "Not run: its merge
  request was merged", as a cancelled leg's reads its cancel message (spec Assumption).

**Alternatives considered**: A new envelope status for "not run". Rejected for now and listed under Complexity
Tracking: today's failed envelope with an honest summary is how cancelled legs are reported too.

## R17. Measuring it

**Decision**: Use the `delegation` suite from PR #1780, which is now commit `9f10f3a3` on `claude/delegation-evals`
and not yet on `main`:

1. Merge that suite to `main`.
2. Record the baseline there: `make eval-prompts SUITES=delegation OUT=eval-runs/before.jsonl`, with three passes on
   `EVAL_MODEL`.
3. Rebase this branch onto it.
4. Extend `RecordingDelegator` to record `thread_id` targets, and add two cases:
   - `test_a_leg_that_reports_its_work_incomplete_gets_a_leg_follow_up` (SC-001);
   - `test_an_independent_second_part_gets_a_new_leg` (SC-002).
   Both resume from a `render_batch_summary` input, as the suite's resumed cases do. Their fixture replies are made
   with `Run.response_text` in mind (a `DBTaskResult` return value), so they exercise the 4,000 limit.
5. Run the after-pass with the same model and suite, and paste `evals/compare_runs.py before.jsonl after.jsonl` into
   the pull request:
   - SC-003: the five baseline cases show no confirmed regression, and case 5 (the finding past character 2,000) flips
     from failing to passing.
   - The two new cases have no "before". Their pass rate over three votes is reported, and a flip is confirmed per
     `AGENTS.md`.

**Rationale**: `patterns/agent-evals.md` rules 1–4. The tool description, the system prompt and the resume input all
change what the model sees.

**Alternatives considered**: An LLM judge. Rejected because the suite's assertions are deterministic on recorded
calls, which is cheaper and less noisy.

## R18. Migrations and deploy

**Decision**: There are two choices-only `AlterField` migrations in `sessions`:

- `InboxEvent.kind` gains `leg_follow_up`;
- `InboxEvent.failure` gains `run_failed`, `mr_merged` and `mr_closed`.

There are no new columns. During a rolling deploy, an old pod's drain that meets a `leg_follow_up` event raises
inside `Drain.start_logged`, which logs it, and the per-minute sweep retries once new pods serve it. No new
`Run` columns are needed: the follow-up's run is identified by its linked event.

**Rationale**: The migrations are deploy-safe, so old and new pods can run side by side.

## Spec refinements (for approval)

These go to the maintainer with the plan. The first is already applied to `spec.md` because it corrected a fact.

1. **Edge case, run limit**: the example now says the limit is reached "after three CI fixes in a row (the leg's
   first run is not one of them)". The earlier text said "after its first run and two CI fixes", but a leg's first run
   does not count toward the streak (R8).
2. **FR-019, "top-level delegations"**: the top bar no longer lists delegations, so "list and count once" is met by
   the details panel's count of distinct leg sessions. Batch details list a leg under each batch it served (R15).
3. **FR-007, `starts_after_this_turn`**: always false for a leg follow-up. It never waits for the coordinator's turn,
   and a leg with no branch of its own forks from its session's branch as that branch stands when the follow-up
   starts. Holding inbox runs on `waits_for` would need drain support that only this rare case uses: a leg that
   published nothing, on the coordinator's branch, followed up in the same turn as a push.
4. **CHANGELOG**: the existing Orchestration entry still mentions the top bar. The FR-021 changelog edit corrects
   that line too.
