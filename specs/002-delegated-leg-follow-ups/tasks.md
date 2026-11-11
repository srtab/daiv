---
description: "Task list for the Delegated Leg Follow-ups feature"
---

# Tasks: Delegated Leg Follow-ups

**Input**: Design documents from `specs/002-delegated-leg-follow-ups/`

**Prerequisites**: [plan.md](plan.md) (draft, awaiting approval), [spec.md](spec.md), [research.md](research.md), [data-model.md](data-model.md), [contracts/](contracts/), [quickstart.md](quickstart.md)

**Tests**: Included. Constitution principle IV (NON-NEGOTIABLE) requires every behaviour change to ship with tests that fail without it. In each phase, write the test tasks first and confirm they fail before implementing.

**Organization**: Tasks are grouped by user story (US1–US5 from spec.md), so each story can be implemented, tested and shipped on its own after the foundation. US4 (full leg replies) does not need the foundation and can ship first.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependency on an unfinished task).
- **[Story]**: The user story the task belongs to.
- Paths are repository-relative. Source is under `daiv/` and tests under `tests/unit_tests/`, mirroring `daiv/` (`constitutions/patterns/tests.md`). Imports have no `daiv.` prefix.

**Conventions for every task**:
- Run Python tools through `uv run`.
- Async tests that write to the database use `django_db(transaction=True)`.
- Operator-actionable failures log with `logger.error` or `logger.exception`. No exception text reaches the agent or a person (`constitutions/patterns/errors.md`).
- Detailed behaviour goes in module docstrings, not comments, and comments follow the organization rules (at most two lines, only where a reader would get it wrong).
- "Leg follow-up" is the term in code, docs and UI labels, never a bare "follow-up" (FR-022). Identifiers use `leg_follow_up` or `LegFollowUp`.
- Exact texts (refusals, outcome words, cut marker, summary lines, tool descriptions) are in [contracts/delegate-jobs-tool.md](contracts/delegate-jobs-tool.md). Copy them from there.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Bring in the eval suite this feature is measured with, record its baseline, and add the vocabulary.

- [X] T001 Bring the `delegation` eval suite into this branch.
  - Check that srtab/daiv#1780 is merged to `main`.
  - Run `git fetch origin main && git merge origin/main` on `claude/delegated-leg-follow-ups`: a merge, not a rebase, because the branch has an open PR.
  - Confirm `uv run pytest --collect-only -m delegation tests/integration_tests` collects five cases.
  - If #1780 is not merged, report that and continue with the other phases. Only T061–T062 and SC-003 wait for it.
- [X] T002 Record the baseline on `main` (research R17, `patterns/agent-evals.md` rule 4).
  - On a clean checkout of `origin/main` that includes the suite, run `make eval-prompts SUITES=delegation OUT=eval-runs/delegation-before.jsonl`. It needs `docker/local/app/config.secrets.env` with an LLM key.
  - If no key is available, note "baseline not run" for the PR description (rule 2). Do not claim results later.
- [X] T003 [P] Add four rows to the table in `constitutions/glossary/README.md`: "Coordinator", "Leg", "Batch" and "Leg follow-up".
  - Take each row's Meaning and Not columns exactly from data-model.md "Glossary additions".
  - Add "a leg follow-up" to the "Inbox event" row's list of examples.
  - Write into the PR #1781 description the impact report Governance requires for a companion change:
    - changed clauses: four glossary rows added, and the "Inbox event" row extended;
    - reason: FR-022;
    - compatibility: none (terms are added, none redefined);
    - enforcement: review only;
    - remaining gaps: none;
    - classification: MINOR, with the reasoning.

    The version stays "1.0.0, proposed" until ratification.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The leg facts (`Leg`), the `leg_follow_up` inbox event with its own run, and how the inbox drains, settles and limits it. Also the branch rule, the batch id parameter and the agent dataclass fields. US1, US2, US3 and US5 depend on this phase; US4 does not.

**⚠️ CRITICAL**: No US1/US2/US3/US5 work starts until this phase is complete.

### Tests for the foundation ⚠️ (write first, confirm they fail)

- [X] T004 [P] Write `tests/unit_tests/sessions/test_legs.py` for `Leg` (data-model.md "Leg"). Cover:
  - `Leg.of(thread_id, coordinator=…)` returns `None` for a session whose `parent_id` is another coordinator, and for a child session with no `DELEGATED_JOB` run;
  - `base_ref` is the `ref` of the oldest `DELEGATED_JOB` run, including after a waiting leg's rebase (`sessions.queue._rebase_on_waited_branch` rewrote it);
  - `has_own_branch()` is false while `session.ref == base_ref` and true once they differ;
  - `merge_request()` is the newest of its runs' merge requests on the session's main repository, and `None` when it has none;
  - `merge_request_state()`:
    - `open` with no `MergeRequestOwnership` row;
    - `merged` after `Ownerships(repo_id).aclose(iid, merged=True, …)`;
    - `closed` after `aclose(iid, merged=False, …)`;
  - `is_busy()` is true for a QUEUED (including one with `waits_for`), READY or RUNNING delegated run, and false when all are terminal;
  - `is_lost()` is true only when the latest delegated run is FAILED with `LINK_FAILED_PREFIX`;
  - `archived_by(user)` is true only for that user's `HiddenSession`.
- [X] T005 [P] Extend `tests/unit_tests/sessions/inbox/test_kinds.py`. Cover:
  - `leg_follow_up` requires `prompt` and `batch_id` (`EventContent.validate_payload` raises `ValueError` without either);
  - it is waking, not in `GATED_KINDS`, and not in `PERSON_KINDS`;
  - `follows_session()` is true, and `run_trigger_type()` is `SessionOrigin.DELEGATED_JOB`;
  - `SourceKey.leg_follow_up(batch_id)` is `"leg-follow-up:<batch_id>"`;
  - `INBOX_TRIGGERS` does not contain `delegated_job`.
- [X] T006 [P] Create `tests/unit_tests/sessions/inbox/test_splits.py`. Cover:
  - a leg follow-up is a split of its own, whether it comes before, between or after a person's chat messages from the same requester;
  - `RequestSplits.takes_context()` is false for a builtin slash command and true for a leg follow-up and for ordinary requests;
  - the existing slash-command splitting is unchanged.
- [X] T007 [P] Create `tests/unit_tests/sessions/inbox/test_leg_follow_ups.py` for `LegFollowUps.post` and `LegFollowUps.end_with_run` (contracts/sessions-services.md). Cover:
  - `post` creates one `leg_follow_up` event and one QUEUED run in one transaction, linked by `InboxEvent.run`. The run has `trigger_type=delegated_job`, the given `batch_id`, `user`, `muted` and `prompt`, `ref=session.ref`, and the leg's `repo_id`;
  - the event is waking, `author` is the given user, and its payload holds `prompt`, `batch_id`, optional `references` and optional `muted`;
  - `drain=True` schedules `Drain(thread_id).start_logged` on commit, and `drain=False` does not;
  - posting while `Leg.is_busy()` raises `LegFollowUpRefused`, as does posting when the merge request is merged or closed. Nothing is created in either case;
  - `NextRuns(session).queued()` does not include the follow-up's run, and `sessions.queue.dispatch_next_queued` leaves it QUEUED;
  - `end_with_run`:
    - a pending follow-up linked to a cancelled run becomes `dismissed`, and one linked to a FAILED, non-cancelled run becomes `failed` with `run_failed`;
    - a taken follow-up whose run failed becomes `failed` with `run_failed`;
    - every other event on the session is untouched.
- [X] T008 [P] Extend `tests/unit_tests/sessions/inbox/test_post.py`: `Inbox.post(kind=EventKind.LEG_FOLLOW_UP, …)` raises `ValueError` and creates nothing.
- [X] T009 [P] Extend `tests/unit_tests/sessions/inbox/test_render.py`: a run that took one leg follow-up and remembered events gets:
  - `RunInput.request()` equal to the payload's `prompt`;
  - a `HumanMessage` with that prompt, after the hidden inbox context message that lists the remembered events.
- [X] T010 [P] Extend `tests/unit_tests/sessions/inbox/test_drain.py`. Cover:
  - on an idle leg session, the drain promotes the follow-up's own run to READY. The run keeps its `batch_id` and gets `ref` set to the take's branch, the requester's user, and the session's pinned agent model. It claims the slot with that run's pk, and no other run is created;
  - the take holds the follow-up plus every remembered event for the branch, and no other waking event;
  - a person's chat message from the same user waits for a run of its own;
  - when the requester may no longer run on the repository:
    - the follow-up event becomes `failed` with `access_denied`;
    - its own run finishes FAILED, `never_started`, with `error_message` equal to the `access_denied` label;
    - `run_finished` is emitted for that run;
  - the promoted run keeps `trigger_type=delegated_job` and takes `muted` from the payload (FR-005);
  - a taken leg follow-up's `references` are merged into `session.external_refs`.
- [X] T011 [P] Extend `tests/unit_tests/sessions/inbox/test_complete.py`. Cover:
  - a follow-up's run ending SUCCESSFUL leaves its event `handled`;
  - ending FAILED (not cancelled) leaves its event `failed` with `run_failed`. Its `attempts` are not bumped back to `pending`, and no new run is ever created for it;
  - ending cancelled leaves it `dismissed`;
  - remembered events the same run took still go through `FailedAttempt` (back to `pending`);
  - `FailedAttempt.record` (the stale-slot sweep path) fails a leg follow-up on its first attempt with `run_failed`, not `retries_exhausted`.
- [X] T012 [P] Extend `tests/unit_tests/sessions/inbox/test_policy.py`. Cover:
  - with three `pipeline_webhook` runs in a row, a pending `leg_follow_up` still wakes, because `quiet_reasons_now` gives it no reason;
  - a `delegated_job` run with a `leg_follow_up` linked counts in `system_runs_in_a_row`;
  - a leg's first `delegated_job` run (no event linked) does not count;
  - `_last_person_run_at` skips follow-up runs, so `report_run_limit` names the right streak.
- [X] T013 [P] Extend `tests/unit_tests/sessions/test_queue.py`. `aenqueue_run` passes:
  - `fork_branch=True` for a leg's first run;
  - `fork_branch=True` for a follow-up run on a leg whose `session.ref == base_ref`;
  - `fork_branch=False` for a follow-up run on a leg with a branch of its own (research R9);
  - `fork_branch=False` for non-delegated runs;
  - `ask_user_enabled=False` for a leg follow-up's run, as for every delegated run (FR-005).
- [X] T014 [P] Extend `tests/unit_tests/sessions/test_services.py`: `asubmit_batch_runs(batch_id=X)` stamps `X` on every created run and returns it in `BatchSubmitResult.batch_id`. Without `batch_id`, a fresh one is minted, as today.

### Implementation for the foundation

- [X] T015 In `daiv/sessions/models.py`:
  - add `EventKind.LEG_FOLLOW_UP = "leg_follow_up", _("Leg follow-up")`;
  - add `EventFailure.RUN_FAILED = "run_failed", _("The run that took it failed")`;
  - add `EventFailure.MR_MERGED = "mr_merged", _("Its merge request was merged")`;
  - add `EventFailure.MR_CLOSED = "mr_closed", _("Its merge request was closed")`;
  - move `LINK_FAILED_PREFIX` here from `daiv/sessions/queue.py`. `queue.py` imports it from `sessions.models`, keeping `from sessions.queue import LINK_FAILED_PREFIX` working, and `daiv/sessions/delegation.py` imports it from `sessions.models`.
- [X] T016 Generate the migration: `uv run python daiv/manage.py makemigrations agent_sessions --name leg_follow_up_choices`. Check that it only alters `choices` on `InboxEvent.kind` and `InboxEvent.failure`, with no column change (research R18).
- [X] T017 Create `daiv/sessions/legs.py` with `Leg`, per contracts/sessions-services.md "`Leg`":
  - the module docstring states what a leg is and each property's definition from data-model.md "Leg";
  - it imports only `sessions.models`;
  - it defines `NOT_RUN_MERGED = "Not run: its merge request was merged"` and `NOT_RUN_CLOSED = "Not run: its merge request was closed"`.
  Makes T004 pass.
- [X] T018 [P] In `daiv/sessions/inbox/kinds.py`:
  - add `LegFollowUpPayload(TypedDict)`: `prompt: str`, `batch_id: str`, `references: NotRequired[list[dict[str, Any]]]`, `muted: NotRequired[bool]`;
  - register it in `PAYLOADS`, and in `PRECEDENCE` at the same rank as `FOLLOW_UP_JOB`;
  - keep it out of `GATED_KINDS` and `PERSON_KINDS`;
  - make `follows_session()` true for it, and make `run_trigger_type()` return `SessionOrigin.DELEGATED_JOB` for it without adding to `_TRIGGERS` or `INBOX_TRIGGERS`;
  - add `SourceKey.leg_follow_up(batch_id)`.
  Makes T005 pass.
- [X] T019 [P] In `daiv/sessions/inbox/render.py`, make `RunInput.request()` (and the HumanMessage it feeds) use `payload["prompt"]` for `EventKind.LEG_FOLLOW_UP`, as for `FOLLOW_UP_JOB`. Makes T009 pass.
- [X] T020 [P] In `daiv/sessions/inbox/splits.py`:
  - make `RequestSplits.runs_alone(event)` true for a waking `leg_follow_up`;
  - add `RequestSplits.takes_context(event)`, false only for a builtin slash command;
  - update the module docstring.
  Makes T006 pass.
- [X] T021 Create `daiv/sessions/inbox/leg_follow_ups.py` with `LegFollowUps(leg)`, `post`/`apost`, `end_with_run` and `LegFollowUpRefused(reason: str)`, per contracts/sessions-services.md:
  - `post` locks the leg session row (`select_for_update`) and re-checks `Leg.is_busy()` and `merge_request_state()` under the lock;
  - it creates the event, keyed by `SourceKey.leg_follow_up(batch_id)`, and its QUEUED run, then links them and calls `session_changed`;
  - with `drain=True`, it schedules the drain with `transaction.on_commit(Drain(...).start_logged, robust=True)`.
  Leave `stop_reason`/`stop` to T041. Makes T007 pass. Depends on T017 and T018.
- [X] T022 In `daiv/sessions/inbox/post.py`, `Inbox.post` raises `ValueError("a leg_follow_up is posted with LegFollowUps.post")` for that kind, before any write. Makes T008 pass.
- [X] T023 In `daiv/sessions/inbox/drain.py` (`_Take.take`, `_promote_or_create`, `_fail`):
  - when `splits.splits[0]` starts with a leg follow-up, take it plus every remembered event for the branch, using `RequestSplits.takes_context`;
  - promote that event's own run (`event.run`) instead of a next run, setting `status=READY`, `ref=branch`, `requester.run_fields(...)`, `prompt` and the pinned `agent_model`/`agent_thinking_level`, `sandbox_environment_id` from `_sandbox_env_id` and `muted` from the payload;
  - `_fail` also finishes the run of each failed leg follow-up FAILED (`never_started=True`, `skip_dispatch=True`, `error_message=str(EventFailure.ACCESS_DENIED.label)`);
  - `_merge_references` also merges a taken `leg_follow_up`'s `references` into the session;
  - update the module docstring.
  Makes T010 pass. Depends on T018, T020 and T021.
- [X] T024 In `daiv/sessions/inbox/complete.py`:
  - `RunEnd.settle_events` first calls `LegFollowUps.end_with_run(self.run)` and excludes leg follow-ups from the `FailedAttempt` it records;
  - `FailedAttempt.record` fails a `leg_follow_up` on its first attempt with `EventFailure.RUN_FAILED`;
  - add the leg follow-up row to the module docstring's table.
  Makes T011 pass. Depends on T021.
- [X] T025 [P] In `daiv/sessions/inbox/policy.py`, add one `Q` expression for "a run DAIV started", used by `system_runs_in_a_row` and `_last_person_run_at`: `trigger_type__in=SessionOrigin.daiv_authored()`, or `trigger_type=DELEGATED_JOB` with an `Exists(InboxEvent(kind=LEG_FOLLOW_UP, run=OuterRef("pk")))`. Leave `SessionOrigin.daiv_authored()` unchanged (research R8). Update the module docstring. Makes T012 pass.
- [X] T026 [P] In `daiv/sessions/inbox/__init__.py`, add the leg follow-up rule from contracts/sessions-services.md "`sessions/inbox` changes" to the module docstring.
- [X] T027 [P] In `daiv/sessions/queue.py` `aenqueue_run`, set `fork_branch=run.trigger_type == SessionOrigin.DELEGATED_JOB and not await Leg(run.session).ahas_own_branch()`, loading the session if it is not loaded. Makes T013 pass. Depends on T017.
- [X] T028 [P] In `daiv/sessions/services.py`, add `batch_id: uuid.UUID | None = None` to `asubmit_batch_runs`, using it in place of `uuid.uuid4()` when given, and document it in the docstring. Makes T014 pass.
- [X] T029 [P] In `daiv/automation/agent/delegation.py`, add the fields from data-model.md "Agent-facing dataclasses":
  - `DelegatedTarget.thread_id: str = ""`;
  - `StartedLeg.leg_follow_up: bool = False`;
  - `FailedLeg.thread_id: str = ""`;
  - `LegStatus.leg_follow_up: bool = False`.
  Update the `JobDelegator.adelegate` docstring: "a target with `thread_id` continues that leg in its own session, branch and merge request".

**Checkpoint**: the foundation is in place. `uv run pytest tests/unit_tests/sessions/ -q` is green, and a leg follow-up can be posted, drained, run and settled in tests. No agent can send one yet.

---

## Phase 3: User Story 1 - The coordinator continues a leg in the leg's own session and merge request (Priority: P1) 🎯 MVP

**Goal**: A `delegate_jobs` target with a leg's `thread_id` continues that leg in its session, pushes to its branch and merge request, and resumes the coordinator once with the batch.

**Independent Test**: A coordinator delegates one leg, which publishes a merge request. The coordinator calls `delegate_jobs` with that leg's `thread_id`. The leg's session gets a second run and no new session is created. The run is enqueued with `fork_branch=False`, and the coordinator is resumed exactly once after it finishes (spec US1 "Independent Test").

### Tests for User Story 1 ⚠️

- [X] T030 [P] [US1] Extend `tests/unit_tests/sessions/test_delegation.py` (use the module's `_delegate`, `_target`, `_checkout` helpers). Cover:
  - **One follow-up**: a target with `thread_id` of a finished leg posts one leg follow-up through `LegFollowUps`. It has the call's `batch_id`, the coordinator run's `user` and `effective_muted`, and the target's prompt and references. No new `Session` is created. The result's `StartedLeg` has `leg_follow_up=True`, the leg's `thread_id` and `session_url`, `ref` equal to the leg session's `ref`, and `starts_after_this_turn=False`;
  - **Mixed call**: one new leg and one follow-up share one `batch_id`. A call with only follow-ups never calls `asubmit_batch_runs`;
  - **Order** (research R11): the follow-up's run exists QUEUED before `asubmit_batch_runs` is called, and its leg session is drained only after it returns;
  - **Access**: `aassert_repo_access` is called with the new legs' repositories only (FR-005a);
  - **Not a leg**: a `thread_id` that is not this coordinator's leg is refused with refusal 1's text and `FailedLeg.thread_id` set, and nothing is posted;
  - **Posting fails**: an unexpected exception posting a follow-up gives a `FailedLeg` with today's "DAIV could not start this leg" text, and `logger.exception` is called;
  - **`_resumes`**: true for a batch whose only run is a follow-up's FAILED `access_denied` run, and false when every run is cancelled;
  - **Resume once**: after the follow-up's run ends SUCCESSFUL (and the new leg's, in a mixed batch), exactly one `delegation_finished` event is posted to the coordinator;
  - **`_leg_status` for a follow-up** (FR-014): `ref` is the leg's `base_ref`; `branch` is the leg's branch even though the follow-up run's `ref` equals it; `merge_request_url` is the leg's merge request when the follow-up run has none; `leg_follow_up=True`;
  - **Summary**: `render_batch_summary` shows `(leg follow-up)` after that leg's status.
- [X] T031 [P] [US1] Extend `tests/unit_tests/automation/agent/middlewares/test_delegate_jobs.py`. Cover:
  - `DelegateTarget` accepts an optional `thread_id`: stripped, and blank means none;
  - `_adelegate` passes it to `DelegatedTarget.thread_id`;
  - the returned JSON carries `leg_follow_up` on started entries and `thread_id` on failed entries;
  - more than `MAX_DELEGATED_TARGETS` targets, counting follow-ups, is refused as today.

### Implementation for User Story 1

- [X] T032 [US1] In `daiv/automation/agent/middlewares/delegate_jobs.py`:
  - add `thread_id: str | None = Field(default=None, description=…)` to `DelegateTarget`, with the description from contracts/delegate-jobs-tool.md "target schema";
  - pass it (stripped, `""` when blank) to `DelegatedTarget` in `_adelegate`;
  - update `DELEGATE_JOBS_DESCRIPTION` per "description changes".
  Makes T031 pass.
- [X] T033 [US1] In `daiv/sessions/delegation.py` `SessionJobDelegator.adelegate`:
  - split targets on `thread_id`;
  - check every follow-up target with `Leg.of(thread_id, coordinator=self._thread_id)`, refusing a non-leg with refusal 1;
  - run `aassert_repo_access` over new legs only;
  - mint `batch_id = uuid.uuid4()`;
  - run the new legs' admission (`check_agent_override`, `aresolve_repo_envs`) before posting anything;
  - post each follow-up with `LegFollowUps(leg).apost(..., drain=False)`, catching `LegFollowUpRefused` into a `FailedLeg` and any other exception with `logger.exception` into `FailedLeg(_UNSTARTED_LEG)`;
  - submit the new legs with `asubmit_batch_runs(..., batch_id=batch_id)` when there are any;
  - drain each posted follow-up's session (`Drain(thread_id).astart` via `start_logged`);
  - return `StartedLeg(leg_follow_up=True, …)` entries in target order.
  Makes the follow-up, mixed, order, access, not-a-leg and posting-fails parts of T030 pass.
- [X] T034 [US1] In `daiv/sessions/delegation.py`:
  - `_legs()` uses `select_related("session", "task_result")`, annotated `is_leg_follow_up=Exists(InboxEvent(kind=LEG_FOLLOW_UP, run=OuterRef("pk")))`;
  - `_resumes` counts a follow-up run as started;
  - `_leg_status` builds `ref`, `branch` and `merge_request_url` from `Leg` (research R14) and sets `leg_follow_up`;
  - `render_batch_summary` appends ` (leg follow-up)` to a follow-up's heading line.
  Makes the remaining T030 parts pass.
- [X] T035 [US1] Rewrite the module docstring of `daiv/sessions/delegation.py`. "A leg's first run is its whole answer" no longer holds. State how a leg follow-up joins its call's batch, why the follow-ups are posted before the new legs are submitted and drained after (R11), and that `_resumes` counts follow-up runs.

**Checkpoint**: US1 works. The coordinator can follow up any leg of its own, and the work lands on the leg's merge request. The other refusals come in US2.

---

## Phase 4: User Story 2 - The coordinator is told, per leg, why a leg follow-up cannot be sent (Priority: P1)

**Goal**: Every refusal from FR-008, the whole-call refusal from FR-010, and the stop at start from FR-008a, each with a reason that says what to do instead.

**Independent Test**: One call names a non-leg, a leg still working, and a valid finished leg. The first two come back refused with their reasons, and the third starts. A leg whose merge request merges while its follow-up waits stops at start with "not run" (spec US2 "Independent Test").

### Tests for User Story 2 ⚠️

- [X] T036 [US2] Extend `tests/unit_tests/sessions/test_delegation.py`. Run after T030, in the same file. Cover:
  - refusals 2–7, each with its exact text from contracts/delegate-jobs-tool.md "Refusals" and `FailedLeg.thread_id` set:
    - a `ref` given;
    - another `repo_id`;
    - archived by the coordinator run's user (a `HiddenSession` for another user is not refused);
    - busy: a running delegated run, a QUEUED follow-up, and a leg QUEUED with `waits_for`;
    - lost;
    - merge request merged, and merge request closed (`Ownerships.aclose`);
  - when several apply, the earliest in the contract's order wins;
  - a refused follow-up beside a valid one: the valid one starts, and the result lists both;
  - a race where `LegFollowUps.apost` raises `LegFollowUpRefused` maps to the busy or merged text;
  - the same `thread_id` twice raises `DelegationError` with the FR-010 text from `check_delegation`, and nothing is posted or submitted.
- [X] T037 [P] [US2] Extend `tests/unit_tests/sessions/inbox/test_leg_follow_ups.py` and `tests/unit_tests/sessions/inbox/test_drain.py`. Cover:
  - `LegFollowUps.stop_reason` returns `MR_MERGED`, `MR_CLOSED` or `None`;
  - when the drain's first split is a follow-up whose leg's merge request was closed through `Ownerships.aclose` after posting (merged and not merged):
    - the event becomes `failed` with `mr_merged` or `mr_closed`;
    - its run finishes FAILED, `never_started`, with `error_message` equal to `NOT_RUN_MERGED` or `NOT_RUN_CLOSED`;
    - no task is enqueued;
    - a new drain is scheduled on commit.
- [X] T038 [US2] Extend `tests/unit_tests/sessions/test_delegation.py` (after T036, same file). Cover:
  - `leg_state` returns `not_run_merged` and `not_run_closed` for those labels;
  - `_OUTCOMES` words them "not run: its merge request was merged" and "…closed";
  - `outcome_states()` reads them back;
  - the summary gives such a leg no reply and adds "- Next: delegate a new leg for further work.";
  - it counts as not succeeded, and the batch still resumes the coordinator.
- [X] T039 [P] [US2] Extend `tests/unit_tests/sessions/test_transcript.py` and `tests/unit_tests/sessions/test_transcript_labels.py`:
  - `_marker` gives a not-run run the "aborted" status with its label as message;
  - `leg_not_run_merged` and `leg_not_run_closed` exist with the texts from contracts/session-page.md "Labels".

- [X] T040 [P] [US2] Extend `tests/unit_tests/notifications/test_tasks.py` and `tests/unit_tests/notifications/test_run_notifiers.py`. Cover:
  - `classify_run_task` gives a FAILED run whose `error_message` is `NOT_RUN_MERGED` or `NOT_RUN_CLOSED` an `all-clear` envelope with that label as its summary, without calling the classification model;
  - any other FAILED run still gets a `failed` envelope, as today;
  - in a batch with another run, `_handle_batch_completion` counts the not-run run neither in `failed` nor in `notable`.

### Implementation for User Story 2

- [X] T041 [US2] In `daiv/sessions/inbox/leg_follow_ups.py`, add `stop_reason` and `stop` per contracts/sessions-services.md. In `daiv/sessions/inbox/drain.py` `_Take.take`, call them before promoting a follow-up's run, then return `None` and schedule `Drain(...).start_logged` on commit. Makes T037 pass.
- [X] T042 [US2] In `daiv/sessions/delegation.py` `adelegate`:
  - add the duplicate-`thread_id` check to `check_delegation`, the call validation the eval suite's recording delegator shares since #1780, raising `DelegationError` with the FR-010 text;
  - apply refusals 2–7 in contract order after refusal 1, using `Leg`;
  - map `LegFollowUpRefused.reason` to the busy or merged/closed text.
  Makes T036 pass.
- [X] T043 [US2] In `daiv/sessions/delegation.py`:
  - `leg_state` recognises `NOT_RUN_MERGED` and `NOT_RUN_CLOSED` in `error_message`, before the generic `failed`;
  - add the two words to `_OUTCOMES`;
  - `render_batch_summary` writes the "Next" line for them.
  In `daiv/automation/agent/middlewares/delegate_jobs.py`, add the two words to `CHECK_DELEGATED_JOBS_DESCRIPTION`'s status list. Makes T038 pass.
- [X] T044 [P] [US2] In `daiv/sessions/transcript.py` `_marker`, a FAILED delegated run whose `error_message` is `NOT_RUN_MERGED` or `NOT_RUN_CLOSED` gets `status="aborted"` with that label. Add `leg_not_run_merged` and `leg_not_run_closed` to `daiv/sessions/transcript_labels.py`, and the fallbacks to `daiv/chat/static/chat/js/transcript-labels.js`. Makes T039 pass.

- [X] T045 [P] [US2] In `daiv/notifications/tasks.py` `classify_run_task`, before the FAILED gate, give a run whose `error_message` is `sessions.legs.NOT_RUN_MERGED` or `NOT_RUN_CLOSED` an `all-clear` envelope with that label as its summary and no actionable items (research R16). Notifications sit above `sessions` in `.importlinter`, so the import is allowed. Add the rule to the docstring's list of invariants. Makes T040 pass. Depends on T017, which defines the labels.

**Checkpoint**: US1 and US2 together are the follow-up MVP. Every refusal and the not-run stop are covered.

---

## Phase 5: User Story 3 - A leg follow-up to a busy leg waits its turn and keeps the batch open (Priority: P2)

**Goal**: A leg follow-up waits behind the leg's own work, never runs beside it, keeps its batch open, and can be cancelled without touching anything else waiting on the leg.

**Independent Test**: A leg session is running a CI-fix run when a follow-up arrives. The follow-up starts only after that run ends, its input includes events remembered meanwhile, and the coordinator is resumed after the follow-up, not after the CI fix (spec US3 "Independent Test").

### Tests for User Story 3 ⚠️

- [X] T046 [P] [US3] Extend `tests/unit_tests/sessions/inbox/test_drain.py` (after T037, same file). Cover:
  - with the leg's slot held by a CI-fix run, posting a follow-up starts nothing;
  - once the slot is released, the drain starts the follow-up alone, and its `RunInput` context lists a `branch_pushed` event remembered meanwhile;
  - a CI failure and a chat message waiting beside it each get later runs of their own, in arrival order.
- [X] T047 [US3] Extend `tests/unit_tests/sessions/test_delegation.py` (after T038, same file). Cover:
  - a batch with a finished new leg and a waiting follow-up posts no `delegation_finished` until the follow-up's run ends;
  - `acancel` on a waiting follow-up cancels its run, leaves its event `dismissed`, and leaves a CI failure waiting on the same leg `pending` and waking (SC-006);
  - a batch of only follow-ups, all cancelled before starting, posts no resume and `CancelResult.resumes` is false;
  - cancelling a running follow-up sets `cancel_requested_at`, and its status word is `stopping`.
- [X] T048 [P] [US3] Extend `tests/unit_tests/sessions/test_inbox_display.py`, `tests/unit_tests/sessions/inbox/test_render.py` and `tests/unit_tests/chat/test_chat_stream_js.py`. Cover:
  - `bubble()` for a `leg_follow_up` has the prompt as `text`, the kind `leg_follow_up`, and `received=False`;
  - `EventText(event).summary()` for a leg follow-up is its prompt;
  - `bubbleKindLabel` gives "Leg follow-up" for a `leg_follow_up` bubble.

### Implementation for User Story 3

- [X] T049 [P] [US3] Show the leg follow-up on the leg's page:
  - in `daiv/sessions/inbox_display.py` `bubble`, add `case EventKind.LEG_FOLLOW_UP: text = payload["prompt"]`;
  - in `daiv/sessions/inbox/render.py` `EventText.summary`, add the same case;
  - in `daiv/chat/static/chat/js/chat-stream.js` `bubbleKindLabel`, return `T.t("inbox_leg_follow_up")` for a `leg_follow_up` bubble;
  - add the fallback `inbox_leg_follow_up: "Leg follow-up"` to `daiv/chat/static/chat/js/transcript-labels.js`. The server already builds that label from the event kind.

  Makes T048 pass.
- [X] T050 [US3] Make T046 and T047 pass, fixing any gap they show in `daiv/sessions/delegation.py` (`_cancel`) or `daiv/sessions/inbox/leg_follow_ups.py` (`end_with_run`). In `daiv/automation/agent/middlewares/delegate_jobs.py`, add to `CANCEL_DELEGATED_JOBS_DESCRIPTION` that `thread_ids` may name a leg follow-up: a waiting one never starts, and a running one stops as a leg does.

**Checkpoint**: Follow-ups behave on busy legs, and cancel is precise.

---

## Phase 6: User Story 4 - The coordinator reads a leg's whole reply (Priority: P2)

**Goal**: Replies up to 4,000 characters reach the resume and the single-batch status whole, and longer ones are cut with a pointer. The overview stays at 1,000.

**Independent Test**: A leg replies with about 2,600 characters, its decisive finding last. The resume input and `check_delegated_jobs(batch_id)` both contain it (spec US4 "Independent Test"). This phase needs only Setup, so it can ship before Phase 2.

### Tests for User Story 4 ⚠️

- [X] T051 [US4] Extend `tests/unit_tests/sessions/test_delegation.py`, ordered against the US1–US3 edits to that file. Build legs whose `DBTaskResult.return_value["response"]` is the full reply and whose `result_summary` is cut at 2,000, as `run_job_task` leaves them. Cover:
  - a 2,600-character reply is whole in `render_batch_summary` and in `astatus(batch_id=…)`;
  - a 4,000-character reply is whole;
  - a 4,500-character reply is its first 4,000 characters, then the cut marker `[cut at 4,000 characters: the rest is in the leg's session, <session_url>]`;
  - `astatus(batch_id=None)` cuts at 1,000 with the marker naming 1,000;
  - a run with no task result falls back to `result_summary`.

### Implementation for User Story 4

- [X] T052 [US4] In `daiv/sessions/delegation.py`:
  - set `LEG_REPLY_MAX_CHARS = 4000` and add `LEG_OVERVIEW_REPLY_MAX_CHARS = 1000`;
  - `_leg_status(leg, *, reply_limit)` reads `leg.response_text.strip()`, cuts it at `reply_limit`, and appends the cut marker from contracts/delegate-jobs-tool.md "Cut marker";
  - `_status` passes 4,000 for a named batch and 1,000 for the overview, and `render_batch_summary` and `_cancel` pass 4,000;
  - `_legs()` and `_resume_batch`'s query `select_related("task_result")`, if T034 has not already.
  Makes T051 pass.
- [X] T053 [P] [US4] In `daiv/automation/agent/middlewares/delegate_jobs.py` `CHECK_DELEGATED_JOBS_DESCRIPTION`, say that `reply` is up to 4,000 characters for a named batch and 1,000 for the overview, and that a cut reply names the leg's session.

**Checkpoint**: SC-007 holds. Eval case 5 (`test_a_finding_deep_in_a_legs_reply_reaches_the_report`) is expected to pass after this phase.

---

## Phase 7: User Story 5 - The coordinator knows how to address a leg, and the session page shows each leg once (Priority: P3)

**Goal**: The resume and status name each leg's session id. The coordinator is told when to follow up and when to delegate anew. The session page counts each leg session once, and the call card marks follow-ups. The eval suite measures the guidance.

**Independent Test**: After a batch with one follow-up, the resume names each leg's session id. The coordinator's page counts each leg session once and lists the followed-up leg under both batches. In the eval suite, an incomplete leg gets a follow-up and an independent part gets a new leg (spec US5 "Independent Test", SC-001, SC-002).

### Tests for User Story 5 ⚠️

- [X] T054 [US5] Extend `tests/unit_tests/sessions/test_delegation.py` (after T051, same file). Cover:
  - every leg section of `render_batch_summary` has `- Session id: <thread_id>`;
  - the closing paragraph is the one from contracts/delegate-jobs-tool.md "Resume summary";
  - `_leg_payload` carries `leg_follow_up`, and `thread_id` only for a session the viewer may see (`""` otherwise).
- [X] T055 [P] [US5] Extend `tests/unit_tests/sessions/test_header.py`: a leg session with runs in two batches appears in both `batches[].legs`, and `total` counts it once.
- [X] T056 [P] [US5] Extend `tests/unit_tests/chat/test_tool_renderers_js.py`. Cover:
  - a call whose target has `thread_id` matches the server leg with that `thread_id`, even when another leg shares its repository and branch;
  - the row is labelled "Leg follow-up";
  - a failed entry with `thread_id` and no matching server leg is a row of its own with its error.

### Implementation for User Story 5

- [X] T057 [US5] In `daiv/sessions/delegation.py`:
  - `render_batch_summary` writes `- Session id: <thread_id>` after `- Session:`, and replaces the closing paragraph with the contract's text;
  - `_leg_payload` adds `leg_follow_up`, and `thread_id` when visible;
  - `load_delegations` annotates `is_leg_follow_up` alongside `_LEG_FIELDS`.
  Makes T054 pass.
- [X] T058 [P] [US5] In `daiv/sessions/header.py` `delegations_for`, set `total` to the number of distinct `session_id`s across `delegations.batches`, and update the docstring. Makes T055 pass.
- [X] T059 [P] [US5] In `daiv/chat/static/chat/js/tool-renderers.js`:
  - `mergedLegs` and `claimTarget`/`delegatedTable` match on `thread_id` first and fall back to today's repository-and-branch matching;
  - `resultLegs` keeps `leg_follow_up` and `thread_id`;
  - follow-up rows show the `leg_follow_up` label.
  Add `leg_follow_up: "Leg follow-up"` to `daiv/sessions/transcript_labels.py` and `daiv/chat/static/chat/js/transcript-labels.js`. Makes T056 pass.
- [X] T060 [US5] In `daiv/automation/agent/middlewares/delegate_jobs.py` `DELEGATE_JOBS_SYSTEM_PROMPT`, replace step 5 and add the leg follow-up paragraph after the `ref` paragraph, both from contracts/delegate-jobs-tool.md "System prompt" (FR-018).
- [X] T061 [US5] In `tests/integration_tests/test_delegation.py`, extend `RecordingDelegator`, which puts each leg in the database since #1780. For a target with `thread_id`, it creates a QUEUED `delegated_job` run with the call's `batch_id` on that leg's existing session, creating no session, and returns `StartedLeg(leg_follow_up=True, …)`. Reword case 5's failure message, which names `LEG_REPLY_MAX_CHARS` as the cap the coordinator sees. Add two cases built with the suite's `_resumed()`:
  - `test_a_leg_that_reports_its_work_incomplete_gets_a_leg_follow_up` (SC-001): one leg's reply says its tests still fail. Assert one `delegate_jobs` call whose only target names that leg's `thread_id` and no `ref`.
  - `test_an_independent_second_part_gets_a_new_leg` (SC-002): every leg succeeded, and the original request has an independent second part. Assert a `delegate_jobs` call with a target without `thread_id`.
  Give the legs `DBTaskResult.return_value` responses, so replies come from `response_text`. Check with `uv run pytest --collect-only -m delegation tests/integration_tests`: seven cases.
- [X] T062 [US5] Run the after-pass and compare. Depends on T001, T002 and every agent-facing task: T032, T043, T050, T053, T057, T060, T061.
  - `make eval-prompts SUITES=delegation OUT=eval-runs/delegation-after.jsonl`, with the same `EVAL_MODEL` as T002.
  - `uv run evals/compare_runs.py eval-runs/delegation-before.jsonl eval-runs/delegation-after.jsonl`, then confirm any `unconfirmed` verdict with the printed `CASES=` re-runs and `--confirm`.
  - Expected: no confirmed regression among the five baseline cases; case 5 flips to passing (SC-003); each new case passes a majority of three votes (SC-001, SC-002).
  - Keep the output for the PR (T067). If evals cannot run, record that as T002 does.

**Checkpoint**: Every user story is complete, and the agent-behaviour change is measured.

---

## Phase 8: Polish & Cross-Cutting Concerns

- [X] T063 [P] Update `docs/features/orchestration.md` per contracts/session-page.md "Docs":
  - add a "How it works" paragraph;
  - add a "Following up a leg" section after "Where a leg's work lands";
  - add the "Rules and limits" rows: one leg follow-up outstanding per leg; follow-ups count toward the 10-target limit; the refusals; a follow-up always wakes the leg; replies up to 4,000 characters, 1,000 in the overview;
  - in "Seeing the batch", say a leg is counted once, list the two "not run" states, and describe the "Leg follow-up" label on the card.
- [X] T064 [P] Update `docs/features/sessions.md`: the inbox section lists the "Leg follow-up" event, and the delegated-batch paragraph says a leg follow-up runs in the leg's session as a Delegated Run.
- [X] T065 [P] Update `CHANGELOG.md` under `Unreleased`:
  - under **Added**, an entry for leg follow-ups (continue a leg in its own session, branch and merge request; per-target refusals; never retried) and the longer leg replies (up to 4,000 characters, from the full answer);
  - in the existing **Orchestration** entry, remove the stale "in the top bar (how many run, wait or failed)" (research refinement 4).
  Common Changelog form, no Upgrade note.
- [X] T066 [P] In `AGENTS.md`'s `daiv/jobs/` repo-map line, add "leg follow-ups (`sessions.inbox.leg_follow_ups`)" to the list of what `sessions.inbox.drain` starts runs from. Note that `delegate_jobs` can now continue a leg (`sessions.delegation`).
- [X] T067 Run the gates and record what ran (`patterns/tests.md` rule 5):
  - `make lint-fix && make lint`, then `rm -rf .ruff_cache` and `uv run --only-group=dev ruff check --no-cache .`, if lint looks suspiciously clean;
  - `make lint-typing`;
  - `make test`;
  - `uv run --only-group=docs mkdocs build --clean --strict`.
  Then update the PR #1781 description: the Test plan with these results, and the T062 `compare_runs.py` table or the "evals not run" statement.
- [ ] T068 Walk quickstart.md section 3 (manual end-to-end on `make up`) and record which scenarios ran and their outcome in the PR. Report any scenario not run as not run.

---

## Dependencies & Execution Order

### Phase dependencies

- **Setup (Phase 1)**: T001 waits on srtab/daiv#1780 being merged. T002 needs T001's suite on `main`. T003 has no dependency.
- **Foundational (Phase 2)**: depends on nothing in Setup. It blocks US1, US2, US3 and US5.
- **US1 (Phase 3)**: depends on Foundational. MVP, with US2.
- **US2 (Phase 4)**: depends on US1 (T033's refusal plumbing in `adelegate`).
- **US3 (Phase 5)**: depends on Foundational and US1 (a follow-up must be sendable to exercise the batch and cancel paths).
- **US4 (Phase 6)**: depends on nothing but the shared file order below. It can ship first, as plan slice 1.
- **US5 (Phase 7)**: T054–T060 depend on US1. T061–T062 depend on T001–T002 and on every agent-facing task (listed in T062).
- **Polish (Phase 8)**: after the stories in the release.

### Within each story

- Tests first, failing; then models, then services, then the agent-facing text, then UI, then docs.
- Shared files are edited in sequence, never in parallel:

| File | Tasks, in order |
|---|---|
| `daiv/sessions/delegation.py` | T033, T034, T035, T042, T043, T050, T052, T057 (T052 may go first if US4 ships first; then T034 only adds the annotation) |
| `tests/unit_tests/sessions/test_delegation.py` | T030, T036, T038, T047, T051, T054 (T051 may go first with T052) |
| `daiv/automation/agent/middlewares/delegate_jobs.py` | T032, T043, T050, T053, T060 |
| `daiv/sessions/inbox/drain.py` | T023, T041 |
| `tests/unit_tests/sessions/inbox/test_drain.py` | T010, T037, T046 |
| `daiv/sessions/inbox/leg_follow_ups.py` | T021, T041, T050 |
| `tests/unit_tests/sessions/inbox/test_leg_follow_ups.py` | T007, T037 |
| `daiv/sessions/transcript_labels.py` | T044, T059 |
| `daiv/chat/static/chat/js/transcript-labels.js` | T044, T049, T059 |
| `daiv/sessions/inbox/render.py` | T019, T049 |
| `tests/unit_tests/sessions/inbox/test_render.py` | T009, T048 |
| `daiv/sessions/models.py` | T015 (then T016 generates the migration) |

### Parallel opportunities

- Setup: T003 alongside T001–T002.
- Foundational tests: T004–T014 all touch different files.
- Foundational implementation: T015, then T016 and T017. T018, T019, T020, T025, T026 and T028 go together; T029 runs anytime. T021 waits for T017 and T018; T027 for T017; T022 after T021; T023 after T018, T020 and T021; T024 after T021.
- US1: T030 and T031 together, then T032 alongside T033.
- US2: T037, T039 and T040 alongside T036, and T044 and T045 alongside T041–T043.
- US3: T046 and T048 alongside T047, and T049 alongside T050.
- US4: in full alongside Phase 2, given the shared-file order.
- US5: T055 and T056 alongside T054, and T058 and T059 alongside T057.
- Polish: T063–T066 together.

## Parallel Example: Foundation

```bash
# The foundation's tests together (different files):
Task: "T004 Leg facts in tests/unit_tests/sessions/test_legs.py"
Task: "T005 leg_follow_up kind in tests/unit_tests/sessions/inbox/test_kinds.py"
Task: "T006 splits in tests/unit_tests/sessions/inbox/test_splits.py"
Task: "T007 LegFollowUps.post and end_with_run in tests/unit_tests/sessions/inbox/test_leg_follow_ups.py"
Task: "T012 DAIV-started expression in tests/unit_tests/sessions/inbox/test_policy.py"
Task: "T013 fork rule in tests/unit_tests/sessions/test_queue.py"

# Independent implementation once T015-T017 land:
Task: "T018 kind in daiv/sessions/inbox/kinds.py"
Task: "T020 splits in daiv/sessions/inbox/splits.py"
Task: "T025 policy expression in daiv/sessions/inbox/policy.py"
Task: "T028 batch_id in daiv/sessions/services.py"
Task: "T029 dataclass fields in daiv/automation/agent/delegation.py"
```

## Parallel Example: User Story 5

```bash
Task: "T055 distinct total in tests/unit_tests/sessions/test_header.py"
Task: "T056 thread_id matching in tests/unit_tests/chat/test_tool_renderers_js.py"
# then
Task: "T058 distinct total in daiv/sessions/header.py"
Task: "T059 thread_id matching in daiv/chat/static/chat/js/tool-renderers.js"
```

## Implementation Strategy

### MVP first

1. **US4 alone** (plan slice 1): longer replies, after T001–T002 if the eval baseline is to show case 5 flipping. It is shippable by itself.
2. **Foundation + US1 + US2** (slices 2–3): leg follow-ups with every refusal and the not-run stop. Validate with the quickstart.md unit scenarios and manual steps 1–4. **Stop and ship.**

### Incremental delivery

1. US4 → release.
2. Foundation + US1 + US2 → release (the follow-up MVP).
3. US3 → release, with busy legs and precise cancel verified. Most of its behaviour already ships with the foundation; this phase proves it and finishes the leg's inbox display.
4. US5 → release, with the coordinator's guidance, the session page, and the eval comparison (T062) in the PR.
5. Polish rides with whichever release it documents. The CHANGELOG entry lands with the first release that changes behaviour users see.

## Notes

- [P] tasks touch different files and depend on no unfinished task.
- Each user story is testable on its own at its checkpoint.
- Confirm tests fail before implementing (Constitution IV).
- Commit after each task or logical group, with the repository's commit style, and push. PR #1781 is watched.
- Never claim an eval result without T062's comparison (`patterns/agent-evals.md` rule 3).
