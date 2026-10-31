# Research: Session Inbox

Phase 0 of the plan for [spec.md](spec.md). Each decision records what was chosen, why, and what else was considered. The code facts that each decision rests on come first. Paths are under `daiv/` unless noted.

## What the code does today

**The session slot.**
- `Session.active_run_id` is the only per-session mutual exclusion, and `last_active_at` is its heartbeat.
- `SessionLock.try_claim` claims the slot with a compare-and-set, and takes it over once the heartbeat is older than 30 minutes (`sessions/executor/lock.py:128-202`).
- Chat claims the slot in the view (`chat/api/views.py:263`) and streams with a `Held` policy.
- Background jobs and webhook addressors use `Wait`, which polls every 5 s for up to 30 min (`lock.py:205-315`). A background run is marked RUNNING before it has the slot (`lock.py:100-102`), so a waiting run occupies a worker.

**Busy sessions.**
- A chat message sent while the slot is held, or while non-chat runs are unfinished, gets a 409 (`chat/api/views.py:260-264`). Chat messages are never queued.
- `run_one_active_per_session` only covers `api_job`, `mcp_job`, `delegated_job` and `delegation_resume` (`sessions/models.py:437-444`).
- A CI fix run is created READY and enqueued at once (`sessions/pipeline_watch/dispatch.py:47-61`), so on a busy session it waits on the lock.

**Run end.**
- `RunManager.afinish` emits `run_ended` for every run and `run_finished` for non-chat runs only (`sessions/managers.py:343-350`).
- Receivers of these signals: queue dispatch, release of runs that wait for this one, delegation resume, memory capture, and classification (`sessions/signals.py:117-145`, `memory/signals.py`, `notifications/signals.py`).
- A chat turn ending does not dispatch the session's queued runs.

**Input.**
- A background run's input is `HumanMessage(prompt)` (`sessions/executor/tasks.py:83`).
- `automation/agent/synthetic.py` already marks DAIV-written `HumanMessage`s, and transcripts hide them (`sessions/turns.py:72-73`).
- The transcript tags `daiv_authored()` triggers with a "DAIV · CI failed" style notice (`sessions/transcript.py:173-177`).
- `annotate_transcript` already splices in a non-message `run_status` entry (`transcript.py:59-80`).

**Publishing.**
- The SHA DAIV pushed is not stored anywhere. `pushed_sha` exists only on the ephemeral-token "heal" path (`automation/agent/publishers.py:243-263`).
- `GitManager.head_sha()` exists (`automation/agent/workspace/git_manager.py:308`).
- `MergeMetric` attributes DAIV's commits by bot author email, which anyone can forge.

**Webhooks.**

| Event | GitLab | GitHub |
|---|---|---|
| Push | Hook enabled for all branches, but only the default branch is handled (`webhooks/gitlab/callbacks.py:259-278`) | Default branch only (`webhooks/github/callbacks.py:231-250`) |
| Merge request / pull request | Only a merge into the default branch counts (`:222-256`) | Only closed-and-merged into the default branch counts (`:196-228`) |
| Close without merge | Ignored | Ignored |
| Diff comments | DiffNotes ignored (`:199`) | `pull_request_review_comment` not subscribed |

- GitHub's unreachable review-reply branch is at `codebase/clients/github/client.py:617-635`.
- The only check on a mention's author is `RepositoryConfig.is_user_allowed` (`codebase/repo_config.py:216-223`), plus "not the bot".
- No delivery id is used for de-duplication. Mention tasks de-duplicate on `(repo, iid, note_id)` (`webhooks/tasks.py:309`).

**Mention replies.**
- `CommentsAddressorManager` posts a plain answer as a top-level merge request comment, and a question as a reply in the discussion (`webhooks/managers/review_addressor.py:196-197, 278-288`).
- It passes no `acting_user_id`, and neither does the issue addressor (`webhooks/managers/issue_addressor.py:148-170`).

**Notifications.**
- `PIPELINE_WEBHOOK` and `CHAT` runs are not classified, so CI fix runs produce no envelope and no notification (`sessions/models.py:101-104`).
- `watch_exhausted` notifies `session.user` of the merge request's own thread only, and is skipped when that is empty (`notifications/watch_notifiers.py:46-79`).

**Sandbox reuse.** A warm sandbox is not re-synced with the remote, so a person's push since the last run is invisible until push time (`git_manager.py:326-366`).

**Sweeps.**
- `sync_stuck_runs` and `resume_stalled_batches` run every 5 min.
- `reconcile_pipeline_watches` runs every 10 min and expires watches after 6 h.
- Notification backstops run every 15 min.

## Decisions

### R1. The inbox lives in `sessions`

- **Decision**: add an `InboxEvent` model and a `MergeRequestOwnership` model to `sessions/models.py`, and a `sessions/inbox/` package for posting, taking, rendering and completing events.
  - The trigger apps (`chat`, `jobs`, `mcp_api`, `webhooks`) call `sessions.inbox` to post.
  - `notifications` reacts to signals.
- **Rationale**:
  - The layering puts `sessions` below every caller (`.importlinter`), and `sessions.pipeline_watch` already lives there.
  - No new `ignore_imports` exception is needed.
- **Alternatives**:
  - A new top-level app: rejected. It would sit between `sessions` and `notifications` and need its own contracts, for no gain.
  - Storing events in the LangGraph checkpoint: rejected. Writing to a checkpoint while no run holds it races the checkpointer, and the operator could not query pending events (FR-034).

### R2. One slot, claimed when the run is created; no worker waits

- **Decision**: the drainer claims `Session.active_run_id` for the run it creates, in the same transaction, with the run's pk as holder. `run_job_task` runs such a pre-claimed run with a new lock policy, `Claimed`: it heartbeats and releases at the end, and never polls.
  - The drainer drains only when the slot is free or stale.
  - Otherwise it leaves events pending, and the end of the run holding the slot drains them.
- **Rationale**:
  - FR-012 and SC-004: today a READY fix run takes a worker and polls for up to 30 minutes.
  - Claiming at creation uses the same compare-and-set as chat, so the slot keeps being the single source of FR-007.
- **Alternatives**:
  - Keep `Wait` and rely on QUEUED rows: rejected. This is the path that holds workers.
  - Extend `run_one_active_per_session` to every trigger: rejected. Chat claims through the slot rather than a READY row, so the constraint cannot cover chat.
- **Risk**: a run claimed but not picked up by a worker for 30 min becomes stale and can be taken over. `sync_stuck_runs` already fails READY runs that were never queued after 10 min, and the drainer treats a stale slot as free.

### R3. One "next run" per busy session

- **Decision**: when a waking event lands on a busy session, the drainer ensures one QUEUED run exists for that session and the event's branch, as "the next run", and points the event at it. When the slot frees, the oldest next run is dispatched with every pending event for its branch. An idle session gets a READY run straight away.
- **Rationale**:
  - It gives every source a Run id to return or show while the session is busy. The Jobs API's `job_id` is the Run's id, per the glossary.
  - It gives the page something concrete to render as queued.
  - It keeps FR-010: one run takes every pending event.
- **Consequence**: two follow-up jobs on the same branch submitted while a session is busy return the same `job_id`, the run that handles both. Jobs on different branches get different next runs. This changes the jobs contract (see [contracts/http.md](contracts/http.md)).
- **Alternatives**:
  - One run per explicit request: rejected. It breaks FR-010 and US2 scenario 2.
  - Absorbed placeholder runs that mirror another run's status: rejected. They add a status relation to every reader of `Run`.

### R4. A run works on one branch

- **Decision**: each event records the branch it concerns (`ref`), or none. A run takes every pending event whose `ref` is empty or equals the branch of the first waking event, oldest first. Events for another branch stay pending for the next run.
- **Rationale**:
  - A session can own merge requests on different branches, for example a chat that published twice.
  - A run clones one ref, and its warm sandbox has one checkout.
- **Spec refinement**: this narrows FR-010 ("every pending event") for the rare multi-branch case. The spec should record it during review.
- **Alternatives**: let the agent switch branches inside a run. Rejected: a warm sandbox and `persist_ref` assume one branch per run.

### R5. What the model receives

- **Decision**: the run's input is two parts.
  - First, a synthetic message (`synthetic_message(kind="inbox")`, hidden from the transcript) listing the taken events, newest state per subject, replaced ones marked.
  - Then one `HumanMessage` carrying the explicit requests: the chat messages, the follow-up job prompt, and the quoted mentions with their authors.
  - When there is no explicit request, the `HumanMessage` is a DAIV-written prompt, for example the current CI fix prompt, tagged so the transcript keeps its "DAIV · CI failed" notice.

  The format is in [contracts/agent-input.md](contracts/agent-input.md).
- **Rationale**:
  - It reuses `synthetic.py`, so the transcript shows what people wrote, and remembered events appear as timeline entries instead.
  - The `HumanMessage` stays the person's words, which `annotate_transcript` joins to runs.
- **Constitution V**: this changes what the model sees. See R18.
- **Alternatives**: one combined `HumanMessage`. Rejected: the transcript would show DAIV's summary as if a person wrote it.

### R6. Which run kind a combined run gets

- **Decision**: the run's `trigger_type` is set when it is dispatched, from the highest-precedence waking event it takes:
  1. chat message → `chat`
  2. mention → `mr_webhook`
  3. follow-up job → `api_job` / `mcp_job`
  4. delegated jobs finished → `delegation_resume`
  5. CI failure → `pipeline_webhook`

  While QUEUED, the value is provisional.
- **Rationale**:
  - The trigger type drives classification, notifications, retry and transcript tagging, and a person's request should win over DAIV's own.
  - A queued chat message runs as a task-backed `chat` run, which needs the chat-specific branches audited (R9).
- **Alternatives**: a new `inbox` trigger type. Rejected: it hides what started the run from dashboards and filters that deep-link on `?trigger=`.

### R7. Merge request ownership, and "DAIV pushed this commit"

- **Decision**:
  - At publish, the executor claims ownership as FR-001 defines it, replacing today's `arm_watch` step (`sessions/executor/run.py:593-603`). It records the SHA DAIV pushed in `MergeRequestOwnership.last_daiv_sha` and in a new `Run.pushed_sha`.
  - The publisher returns `pushed_sha = GitManager.head_sha()` after a successful push, in `PublishOutcome` and the `daiv_publish_result` event.
  - A CI failure wakes the session only if:
    - the pipeline's SHA equals the merge request head and `last_daiv_sha`;
    - it differs from `last_examined_sha`;
    - the attempt budget is left, the watch is on for the merge request, and the session isn't muted (FR-015).
  - Dispatching a CI-woken run sets `last_examined_sha` and charges an attempt.
  - A run started by a person that publishes resets the budget (FR-016).
- **Rationale**: the SHA DAIV itself pushed is the only signal that can't be forged and works on both platforms. Author email can be spoofed. On GitLab, the pusher identity changes with ephemeral project tokens, and the service account re-triggers pipelines.
- **Alternatives**:
  - The bot's commit email: rejected, forgeable.
  - The `DAIV-Session` trailer: rejected, only present when session links are enabled.

### R8. Pushes, merges and closes become events

- **Decision**:
  - Handle pushes to non-default branches that have an owned merge request.
  - Handle merge request close and merge on any target branch: GitLab MR hook `close` and `merge`, GitHub `pull_request` `closed`.
  - A push whose `after` SHA is not a SHA DAIV pushed becomes a remembered `branch_pushed` event. The decision runs in a task enqueued 60 s after the webhook, so a DAIV publish whose webhook beats the SHA write isn't misread as a person's push.
  - A push by another person's DAIV session is recorded at publish time as `foreign_publish` (FR-001), not from the webhook.
  - Both platforms already deliver these events: the GitLab hook has push and merge-request events enabled for all branches (`codebase/clients/gitlab/client.py:367-372`), and the GitHub App subscribes to push and pull_request. Only the callbacks change.
- **Rationale**: FR-023 and FR-024, using deliveries DAIV already receives.
- **Alternatives**: poll the merge request on each run start to detect pushes. Rejected: it doesn't record the event for the timeline (US3).

### R9. Chat while the session is busy

- **Decision**:
  - When the slot is held, or a next run is queued, the chat endpoint stores the message as a `chat_message` waking event and joins the next run, instead of answering 409.
  - The stream it returns tells the page the message is queued (see [contracts/http.md](contracts/http.md)). The page shows it as queued, and tails the run through the existing live relay when it starts.
  - When the session is idle, chat runs inline as today, takes every pending event right after claiming the slot, and prepends the synthetic context (R5).
- **Rationale**: FR-032 and US2 scenario 5. Inline chat keeps today's latency when nothing is waiting.
- **Audit**: a task-backed `chat` run must pass the chat-specific branches:
  - `sync_stuck_runs`, which fails RUNNING chat runs on stale sessions;
  - `finalize_chat_run`;
  - `SessionOrigin.retryable`;
  - `LiveRun.stoppable`;
  - `run_ended` versus `run_finished`.

  The drainer and completion handlers listen on `run_ended`, which fires for chat too.
- **Alternatives**: queue the message in the browser. Rejected: it is lost when the tab closes, which breaks "accepted, never rejected".

### R10. Mentions on owned merge requests

- **Decision**: `webhooks.dispatch` looks up `MergeRequestOwnership` for `(repo, iid)`.
  - **Owned**: post a waking `mention` event to the owning session, with source key `note:<platform note id>`. Keep the 👀 reaction and the `is_user_allowed` check (FR-026). Create no merge request thread run.
  - **Not owned**: unchanged.

  Diff comments:
  - GitLab DiffNotes are accepted only for owned merge requests.
  - GitHub needs the App subscribed to `pull_request_review_comment` (an operator step, documented in the Upgrade note), and a callback that posts the same event.
- **Reply**: the completion step posts the run's final message on the merge request.
  - With one mention, it replies in that mention's discussion. On GitHub, a review comment gets `create_review_comment_reply`, which is reachable now, and an issue comment gets a top-level comment.
  - With several mentions on one merge request, it posts the full reply in the latest mention's thread, and a one-line link to it in each other thread.
  - A run that ends WAITING_INPUT posts its question the same way.
- **Rationale**: FR-025 to FR-028 without changing the agent's tools (Constitution V). The reply rule is deterministic and testable.
- **Alternatives**: a `reply_to_mention` tool for the agent. Rejected for this feature: it is an agent-behaviour change that needs evals, and the fixed rule meets the requirement.

### R11. Acting identity

- **Decision**:
  - Every inbox-drained run sets `acting_user_id = session.user_id` (FR-027, FR-022).
  - A session with no DAIV user runs with `acting_user_id=None`, as today's webhook runs do, and skips the access check (clarification, 2026-10-30).
  - The access check reuses `assert_repo_access`. A denial marks the taken events `failed` with reason `access_denied`, and the run never starts.
  - The issue addressor starts passing `acting_user_id`, so issue sessions get an owner for ownership (FR-001).
- **Rationale**: the decisions the spec clarified.

### R12. Taken, handled, retried

Events are completed in the `run_ended` receiver.

| Run outcome | What happens to its events |
|---|---|
| SUCCESSFUL or WAITING_INPUT | `handled` |
| Cancelled (`cancel_requested_at` set) | `dismissed`: the owner chose to stop, so the run doesn't restart |
| FAILED for any other reason | back to `pending` with `attempts += 1` |

- After 3 attempts an event is marked `failed`, logged with `logger.error` (`patterns/errors.md` rule 1), and shown in the session.
- A per-minute sweep (R13) recovers events taken by a run that never ended (slot stale).
- **Rationale**: FR-013 and US2 scenario 4, with the stop control (FR-030) honoured.

### R13. Drain triggers and the 1-minute target

The drainer runs:
- on transaction commit after any post;
- in the `run_ended` receiver;
- from a new cron task every minute that drains sessions which have pending waking events and a free or stale slot.

- **Rationale**: SC-009 asks for a start within 1 minute. The immediate paths make the usual case seconds. The existing 5-minute sweeps are too slow as a backstop.
- **Load**: the query reads a partial index on pending waking events, so it is cheap at today's volume (tens of events an hour).

### R14. Superseding and the size limit

- **Decision**: each event has a `subject`, for example `ci:<iid>`, `mr-state:<iid>` or `branch:<ref>`. When a run is dispatched, all but the newest pending event per subject become `superseded`. The run is woken only if a waking event survives (FR-008, FR-009).
- **Size limit**: the synthetic context is capped at 8,000 characters. When it is over, the oldest remembered events collapse into one line per kind ("3 more comments"). Waking events are never dropped (FR-014).
- **Alternatives**: a token count. Rejected: it needs the model's tokenizer in `sessions`. A character cap is provider-neutral (`patterns/platforms-and-providers.md` rule 3).

### R15. The CI watch moves onto ownership

- **Decision**:
  - `MergeRequestOwnership` takes over the watch state: per-merge-request on/off, attempts, `last_daiv_sha`, `last_examined_sha`, last judged pipeline.
  - `PipelineWatch` keeps judging pipelines (`judgment.py` is unchanged). Instead of claiming an attempt and dispatching a run, it posts a `ci_failed`, `ci_passed` or `ci_stopped` event whose `wake` comes from R7.
  - `FixRunDispatcher` is removed.
  - The "no push" exhaust path (`service.py:189-196`) is removed: a CI run that pushes nothing leaves the watch observing (FR-018, FR-019).
  - The 6-hour expiry is removed. The watch observes until the merge request is merged or closed.
  - The reconciler keeps its re-read of the latest pipeline for owned merge requests with no CI event for 30 minutes, as a backstop for missed webhooks.
  - The `POST /api/sessions/{id}/ci-watch` toggle acts on the ownership row.
- **Data migration**: every open watch (a WATCHING or FIXING session) becomes an ownership row owned by that merge request's own thread session. This keeps the spec's assumption that open watches finish where they are, and leaves one mechanism.
- **Deploy safety**: `Session.watch_*` stop being written and are dropped in a later release, because a dropped column breaks pods still running the old code mid-deploy.

### R16. Notifications

- **Decision**:
  - Add `PIPELINE_WEBHOOK` to `SessionOrigin.classified()`, so every CI-woken run gets a `RunEnvelope` and the usual run-finished notification with its summary. This covers FR-018 and SC-007 without a new event type.
  - `PIPELINE_WATCH_EXHAUSTED` is resolved to the ownership's session owner.
  - For an ownerless session, the outcome falls back to a merge request comment.
  - Add an event type for the consecutive-run limit (FR-033).
  - The delegation-only `MAX_RESUMES_IN_A_ROW` is replaced by the general limit.
- **Rationale**: it reuses the existing classifier and channels. The envelope summary is the explanation the owner reads.
- **Alternatives**: a dedicated "CI turn pushed nothing" event type. Rejected: the run notification already carries the summary.

### R17. The working copy follows the remote

- **Decision**: before the agent starts, an inbox-drained run on a warm sandbox fast-forwards the checkout to the remote branch head when the remote moved and the local copy has no unpublished commits. When it can't fast-forward, it leaves the checkout and adds a line to the synthetic context saying so.
  - This is a new `Workspace` operation, so disk and sandbox runs behave alike (`architecture/layering.md` rule 4).
- **Rationale**: FR-024. A warm sandbox is not re-synced today.

### R18. Measuring the agent-visible change (Constitution V)

- **Decision**: add an `inbox` eval marker with three cases:
  1. a CI failure that is not caused by the code: the agent must not push, and must explain;
  2. a combined mention plus CI failure: both are addressed;
  3. the branch moved notice: the agent works from the new head.

  Run before and after with `make eval-prompts SUITES="inbox"` and compare with `evals/compare_runs.py`.
- **Rationale**: R5 changes the input format the model sees. Principle V says SHOULD, and an unmeasured change must say so.

### R19. Delegation through the inbox

- **Decision**:
  - `resume_coordinator` posts a waking `delegation_finished` event (source key `batch:<id>`, payload: the existing batch summary) instead of inserting a QUEUED continuation run.
  - CI outcomes on a delegated job's merge request also post a remembered `delegated_ci_outcome` event to the parent session (FR-021).
  - `continuation_of_batch_id` and its constraint stop being written. The source key gives the same de-duplication.
  - `resume_stalled_batches` keeps its sweep, and posts idempotently for finished batches with no event.
- **Rationale**: one delivery path (US4).

### R20. Follow-up jobs

- **Decision**: a jobs API or MCP `submit_job` with `thread_id` posts a waking `follow_up_job` event and returns the id of the run that will handle it (R3). The payload's `ref` still applies: a ref different from the session's branch is the event's `ref` (R4).
- **Rationale**: FR-021 while keeping `job_id` a Run id.

## Spec refinements accepted 2026-10-30

A maintainer accepted these with the plan, and the spec records them:

1. A run takes pending events for one branch only (R4, narrows FR-010).
2. Follow-up jobs submitted while busy share the next run's `job_id` (R3).
3. With several mentions in one run, the full reply goes to the latest mention's thread, and the others get a link (R10).
4. A run the owner stops dismisses its events rather than retrying them (R12).
5. Every CI-woken run now produces a run notification, not only the ones that give up (R16).
6. GitHub diff-comment mentions need the operator to subscribe the App to `pull_request_review_comment` (R10).
7. Out of scope, and unchanged:
   - Issue-webhook runs on issue sessions keep their own path and still wait for the slot.
   - GitHub CI outside Actions (`check_suite`) remains unseen.
