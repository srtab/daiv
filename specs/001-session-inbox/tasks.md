---
description: "Task list for the Session Inbox feature"
---

# Tasks: Session Inbox

**Input**: Design documents from `specs/001-session-inbox/`

**Prerequisites**: [plan.md](plan.md) (approved 2026-10-30), [spec.md](spec.md), [research.md](research.md), [data-model.md](data-model.md), [contracts/](contracts/), [quickstart.md](quickstart.md)

**Tests**: Included. Constitution principle IV (NON-NEGOTIABLE) requires every behaviour change to ship with tests that fail without it. In each phase, write the test tasks first and confirm they fail before implementing.

**Organization**: Tasks are grouped by user story (US1–US5 from spec.md), so each story can be implemented, tested and shipped on its own after the foundation.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependency on an unfinished task).
- **[Story]**: The user story the task belongs to.
- Paths are repository-relative. Source is under `daiv/`, tests under `tests/unit_tests/` mirroring `daiv/` (`constitutions/patterns/tests.md`), and imports have no `daiv.` prefix.

**Conventions for every task**:
- Run Python tools through `uv run`.
- Async tests that write to the database use `django_db(transaction=True)`.
- Operator-actionable failures log with `logger.error` or `logger.exception`, and no exception text reaches users or the agent (`constitutions/patterns/errors.md`).
- Detailed behaviour goes in module docstrings, not comments.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Place the new package and vocabulary before any code depends on them.

- [X] T001 Create the package `daiv/sessions/inbox/__init__.py` with a module docstring stating the inbox rules from research.md R1–R5. The docstring is where those rules live:
  - an event wakes the agent or is only remembered;
  - one run per session, claimed when the run is created;
  - one QUEUED next run per busy session;
  - a run takes the pending events for its branch;
  - remembered events reach the model as a hidden synthetic message.
- [X] T002 [P] Add the terms "Inbox event", "Owning session" and "Next run" to the table in `constitutions/glossary/README.md`, exactly as in data-model.md "Glossary additions".
- [X] T003 [P] Register the pytest marker `inbox` in `pyproject.toml` (`ini_options.markers`): `"inbox: integration eval of runs built from session inbox events (paid model calls)"`. If the suite needs a per-suite model override, add `INBOX` to `EVAL_SUITE_MODEL_VARS` in `Makefile`.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The inbox itself: the event model, posting, draining with a slot claimed at creation, rendering, completion, retries and the sweep. Every user story depends on this phase.

**⚠️ CRITICAL**: No user story work starts until this phase is complete.

### Tests for the foundation ⚠️ (write first, confirm they fail)

- [X] T004 [P] Write `tests/unit_tests/sessions/inbox/test_kinds.py`. Cover:
  - payload validation per kind: a missing required key raises `ValueError`;
  - the precedence order and run trigger type for each waking kind, per data-model.md "Kinds";
  - source-key and subject helpers.
- [X] T005 [P] Write `tests/unit_tests/sessions/inbox/test_post.py`. Cover:
  - posting the same `(thread_id, source_key)` twice inserts one row and returns `created=False`;
  - a waking event on an idle session schedules `adrain` with `transaction.on_commit`;
  - a waking event on a busy session joins the existing QUEUED next run for its branch, or creates one, and returns its `run_id`; events on different branches get different next runs;
  - a remembered event never drains;
  - an unknown session raises `Session.DoesNotExist`.
- [X] T006 [P] Write `tests/unit_tests/sessions/inbox/test_drain.py`. Cover:
  - a fresh held slot returns `None`;
  - a stale slot (older than `stale_cutoff()`) is treated as free;
  - older pending events with the same `subject` become `superseded`;
  - only events whose `ref` is blank or equals the first waking event's branch are taken;
  - no surviving waking event returns `None`;
  - a waking `delegation_finished` blocked by mute or the run limit becomes remembered, with that `reason`;
  - a QUEUED next run left with no waking event (for example a `ci_failed` replaced by a newer `ci_passed`) is deleted and its events detached;
  - an owner without repository access marks events `failed` with failure `access_denied` and starts no run;
  - the QUEUED next run is promoted to READY, its `trigger_type` set by precedence, and `Session.active_run_id = str(run.pk)` in the same transaction;
  - an `aenqueue_run` failure releases the slot and returns events to `pending`, with `logger.error`.
- [X] T007 [P] Write `tests/unit_tests/sessions/inbox/test_render.py`, per contracts/agent-input.md. Cover:
  - the context message is `synthetic_message(kind="inbox")` and is omitted when empty;
  - oldest-first lines, with "Superseded by …" indented under the replacement;
  - mention and comment quotes are cut at 1,000 characters;
  - over 8,000 characters, the oldest remembered lines collapse into "N more <kind> events", and waking events never do;
  - request sections appear in precedence order;
  - a CI-only run uses the fix prompt.
- [X] T008 [P] Write `tests/unit_tests/sessions/inbox/test_policy.py`. Cover:
  - `Session.inbox_muted` blocks CI and delegation wakes but not `mention`, `chat_message` or `follow_up_job`;
  - the consecutive system-started run limit (default 3) blocks waking after three system-started runs with no person-started run in between;
  - a chat message or mention resets the count.
- [X] T009 [P] Write `tests/unit_tests/sessions/inbox/test_complete.py`, per research.md R12. Cover:
  - a SUCCESSFUL or WAITING_INPUT run marks its events `handled`;
  - a run with `cancel_requested_at` set marks them `dismissed`;
  - a FAILED run returns them to `pending` with `attempts + 1`;
  - the third failure marks them `failed` with `retries_exhausted` and logs with `logger.error`;
  - every outcome then calls `adrain`.
- [X] T010 [P] Extend `tests/unit_tests/sessions/executor/test_lock.py`: the new `Claimed` policy heartbeats, releases the slot only while `active_run_id == holder_id`, and never polls `try_claim`.
- [X] T011 [P] Extend `tests/unit_tests/sessions/executor/test_tasks.py`: `run_job_task` for an inbox run uses `Claimed`, builds `input_messages` from `arender_input`, and passes `acting_user_id = session.user_id`. For a session with no user it passes `None`.
- [X] T012 [P] Extend `tests/unit_tests/sessions/test_tasks.py`:
  - `drain_inboxes_cron_task` drains only sessions with pending waking events and a free or stale slot;
  - it returns `taken` events whose run's slot is stale to `pending` with `attempts + 1`.
- [X] T013 [P] Extend `tests/unit_tests/sessions/test_signals.py`: the `run_ended` receiver calls inbox completion for every run, chat included.

### Implementation for the foundation

- [X] T014 Add `EventKind`, `EventState`, `QuietReason` and `EventFailure` (`TextChoices`), and the `InboxEvent` model, to `daiv/sessions/models.py`, exactly per data-model.md.
  - **Fields**:
    - `id` UUID primary key;
    - `session` FK → `Session` CASCADE, `related_name="inbox_events"`;
    - `kind` Char(32);
    - `source_key` Char(255);
    - `subject` Char(255) blank;
    - `ref` Char(255) blank;
    - `merge_request_iid` PositiveInteger null;
    - `wake` Bool;
    - `reason` Char(32) blank;
    - `payload` JSON;
    - `author` FK → User SET_NULL null;
    - `author_username` Char(255) blank;
    - `state` Char(16);
    - `run` FK → `Run` SET_NULL null, `related_name="inbox_events"`;
    - `claim_token` Char(64) blank;
    - `attempts` PositiveSmallInteger default 0;
    - `failure` Char(32) blank;
    - `created_at` default now;
    - `updated_at` auto_now.
  - **Constraints**: `UniqueConstraint(session, source_key)`, and a `CheckConstraint` on `kind`, `state`, `reason` and `failure`.
  - **Indexes**:
    - `(session, state, created_at)`;
    - a partial index on `session` where `state='pending' AND wake`;
    - a partial index on `run` where `run IS NOT NULL`.
- [X] T015 Add `Session.inbox_muted` (Bool, default False) and `Run.pushed_sha` (Char(64), blank) to `daiv/sessions/models.py`. Generate `daiv/sessions/migrations/0021_inbox_event.py` with `uv run python daiv/manage.py makemigrations sessions`, and extend `tests/unit_tests/sessions/test_migrations.py` if it pins the migration list.
- [X] T016 [P] Implement `daiv/sessions/inbox/kinds.py`:
  - one payload `TypedDict` per kind, as listed in data-model.md "Payloads";
  - `validate_payload(kind, payload)`, raising `ValueError`;
  - the precedence table and the waking-kind → `SessionOrigin` mapping;
  - source-key and subject builders (`pipeline:<id>`, `note:<id>`, `review-comment:<id>`, `batch:<uuid>`, `chat:<id>`, `job:<uuid>`, `push:<sha>`, `ci:<iid>`, `mr-state:<iid>`, `branch:<ref>`).
- [X] T017 Add a `Claimed` lock policy to `daiv/sessions/executor/lock.py` (heartbeat, release at end, never poll). Add `SessionLock.aclaim_for_run(thread_id, run_id)`, a compare-and-set that the drainer calls inside its transaction and that also takes a stale slot, logging a warning as `try_claim` does.
- [X] T018 Implement `daiv/sessions/inbox/policy.py` with `amay_wake(session, kind) -> QuietReason | None`. It applies mute and the consecutive system-started run limit (`MAX_SYSTEM_RUNS_IN_A_ROW = 3`) only to `ci_failed` and `delegation_finished`; `mention`, `chat_message` and `follow_up_job` are never blocked. The limit counts counting the session's most recent runs back to the last run whose trigger is not in `SessionOrigin.daiv_authored()`.
- [X] T019 Implement `daiv/sessions/inbox/post.py` with `apost_event(...) -> PostResult`, exactly per contracts/inbox-service.md:
  - insert with `bulk_create(ignore_conflicts=True)` and re-read on conflict;
  - for a waking event on a busy session, attach it to the session's QUEUED next run for the event's `ref` (defaulting to `Session.ref`), creating one with the kind's provisional trigger type;
  - schedule `adrain` with `transaction.on_commit` only for waking events.
- [X] T020 Implement `daiv/sessions/inbox/render.py` with `arender_input(run) -> RenderedInput(context, request)`, exactly per contracts/agent-input.md. Move `FIX_RUN_PROMPT` here from `daiv/sessions/pipeline_watch/dispatch.py`, unchanged and untranslated.
- [X] T021 Implement `daiv/sessions/inbox/drain.py` with `adrain(thread_id) -> Run | None`, exactly per contracts/inbox-service.md:
  - lock the session row with `select_for_update`;
  - supersede older events per subject;
  - choose the branch;
  - gate with `policy.amay_wake`, turning blocked waking events into remembered ones;
  - delete a QUEUED next run left with no waking event, detaching its events;
  - check access with `sessions.admission.assert_repo_access` unless the session has no user;
  - promote the QUEUED next run, or create a READY one, using `pinned_agent_override(session)` and the session's sandbox environment;
  - claim the slot with `aclaim_for_run`;
  - mark events `taken`;
  - enqueue with `sessions.queue.aenqueue_run` on commit, and on failure release the slot and return events to `pending`.

  Depends on T016–T020.
- [X] T022 Make `run_job_task` in `daiv/sessions/executor/tasks.py` run inbox runs, those with linked `inbox_events`: use the `Claimed` policy, set `input_messages` from `arender_input(run)` (context first, then request), and set `acting_user_id=session.user_id`. Keep the existing `HumanMessage(prompt)` path for runs with no events.
- [X] T023 Implement `daiv/sessions/inbox/complete.py` with `aon_run_ended(run)`, per research.md R12 (mention replies are added in US5). Register a `run_ended` receiver in `daiv/sessions/signals.py` that calls it and then `adrain(run.session_id)`.
- [X] T024 Add `drain_inboxes_cron_task` with `@cron("* * * * *")` to `daiv/sessions/tasks.py`. It recovers stale `taken` events, then drains sessions that have pending waking events and a free or stale slot, at most 200 per tick, as the existing sweeps cap themselves.

**Checkpoint**: an event posted with `apost_event` starts exactly one run, which receives the rendered input. The foundation tests (T004–T013) pass.

---

## Phase 3: User Story 1 - CI failures are handled in the session that produced the merge request (Priority: P1) 🎯 MVP

**Goal**: CI results on a merge request DAIV published reach the publishing session. Whether the agent wakes is decided from facts (R7). A CI run that pushes nothing no longer reads as "attempts exhausted".

**Independent Test**: quickstart.md scenario 1, and the replay of the 2026-10-30 incident.

### Tests for User Story 1 ⚠️

- [X] T025 [P] [US1] Write `tests/unit_tests/sessions/inbox/test_ownership.py`. Cover:
  - every row of data-model.md "Claim rules";
  - every claim sets `last_daiv_sha` from `Run.pushed_sha`;
  - person-started triggers reset `attempts` and the CI and delegation triggers don't;
  - `aowning_session` returns `None` for closed or unowned merge requests, and creates a session for the same person when the owning session was deleted.
- [X] T026 [P] [US1] Extend `tests/unit_tests/automation/agent/test_publishers.py`: `PublishOutcome.pushed_sha` equals `GitManager.head_sha()` after a successful push, and is empty when nothing was published. Extend `tests/unit_tests/automation/agent/test_events.py` for `pushed_sha` in the `daiv_publish_result` payload.
- [X] T027 [P] [US1] Rewrite `tests/unit_tests/sessions/pipeline_watch/test_service.py`. Cover:
  - a green pipeline posts `ci_passed`;
  - a needs-a-person status posts `ci_stopped`;
  - a failure posts `ci_failed` with `wake` and `reason` per each condition of data-model.md "CI wake rule": `watch_off`, `muted`, `not_daiv_commit`, `already_examined`, `budget_spent`, `run_limit`, and a blank `last_daiv_sha` counting as a match;
  - a non-head pipeline posts nothing;
  - a repeated `pipeline_id` posts nothing;
  - `budget_spent` on a new DAIV commit sends `watch_exhausted`.

  Delete `tests/unit_tests/sessions/pipeline_watch/test_dispatch.py` and `test_arm.py`, and move their still-relevant cases to `test_ownership.py`.
- [X] T028 [P] [US1] Rewrite `tests/unit_tests/sessions/pipeline_watch/test_store.py` and `test_reconciler.py` around `MergeRequestOwnership`: no 6-hour expiry, and a re-read of the latest pipeline for open ownerships with no CI event for 30 minutes.
- [X] T029 [P] [US1] Extend `tests/unit_tests/sessions/inbox/test_drain.py`: taking a waking `ci_failed` sets the ownership's `last_examined_sha` to the pipeline SHA and increments `attempts` in the same transaction.
- [X] T030 [P] [US1] Extend `tests/unit_tests/sessions/executor/test_run.py`: after a published run, `aclaim_ownership` is called with the run and its `pushed_sha`, `PipelineWatch.aarm_after_run` is no longer called, and `Run.pushed_sha` is stored.
- [X] T031 [P] [US1] Extend `tests/unit_tests/sessions/test_migrations.py`: the data migration turns a session with `watch_state` `watching` or `fixing` into a `MergeRequestOwnership` row.
  - That session is the owner, with `owner=user`, `attempts=watch_attempts`, `source_branch=ref`, `last_pipeline_id=watch_pipeline_id` and a blank `last_daiv_sha`.
  - Sessions in other states get no row.
- [X] T032 [P] [US1] Extend the notification tests in `tests/unit_tests/notifications/test_signals.py` and `tests/unit_tests/notifications/test_watch_notifier.py`. Cover:
  - in `tests/unit_tests/notifications/test_signals.py`: `pipeline_webhook` runs are classified;
  - in `tests/unit_tests/notifications/test_watch_notifier.py`: the exhausted recipient is the ownership's owner, and an ownerless session falls back to a merge request comment.
- [X] T033 [P] [US1] Extend `tests/unit_tests/sessions/test_header.py` and `tests/unit_tests/sessions/test_api.py`. Cover:
  - the turns poll returns `ci`, a list per owned open merge request with `status`, `attempts`, `max_attempts`, `watch_enabled` and `explanation_run_id`, plus the legacy `watch` derived from the first item;
  - `POST /api/sessions/{id}/ci-watch` toggles `MergeRequestOwnership.watch_enabled`, takes an optional `merge_request_iid`, and answers 409 `not_owned` for a merge request the session doesn't own.
- [X] T034 [P] [US1] Extend `tests/unit_tests/webhooks/managers/test_issue_addressor.py`: the issue addressor's `RunSpec` carries `acting_user_id` resolved from the triggering author.
- [X] T035 [P] [US1] Extend `tests/unit_tests/sessions/inbox/test_complete.py`: when the session has no DAIV user, a SUCCESSFUL run that took a waking `ci_failed` posts its `result_summary` as a merge request comment. A posting failure is logged with `logger.exception` and changes no event state.

### Implementation for User Story 1

- [X] T036 [US1] Add `MergeRequestOwnership` to `daiv/sessions/models.py`, exactly per data-model.md.
  - **Fields**:
    - `repo_id` Char(255);
    - `merge_request_iid` PositiveInteger;
    - `session` FK → `Session` SET_NULL null, `related_name="owned_merge_requests"`;
    - `owner` FK → User SET_NULL null;
    - `source_branch` Char(255);
    - `watch_enabled` Bool default True;
    - `attempts` PositiveSmallInteger default 0;
    - `last_daiv_sha` Char(64) blank;
    - `last_examined_sha` Char(64) blank;
    - `last_pipeline_id` BigInteger null;
    - `closed_at` DateTime null;
    - `created_at`, `updated_at`.
  - **Constraint**: `UniqueConstraint(repo_id, merge_request_iid)`.
  - **Index**: a partial index on `(repo_id, source_branch)` where `closed_at IS NULL`.

  Generate `daiv/sessions/migrations/0022_merge_request_ownership.py`.
- [X] T037 [US1] Write the data migration `daiv/sessions/migrations/0023_migrate_open_watches.py`, per data-model.md "Migrations" step 2, with a no-op reverse.
- [X] T038 [P] [US1] Add `pushed_sha: str = ""` to `PublishOutcome` in `daiv/automation/agent/publishers.py`, set from `GitManager.head_sha()` after every successful push, not only on the `will_heal` path. Carry it in the `daiv_publish_result` event (`daiv/automation/agent/events.py`) and its recorder.
- [X] T039 [US1] Write `Run.pushed_sha` in `_record_publish_result` / `publish_field_updates` (`daiv/sessions/executor/run.py`, `daiv/sessions/models.py`).
- [X] T040 [US1] Implement `daiv/sessions/inbox/ownership.py` with `aclaim_ownership`, `aowning_session` and `aownership_for_branch`, exactly per contracts/inbox-service.md and data-model.md "Claim rules":
  - post `foreign_publish` or `notice` events through `apost_event`;
  - reset `attempts` on claims by person-started triggers.
- [X] T041 [US1] Replace the `arm_watch` step in `daiv/sessions/executor/run.py` (today calling `PipelineWatch.aarm_after_run`) with `aclaim_ownership(run=..., merge_request=..., pushed_sha=...)` when the run published. Then enqueue `evaluate_pipeline_watch_task` for the merge request's source branch. Keep the `RunSpec.arm_watch` flag name as the opt-in.
- [X] T042 [US1] Rewrite `daiv/sessions/pipeline_watch/store.py` to read and write `MergeRequestOwnership`, not `Session.watch_*`, which nothing writes after this task. Update the module docstrings in `daiv/sessions/pipeline_watch/__init__.py` and `store.py`.
- [X] T043 [US1] Rewrite `PipelineWatch` in `daiv/sessions/pipeline_watch/service.py`:
  - `aevaluate` looks up the open ownership by branch, judges with the unchanged `judgment.py`, and posts `ci_passed`, `ci_failed` or `ci_stopped` with `wake` and `reason` from the wake rule. The thread is the ownership's session, through `aowning_session`.
  - It records `last_pipeline_id`, and sends `watch_exhausted` on `budget_spent` for a new DAIV commit.
  - `arequest_evaluation` checks for an open ownership on the branch.
  - `aturn_on` and `aturn_off` toggle `watch_enabled`.
  - Remove `aexhaust`, the no-push path in `aarm_after_run`, and `aarm_after_run` itself, along with `_aact`'s claim-and-dispatch.
- [X] T044 [US1] Delete `daiv/sessions/pipeline_watch/dispatch.py`. Make `daiv/sessions/inbox/drain.py` set `last_examined_sha` and increment `attempts` on the ownership when it takes a waking `ci_failed`, per T029.
- [X] T045 [US1] Update `daiv/sessions/pipeline_watch/reconciler.py`: remove the 6-hour expiry, recover nothing from `FIXING` (that state no longer exists), and re-read the latest pipeline for open ownerships with no CI event for 30 minutes, at most 200 per tick.
- [X] T046 [P] [US1] Add `SessionOrigin.PIPELINE_WEBHOOK` to `SessionOrigin.classified()` in `daiv/sessions/models.py`.
- [X] T047 [P] [US1] Update `daiv/notifications/watch_notifiers.py`: resolve the recipient from the ownership's `owner`, link to the owning session's page, and when there is no owner post the outcome as a merge request comment through `daiv/sessions/pipeline_watch/platform.py` `apost_note`.
- [X] T048 [US1] In `daiv/sessions/inbox/complete.py`, when a run that took a waking `ci_failed` ends SUCCESSFUL on a session with no DAIV user, post its `result_summary` on the merge request through `daiv/sessions/pipeline_watch/platform.py` `apost_note` (FR-018; spec edge case "The owning session has no DAIV user"). Log a posting failure with `logger.exception`.
- [X] T049 [US1] Update `daiv/sessions/header.py`, the turns poll in `daiv/sessions/api/views.py` and `POST /{thread_id}/ci-watch`, exactly per contracts/http.md:
  - `ci` is a list built from the session's owned open merge requests and their latest `ci_*` event;
  - `explanation_run_id` is the last run that took a waking `ci_failed`;
  - the legacy `watch` comes from the first item;
  - the toggle takes an optional `merge_request_iid` and answers 409 `not_owned`.

  Rewrite `header.py`'s docstring.
- [X] T050 [US1] Render the CI status per owned merge request in `daiv/sessions/templates/sessions/_session_topbar.html`, and handle the `ci` list in `daiv/sessions/static/sessions/js/session-page.js`. Show "failing" with a link to the explanation run instead of "Attempts exhausted". Run `uv run --only-group=dev djade --target-version 6.0` on any new template.
- [X] T051 [P] [US1] Pass `acting_user_id` (the resolved DAIV user of the triggering author) in the `RunSpec` built by `daiv/webhooks/managers/issue_addressor.py`, threading it from `daiv/webhooks/tasks.py`.
- [X] T052 [US1] Rewrite `docs/features/pipeline-watch.md` for the new behaviour:
  - the publishing session handles CI;
  - the wake conditions;
  - a run that pushes nothing explains, without giving up;
  - a manual retry shows as passing;
  - per-merge-request toggle.

**Checkpoint**: quickstart.md scenario 1 passes on the local stack. With the foundation, US1 is the MVP release.

---

## Phase 4: User Story 2 - Events that arrive while a session is busy are handled together in its next turn (Priority: P2)

**Goal**: A busy session queues everything into one next run, accepts chat messages instead of answering 409, lets the owner stop a system-started run, supports mute, and enforces the consecutive-run limit.

**Independent Test**: quickstart.md scenario 2.

### Tests for User Story 2 ⚠️

- [X] T053 [P] [US2] Extend `tests/unit_tests/chat/api/test_views.py`. Cover:
  - **Busy session**, meaning the slot is held or a QUEUED next run exists: the POST posts a `chat_message` event keyed `chat:<agui message id>` and returns an AG-UI stream of `RUN_STARTED`, `CUSTOM` `daiv.queued` with `{event_id, run_id, position: "next"}`, and `RUN_FINISHED`. It never returns the old 409s.
  - **Idle session**: `atake_for_chat` runs right after `try_claim`.
  - **Cancel**: `POST /api/chat/cancel` accepts a background run of a session the caller owns and calls `RunManager.cancel`.
- [X] T054 [P] [US2] Extend `tests/unit_tests/chat/api/test_streaming.py`: taken events are linked to the chat `Run` by `alink_chat_run(holder_id, run)` once the row exists, and the synthetic inbox context is the first input message.
- [X] T055 [P] [US2] Extend `tests/unit_tests/sessions/test_tasks.py` with `sync_stuck_runs`: a task-backed `chat` run (one with a `task_result`) is re-synced from its task, and is not failed as an orphaned inline chat run.
- [X] T056 [P] [US2] Extend `tests/unit_tests/sessions/executor/test_lock.py` for `LiveRun.stoppable`: it is true for a background run shown to the session's owner, and false for others.
- [X] T057 [P] [US2] Extend `tests/unit_tests/sessions/test_api.py`: `POST /api/sessions/{id}/inbox-mute` sets `Session.inbox_muted` for the owner or an admin, and answers not found for anyone else. The turns poll returns `queued` with the next run's id and its events' summaries.
- [X] T058 [P] [US2] Extend `tests/unit_tests/notifications/test_choices.py` and add `tests/unit_tests/notifications/test_inbox_notifiers.py`: reaching the consecutive-run limit emits one notification to the session owner per limit reached. Its dedupe key is `"<thread_id>:<first blocked event id>"`.
- [X] T059 [P] [US2] Extend `tests/unit_tests/chat/test_chat_stream_js.py`: on `daiv.queued`, the message renders as queued, and the page switches to following the next run when the turns poll reports it live.

### Implementation for User Story 2

- [X] T060 [US2] Add `atake_for_chat(thread_id, *, holder_id)` and `alink_chat_run(holder_id, run)` to `daiv/sessions/inbox/drain.py`, per contracts/inbox-service.md, using `InboxEvent.claim_token`.
- [X] T061 [US2] Replace the two 409 branches in the chat POST view in `daiv/chat/api/views.py` (the "already in progress" and "waiting to start" checks) with posting a `chat_message` event, carrying the AG-UI message id, text, model and thinking overrides, and attachment references only. Return the queued AG-UI stream. When idle, call `atake_for_chat` right after `try_claim`.
- [X] T062 [US2] In `daiv/chat/api/streaming.py`, call `alink_chat_run` in `on_context_ready` once the chat `Run` exists, and prepend `arender_input`'s context message to the AG-UI input. Once US3 lands (T081), that context includes the fast-forward result.
- [X] T063 [US2] Make a task-backed `chat` run (created by the drainer from a `chat_message` event) safe in the chat-specific paths of `daiv/sessions/tasks.py` and `daiv/chat/api/streaming.py`:
  - `sync_stuck_runs` in `daiv/sessions/tasks.py`: decide by `task_result` presence, not trigger type;
  - `finalize_chat_run` in `daiv/chat/api/streaming.py`: not called for task-backed runs;
  - `SessionOrigin.retryable()`: unchanged.

  Document the rule in the `daiv/sessions/inbox/__init__.py` docstring.
- [X] T064 [US2] Extend `POST /api/chat/cancel` in `daiv/chat/api/views.py` to background runs of a session the caller owns, through `RunManager.cancel`. Set `LiveRun.stoppable` in `daiv/sessions/executor/lock.py` true for background runs shown to the session's owner.
- [X] T065 [US2] Add `POST /{thread_id}/inbox-mute` to `sessions_router` in `daiv/sessions/api/views.py` (owner or `user.is_admin`; others get not found). Add the `queued` field to the turns poll, per contracts/http.md.
- [X] T066 [US2] Add the mute toggle to the topbar menu in `daiv/sessions/templates/sessions/_session_topbar.html` and its handler in `daiv/sessions/static/sessions/js/session-page.js`. Render the queued block (next run plus event summaries) in the session page.
- [X] T067 [US2] Handle the `daiv.queued` custom event in `daiv/chat/static/chat/js/chat-stream.js`: render the user's message as queued, then hand off to the existing live-run following in `daiv/sessions/static/sessions/js/session-stream.js`.
- [X] T068 [US2] Add the consecutive-run limit notification, in a new `daiv/notifications/inbox_notifiers.py` and the files below:
  - a new `EventType` member in `daiv/notifications/choices.py`;
  - a migration if `EventType` is constrained in the database;
  - a renderer in `daiv/notifications/channels/renderers/events.py`;
  - an emitter in a new `daiv/notifications/inbox_notifiers.py`;
  - a receiver in `daiv/notifications/signals.py` for a new `inbox_paused` signal in `daiv/sessions/signal_defs.py`, sent by `daiv/sessions/inbox/policy.py` when the limit first blocks a wake.
- [X] T069 [US2] Remove the delegation-only `MAX_RESUMES_IN_A_ROW` check and `_aresumes_in_a_row` from `daiv/sessions/delegation.py`. The general limit in `policy.py` replaces them (research.md R16). Update `tests/unit_tests/sessions/test_delegation.py`.

**Checkpoint**: quickstart.md scenario 2 passes. A chat message sent during a CI run is queued and handled in one next run.

---

## Phase 5: User Story 3 - Things the agent should know but need not act on are remembered (Priority: P3)

**Goal**: Pushes by people, merges, closes and CI passes are recorded as remembered events, shown in the timeline, and given to the next run. The working copy follows the remote.

**Independent Test**: quickstart.md scenario 3.

### Tests for User Story 3 ⚠️

- [X] T070 [P] [US3] Extend `tests/unit_tests/webhooks/gitlab/test_callbacks.py`. Cover:
  - a push hook to a non-default branch with an open ownership enqueues `classify_push_task` with `run_after` 60 s;
  - a push to the default branch still only invalidates the config cache;
  - MR hook `merge` and `close` on any target post `mr_merged` / `mr_closed` and set `closed_at`, keyed `mr-state:<iid>:merged|closed`;
  - `reopen` clears `closed_at` and posts a `notice`;
  - unowned merge requests post nothing.
- [X] T071 [P] [US3] Extend `tests/unit_tests/webhooks/github/test_callbacks.py` with the same cases for `push` and `pull_request` `closed` (`merged` true or false) and `reopened`. The existing merge-metrics recording still runs.
- [X] T072 [P] [US3] Extend `tests/unit_tests/sessions/test_tasks.py` with `classify_push_task`: an `after` SHA equal to the ownership's `last_daiv_sha` posts nothing; any other SHA posts a remembered `branch_pushed`, keyed `push:<after>`, subject `branch:<ref>`.
- [X] T073 [P] [US3] Extend `tests/unit_tests/sessions/test_transcript.py`: `annotate_transcript` emits `inbox_event` entries for remembered events, interleaved by `created_at` like `run_status` entries, including in `expired_run_turns`.
- [X] T074 [P] [US3] Add Workspace tests in `tests/unit_tests/automation/agent/workspace/`:
  - the fast-forward operation advances a clean checkout to the remote head;
  - it reports "diverged" and leaves the checkout alone when there are unpublished local commits;
  - it reports "unchanged" when the remote didn't move;
  - it behaves the same on the disk and sandbox workspaces.
- [X] T075 [P] [US3] Extend `tests/unit_tests/sessions/inbox/test_render.py`: the branch line states the fast-forward result ("was fast-forwarded" or "could not be fast-forwarded because you have unpublished commits"). Extend `tests/unit_tests/chat/api/test_streaming.py`: an idle chat run after a person's push gets the branch line in its context and starts from the new head.
- [X] T076 [P] [US3] Extend `tests/unit_tests/chat/test_transcript_blocks_js.py`: `inbox_event` entries render as compact timeline rows.

### Implementation for User Story 3

- [X] T077 [P] [US3] Extend the GitLab push and merge request models in `daiv/webhooks/gitlab/models.py` with `before`, `after`, `user_username`, `total_commits_count` and the MR `action`. Route the cases in `daiv/webhooks/gitlab/callbacks.py`, per contracts/webhook-events.md "Pushes and merge request state", keeping default-branch push and merge-metrics handling as is.
- [X] T078 [P] [US3] Extend the GitHub `push` and `pull_request` models in `daiv/webhooks/github/models.py` (`before`, `after`, `pusher`, `commits`; `merged`; actions `closed` and `reopened`) and route them in `daiv/webhooks/github/callbacks.py`, per the same contract.
- [X] T079 [US3] Add `classify_push_task` to `daiv/sessions/tasks.py`, enqueued with a 60 s `run_after` by both callbacks. It compares `after` with the ownership's recorded DAIV SHAs and posts `branch_pushed` through `apost_event`. Add ownership helpers in `daiv/sessions/inbox/ownership.py`: `aclose_ownership` and `areopen_ownership`.
- [X] T080 [US3] Add a fast-forward operation to the `Workspace` protocol in `daiv/automation/agent/workspace/base.py`, returning `"advanced" | "unchanged" | "diverged"`. Implement it in `disk.py` and `sandbox.py` through `git_manager.py` (fetch with the workspace's authenticated git, then fast-forward only).
- [X] T081 [US3] Call the fast-forward in `daiv/sessions/executor/run.py` right after the workspace is acquired, for every session run, inline chat included: chat also goes through `execute_run`. Keep the result on the run context, so `arender_input` (inbox runs) and the chat context prepend (T062) both get the branch line.
- [X] T082 [US3] Emit `inbox_event` pseudo-entries in `daiv/sessions/transcript.py` (`annotate_transcript` and `expired_run_turns`), with summaries built only from payload fields. Add the labels to `daiv/sessions/transcript_labels.py` and the rendering to `daiv/chat/static/chat/js/transcript-blocks.js` and `transcript-labels.js`.
- [X] T083 [US3] Document remembered events and the timeline in `docs/features/sessions.md`.

**Checkpoint**: quickstart.md scenario 3 passes. A person's push and a merge appear in the timeline without starting a run, and the next run works from the new head.

---

## Phase 6: User Story 4 - One delivery path for everything that asks an existing session to act (Priority: P4)

**Goal**: Delegation results and follow-up jobs use the inbox. A delegated job's CI outcomes reach the session that delegated it. Operators can see each session's events.

**Independent Test**: quickstart.md scenario 4.

### Tests for User Story 4 ⚠️

- [X] T084 [P] [US4] Rewrite the resume cases in `tests/unit_tests/sessions/test_delegation.py`:
  - a finished batch posts one waking `delegation_finished` event keyed `batch:<id>`, with the `render_batch_summary` payload;
  - no QUEUED continuation run is inserted;
  - `resume_stalled_batches` posts idempotently.
- [X] T085 [P] [US4] Extend `tests/unit_tests/sessions/pipeline_watch/test_service.py` and `tests/unit_tests/sessions/inbox/test_complete.py`: when the owning session has a parent, `ci_passed`, a `budget_spent` failure and a CI run that pushed nothing each post a remembered `delegated_ci_outcome` to the parent, keyed `child-ci:<thread>:<pipeline id>`.
- [X] T086 [P] [US4] Extend `tests/unit_tests/jobs/api/test_views.py` and `tests/unit_tests/mcp_api/test_server_jobs.py`:
  - a job with `thread_id` on an idle session returns the new run's id;
  - on a busy session, two jobs on the same branch return the same `job_id` (the next run's), and two jobs on different branches return different ones;
  - a `ref` different from the session's branch is stored on the event;
  - the existing ownership, archive and access checks still apply.
- [X] T087 [P] [US4] Extend `tests/unit_tests/sessions/test_models.py`: the next-run constraint rejects a second QUEUED run with an inbox trigger type on the same session and branch, allows one per branch, and allows QUEUED `delegated_job` runs.
- [X] T088 [P] [US4] Extend `tests/unit_tests/sessions/test_views_detail.py`: the inbox panel lists pending and failed events with source, arrival time and state for the owner and admins, and is absent for other viewers.

### Implementation for User Story 4

- [X] T089 [US4] Change `resume_coordinator` and `_resume_batch` in `daiv/sessions/delegation.py` to post `delegation_finished` through `apost_event` instead of `create_run(... status=QUEUED, continuation_of_batch_id=...)`. Make `resume_stalled_batches` post for finished batches that have no event, and update the module docstring. Keep `continuation_of_batch_id` readable for old rows.
- [X] T090 [US4] Post `delegated_ci_outcome` to the parent session from `daiv/sessions/pipeline_watch/service.py`, for passing and `budget_spent`, and from `daiv/sessions/inbox/complete.py`, for a CI-woken run that pushed nothing.
- [X] T091 [US4] Route the `thread_id` continuation in `daiv/sessions/services.py` `asubmit_batch_runs` through `apost_event` with kind `follow_up_job`, returning `PostResult.run_id` as the run id. Keep `daiv/jobs/api/views.py` and `daiv/mcp_api/server.py` responses unchanged in shape.
- [X] T092 [US4] Add the next-run constraint to `Run.Meta.constraints` in `daiv/sessions/models.py`: `UniqueConstraint(session, ref)` where `status='QUEUED' AND trigger_type IN ('chat','mr_webhook','api_job','mcp_job','delegation_resume','pipeline_webhook')`. Add a literal-match test like `test_active_constraint_literals_match_enums`, and generate `daiv/sessions/migrations/0024_run_one_next_run_per_session.py`.
- [X] T093 [US4] Add the inbox panel to the session page, as `daiv/sessions/templates/sessions/_inbox_panel.html`:
  - context in `SessionDetailView` (`daiv/sessions/views.py`), for the owner or `user.is_admin`;
  - a partial `daiv/sessions/templates/sessions/_inbox_panel.html`, included in `session_detail.html`;
  - styles as classes in `daiv/static_src/css/input.css`;
  - `djade` on the new template.
- [X] T094 [P] [US4] Document that a job sent to a busy session joins its next run and may share its `job_id`, in `docs/features/jobs-api.md`, `docs/features/mcp-endpoint.md` and the MCP server instructions text in `daiv/mcp_api/server.py`.

**Checkpoint**: quickstart.md scenario 4 passes. No code path inserts a QUEUED continuation or a READY fix run directly.

---

## Phase 7: User Story 5 - Mentions on a DAIV-published merge request reach the session that owns it (Priority: P5)

**Goal**: Mentions, including diff comments, on an owned merge request go to the owning session, act as its owner, and get replies in the right thread. Comments without a mention are remembered.

**Independent Test**: quickstart.md scenario 5.

### Tests for User Story 5 ⚠️

- [X] T095 [P] [US5] Extend `tests/unit_tests/webhooks/test_dispatch.py`:
  - `dispatch_mr_comment_run` on an owned merge request posts a waking `mention` keyed `note:<id>`, adds the 👀 reaction and creates no merge request thread run;
  - an unowned merge request behaves as today;
  - an author failing `RepositoryConfig.is_user_allowed` posts nothing.
- [X] T096 [P] [US5] Extend `tests/unit_tests/webhooks/gitlab/test_callbacks.py`:
  - a `DiffNote` mention on an owned merge request is accepted, with `is_diff=true` and the `discussion_id` in the payload;
  - on an unowned merge request it is still ignored;
  - a note without a mention on an owned merge request posts a remembered `comment`.
- [X] T097 [P] [US5] Extend `tests/unit_tests/webhooks/github/test_callbacks.py`:
  - `pull_request_review_comment` `created` with a mention on an owned PR posts a `mention` keyed `review-comment:<id>`;
  - without a mention it posts a `comment`;
  - on an unowned PR it posts nothing.

  Extend `tests/unit_tests/webhooks/github/test_views.py` so the payload parses without being mistaken for `PullRequestCallback`.
- [X] T098 [P] [US5] Extend `tests/unit_tests/sessions/inbox/test_complete.py`:
  - a run that took one mention posts its final message in that mention's discussion;
  - with several mentions on one merge request, the full reply goes to the latest mention's thread and a one-line link to each other thread;
  - a WAITING_INPUT run posts its question the same way;
  - a posting failure is logged with `logger.exception`, and the events stay `handled`.
- [X] T099 [P] [US5] Extend `tests/unit_tests/codebase/clients/` (GitHub client tests): `create_merge_request_comment` with a review-comment reply target calls `create_review_comment_reply`. Assert the URL, per the PyGithub lazy-getter pitfall (`is_not_lazy=False`).

### Implementation for User Story 5

- [X] T100 [US5] In `daiv/webhooks/dispatch.py` `dispatch_mr_comment_run`, look up `aowning_session(repo_id, iid)`. When owned, post `mention` through `apost_event`, with author, `author_username`, `note_id`, `discussion_id`, `body` and `web_url`. Keep the reaction. Don't enqueue the comments task. When unowned, keep the existing path.
- [X] T101 [US5] In `daiv/webhooks/gitlab/callbacks.py`, accept `DiffNote` mentions only for owned merge requests, and post non-mention notes on owned merge requests as remembered `comment` events. Bot-authored notes are ignored as today.
- [X] T102 [US5] Add a `pull_request_review_comment` payload model to `daiv/webhooks/github/models.py`, with a discriminator so it doesn't parse as `PullRequestCallback`, and its callback in `daiv/webhooks/github/callbacks.py`, applying the same mention and comment rules.
- [X] T103 [US5] Make the review-comment reply branch in `daiv/codebase/clients/github/client.py` reachable from `create_merge_request_comment` (a typed reply target for review comments), keeping `RepoClient` in `daiv/codebase/clients/base.py` platform-neutral.
- [X] T104 [US5] Implement the mention reply rule in `daiv/sessions/inbox/complete.py`, per research.md R10, using the run's final message and the taken mentions' payloads.
- [X] T105 [P] [US5] Document the routing and diff-comment mentions in `docs/features/pull-request-assistant.md`. Add `pull_request_review_comment` to the GitHub App's **Subscribe to events** list in `docs/getting-started/platform-setup.md`.

**Checkpoint**: quickstart.md scenario 5 passes on GitLab and GitHub.

---

## Phase 8: Polish & Cross-Cutting Concerns

**Purpose**: Evals, docs, changelog, the repository map and the gates.

- [ ] T106 Write `tests/integration_tests/test_inbox.py` (marker `inbox`) with the three cases of research.md R18:
  1. a CI failure not caused by the code: no push, with an explanation;
  2. a mention combined with a CI failure: both addressed;
  3. the branch-moved notice: works from the new head.

  Build the input with `arender_input`.
- [ ] T107 Run the baseline and the comparison, and paste the output into the pull request. If they weren't run, state "unmeasured" (`constitutions/patterns/agent-evals.md`).
  - **Baseline**: on `main`, with the cases' old-format input (the fix prompt alone), run `make eval-prompts SUITES="inbox" OUT=eval-runs/inbox-before.jsonl`.
  - **After**: on this branch, run `make eval-prompts SUITES="inbox" OUT=eval-runs/inbox-after.jsonl`.
  - **Compare**: `uv run evals/compare_runs.py eval-runs/inbox-before.jsonl eval-runs/inbox-after.jsonl`.
- [ ] T108 [P] Update `AGENTS.md`:
  - **Repo map**: `run_job_task` is enqueued by `sessions.inbox.drain`, and `sessions.pipeline_watch.dispatch` is gone.
  - **Invariants**: add `sessions/inbox/` to the "Detailed behaviour lives in module docstrings" examples.
- [ ] T109 [P] Add an `Unreleased` entry to `CHANGELOG.md` in Common Changelog form:
  - the CI watch handles failures in the publishing session;
  - chat messages queue while a session is busy;
  - mentions on DAIV merge requests go to the owning session;
  - **Upgrade note**: subscribe the GitHub App to `pull_request_review_comment`, and migration 0023 converts open CI watches.
- [ ] T110 Run the gates (`Makefile` targets) and record the results:
  - `make lint-fix` (scoped to changed paths, per memory: it mangles untracked WIP);
  - `make lint`;
  - `make lint-typing`;
  - `make test`;
  - `mkdocs build --strict` for the docs changes;
  - `uv run --only-group=dev djade --target-version 6.0` on each new template.
- [ ] T111 Run every scenario in `specs/001-session-inbox/quickstart.md` on the local stack, including the 2026-10-30 replay, and record what ran and what didn't.

---

## Dependencies & Execution Order

### Phase dependencies

- **Setup (Phase 1)**: none.
- **Foundational (Phase 2)**: depends on Setup. Blocks every user story.
- **US1 (Phase 3)**: depends on Foundational. This is the MVP.
- **US2 (Phase 4)**: depends on Foundational only. It can run alongside US1. T069 touches `delegation.py`, which US4's T089 also edits, so finish T069 first.
- **US3 (Phase 5)**: depends on Foundational and US1 (ownership: T036, T040). Its T081 also completes US2's chat context (T062) with the branch line.
- **US4 (Phase 6)**: depends on Foundational and US1 (ownership, `pipeline_watch/service.py`). T092's constraint must land after T061 (queued chat) and T091 (follow-up jobs), so every QUEUED creator goes through the inbox first.
- **US5 (Phase 7)**: depends on Foundational and US1 (`aowning_session`). Its `complete.py` change (T104) follows T090.
- **Polish (Phase 8)**: after the stories that are in the release. T106 and T107 need T020, plus T081 for case 3.

### Within each story

- Tests first, failing; then models, then services, then endpoints and UI, then docs.
- Shared files are edited in sequence, never in parallel:

| File | Tasks |
|---|---|
| `daiv/sessions/models.py` | T014, T015, T036, T046, T092 |
| `daiv/sessions/inbox/drain.py` | T021, T044, T060 |
| `daiv/sessions/inbox/complete.py` | T023, T048, T090, T104 |
| `daiv/sessions/executor/run.py` | T039, T041, T081 |
| `daiv/sessions/api/views.py` | T049, T065 |
| `daiv/webhooks/gitlab/callbacks.py` | T077, T101 |
| `daiv/webhooks/github/callbacks.py` | T078, T102 |

### Parallel opportunities

- Setup: T002 and T003.
- Foundational tests: T004–T013 all touch different files.
- Foundational implementation: T016, then T017 with T018, then T019 and T020; T021 waits for all of them.
- US1 tests T025–T035 run together. Then T038, T046, T047 and T051 alongside the T036–T045 chain.
- After the foundation and US1, US2 can proceed with US3, and US3 with US4 or US5 in separate worktrees, within the shared-file order above.

## Parallel Example: User Story 1

```bash
# The US1 tests together (different files):
Task: "T025 ownership claim-rule tests in tests/unit_tests/sessions/inbox/test_ownership.py"
Task: "T026 pushed_sha publisher tests in tests/unit_tests/automation/agent/test_publishers.py"
Task: "T027 CI event and wake-rule tests in tests/unit_tests/sessions/pipeline_watch/test_service.py"
Task: "T031 open-watch data migration test in tests/unit_tests/sessions/test_migrations.py"

# Independent implementation alongside the ownership chain:
Task: "T038 pushed_sha in daiv/automation/agent/publishers.py"
Task: "T046 classify PIPELINE_WEBHOOK in daiv/sessions/models.py"   # after T036 (same file)
Task: "T047 ownership-based recipient in daiv/notifications/watch_notifiers.py"
Task: "T051 acting_user_id in daiv/webhooks/managers/issue_addressor.py"
```

## Implementation Strategy

### MVP first (Foundation + US1)

1. Phase 1 and Phase 2: the inbox exists and is tested.
2. Phase 3: CI runs in the publishing session. Validate with quickstart.md scenario 1 and the incident replay.
3. **Stop and ship.** This alone fixes the problem in the spec's motivating incident.

### Incremental delivery

1. Foundation + US1 → release (MVP).
2. US2 (busy sessions and queued chat) → release.
3. US3 (remembered events) → release.
4. US4 (delegation and jobs through the inbox, operator panel) → release.
5. US5 (mentions on owned merge requests) → release, with the GitHub App Upgrade note.
6. **Next release after US1 ships**: drop `Session.watch_*` (plan.md Complexity Tracking). It is not part of this task list.

## Notes

- `[P]` means a different file and no unfinished dependency.
- Commit after each task or logical group, with no `Co-Authored-By` lines.
- Each checkpoint is a place to stop, validate the story on its own, and ship.
