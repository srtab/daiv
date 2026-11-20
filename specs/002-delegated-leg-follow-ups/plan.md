# Implementation Plan: Delegated Leg Follow-ups

**Branch**: `claude/delegated-leg-follow-ups` (spec-kit feature `002-delegated-leg-follow-ups`) | **Date**: 2026-11-09 | **Spec**: [spec.md](spec.md) | **Status**: Draft, awaiting maintainer approval, including the [spec refinements](research.md#spec-refinements-for-approval)

**Input**: Feature specification from `specs/002-delegated-leg-follow-ups/spec.md`

## Summary

A `delegate_jobs` target can name a leg's `thread_id` to send it a **leg follow-up**. The follow-up continues that leg
in its own session, branch and merge request instead of starting a new one.

**Delivery.** It reaches the leg as a new `leg_follow_up` inbox event. The event is posted together with its own QUEUED
`DELEGATED_JOB` run, which carries the call's `batch_id`, so:
- the batch counts it from the moment it is sent;
- status and cancel cover it unchanged;
- `run_finished` resumes the coordinator as for any leg.

**Running.** The drain:
- takes the follow-up alone, with the branch's remembered events as context;
- promotes its own run;
- stops it, without running, when the leg's merge request was merged or closed meanwhile.

The follow-up's run is never retried and always wakes the leg, but it counts toward the leg's streak of DAIV-started
runs. A follow-up forks a new branch only when the leg has none of its own yet.

**What the coordinator reads.** Each leg's reply comes from its full final answer, up to 4,000 characters, with a cut
marker past that, and its session id is named. The resume tells the coordinator to follow up a leg's own work and to
delegate a new leg for separate work. The session page counts each leg session once.

The decisions behind each part are in [research.md](research.md) (R1–R18).

## Technical Context

**Language/Version**: Python 3.14

**Primary Dependencies**:
- Django 6 under ASGI, and django-ninja (unchanged routers);
- `django-tasks-db` (`run_job_task`);
- Deep Agents on LangGraph (`DelegateJobsMiddleware`);
- Alpine.js for the session page and the transcript card.

**Storage**: PostgreSQL, with no new tables or columns. There are two choices-only migrations on `InboxEvent`: `kind`
gains `leg_follow_up`, and `failure` gains `run_failed`, `mr_merged` and `mr_closed`. Redis is unchanged.

**Testing**:
- pytest, pytest-django and pytest-asyncio (`asyncio_mode = "auto"`); async database tests use
  `django_db(transaction=True)`;
- the JS card logic is tested through `tests/unit_tests/chat/test_tool_renderers_js.py`;
- prompt evals run with `make eval-prompts SUITES=delegation` (PR #1780's suite, extended by two cases).

**Target Platform**: Linux containers. Web and worker processes share the database; `run_job_task` runs on workers.

**Project Type**: Django web service, with background workers running the agent.

**Performance Goals**:
- A follow-up to an idle leg starts as an inbox run does today: drained on commit, within the per-minute sweep at worst.
- A resume of 10 legs stays near 40,000 characters of input (R12).

**Constraints**:
- At most one run per session (inherited).
- One outstanding follow-up per leg.
- No new agent tool (spec Assumption).
- Delegation stays one level deep.
- Deploy-safe migrations (R18).
- GitLab and GitHub parity: merge state comes from `MergeRequestOwnership`, which both platforms' webhooks maintain.

**Scale/Scope**: At most 10 targets per call. Today's volume is a few delegated batches a day per active coordinator.
The new per-leg checks are a handful of indexed queries per follow-up.

**Unknowns**: none open. The reply limit is set at 4,000 (R12), and the eval baseline's dependency on PR #1780 is a
sequencing step (R17), not an unknown.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design.*

| Principle | How the plan complies | Evidence to produce |
|---|---|---|
| I. Secure and isolated by default | No new entry point: follow-ups come only from the coordinator's agent tool, which already runs as the triggering user. A follow-up acts as that user (FR-005), its repository access is checked when sent, with the new legs', and the inbox's existing check of every run's repository access gates it again at start (FR-005a, R6). Refusals and failure labels are fixed texts, never exception text. No credential enters the event payload. | Tests: an access-denied follow-up never enqueues, and its run and summary carry only the label |
| II. Enforced architecture boundaries | All new code is in `sessions` (`legs.py`, `inbox/leg_follow_ups.py`) and in `automation.agent`'s delegation data and middleware. No contract changes and no `ignore_imports`. The tool stays in its middleware. There is no second delivery path: follow-ups go through the inbox drain, a split rule and a promoted run. `Leg` and `LegFollowUps` are one-concern classes (`patterns/code-organization.md`). | `make lint-imports`; `test_import_hygiene.py` unchanged |
| III. Platform and provider neutrality | Merge and close state comes from `MergeRequestOwnership`, which the GitLab and GitHub callbacks both maintain. Nothing depends on a provider or model. The reply limit is in characters. | Tests: a merge request closed through `Ownerships.aclose` (merged and not merged), the call both platforms' merge and close callbacks make, stops a follow-up |
| IV. Tests prove project behaviour | Each slice ships unit tests mirroring its modules (`test_legs.py`, `inbox/test_leg_follow_ups.py`, and updates to drain, complete, policy, queue, delegation, header, transcript, delegate_jobs and tool renderers), with no real LLM or network. | `make test` output |
| V. Measured agent behaviour | The tool description, system prompt and resume input change. The `delegation` suite is measured before (on `main`) and after, plus two new cases for SC-001 and SC-002 (R17). | `evals/compare_runs.py` output in the pull request |
| VI. Honest failure handling | Every follow-up outcome is reported as what happened: succeeded, failed (the run failed, or access was denied), not run (merge request merged or closed, classified `all-clear` with its label so nobody is notified of a failure, R16), or cancelled. Nothing is retried in a way that would hide the first failure (R7), and the failure labels are true (`run_failed`, not `retries_exhausted`). The cases below are logged at ERROR. | Tests for each outcome path; the logged cases below |

The cases logged at ERROR:
- a follow-up whose run could not be enqueued (as for every inbox run);
- an unexpected failure while posting a follow-up, logged with `logger.exception`. The target is reported to the
  coordinator with today's fixed "DAIV could not start this leg" text.

**Gates**:
- `make lint`, `make lint-typing` and `make test`.
- A docs build, because `docs/features/orchestration.md` changes.
- Evals, per Principle V.
- A `CHANGELOG.md` `Unreleased` entry under **Added**, describing leg follow-ups and the longer replies. It also fixes
  the stale "top bar" line in the existing Orchestration entry. No Upgrade note: there are no operator actions and no
  breaking change.
- The glossary companion change (FR-022), classified MINOR.

**Result**: PASS, with one exception to `patterns/errors.md` rule 4 recorded under Complexity Tracking for the approving maintainer.

**Post-design re-check (after Phase 1)**: PASS.
- The data model adds no table, column or endpoint.
- The contracts change only the agent-facing JSON, by adding fields, and the session page's `delegations` payload.
- The glossary additions are listed in the [data model](data-model.md#glossary-additions).

## Project Structure

### Documentation (this feature)

```text
specs/002-delegated-leg-follow-ups/
├── spec.md
├── plan.md                      # this file
├── research.md                  # Phase 0: decisions R1–R18, spec refinements
├── data-model.md                # Phase 1: event kind, failures, run, Leg, dataclasses, glossary
├── contracts/
│   ├── delegate-jobs-tool.md    # tool schema, result, refusals, status, resume summary, system prompt
│   ├── sessions-services.md     # Leg, LegFollowUps, inbox/queue/services/delegation changes
│   └── session-page.md          # delegations payload, call card, labels, leg page, docs
├── quickstart.md                # validation scenarios
└── checklists/requirements.md
```

### Source Code (repository root)

```text
daiv/
├── sessions/
│   ├── legs.py                     # NEW: Leg (base ref, own branch, merge request and state, busy, lost, archived)
│   ├── models.py                   # EventKind.LEG_FOLLOW_UP; EventFailure.{RUN_FAILED, MR_MERGED, MR_CLOSED}; LINK_FAILED_PREFIX moves here
│   ├── migrations/                 # choices-only AlterField × 2
│   ├── inbox/
│   │   ├── leg_follow_ups.py       # NEW: LegFollowUps.post / stop_reason / stop / end_with_run
│   │   ├── __init__.py             # docstring: the leg follow-up rules
│   │   ├── kinds.py                # payload, precedence, source key, trigger type, follows_session
│   │   ├── splits.py               # runs alone, but takes context
│   │   ├── drain.py                # promote its own run; stop at take; _fail finishes its run
│   │   ├── render.py               # request = payload prompt
│   │   ├── complete.py             # never retried; pending event ends with its run
│   │   ├── policy.py               # DAIV-started expression
│   │   └── post.py                 # Inbox.post refuses the kind
│   ├── queue.py                    # fork_branch from Leg.has_own_branch
│   ├── services.py                 # asubmit_batch_runs(batch_id=...)
│   ├── delegation.py               # targets split, refusals, batch assembly, replies, summary, states, payload
│   ├── header.py                   # total = distinct leg sessions
│   ├── transcript.py               # not-run marker
│   ├── transcript_labels.py        # leg_not_run_*, leg_follow_up
│   └── inbox_display.py            # bubble text for the kind
├── automation/agent/
│   ├── delegation.py               # DelegatedTarget.thread_id; StartedLeg.leg_follow_up; FailedLeg.thread_id; LegStatus.leg_follow_up
│   └── middlewares/delegate_jobs.py  # schema, descriptions, system prompt, duplicate check
├── chat/static/chat/js/
│   ├── tool-renderers.js           # match by thread_id; leg follow-up label; not-run states
│   ├── chat-stream.js              # the leg follow-up bubble's label
│   └── transcript-labels.js        # label fallbacks
└── notifications/tasks.py          # a not-run leg follow-up is classified all-clear

tests/unit_tests/
├── sessions/test_legs.py                         # NEW
├── sessions/inbox/test_leg_follow_ups.py         # NEW
├── sessions/inbox/test_splits.py                 # NEW: splits.py has no test file yet
├── sessions/inbox/test_{kinds,drain,render,complete,policy,post}.py
├── sessions/test_{delegation,queue,services,header,transcript,transcript_labels,inbox_display,migrations}.py
├── automation/agent/middlewares/test_delegate_jobs.py
├── chat/test_{tool_renderers,chat_stream}_js.py
└── notifications/test_{tasks,run_notifiers}.py
tests/integration_tests/test_delegation.py        # + two cases (SC-001, SC-002), RecordingDelegator records thread_id
docs/features/orchestration.md, docs/features/sessions.md
constitutions/glossary/README.md
CHANGELOG.md
```

**Structure Decision**: This extends the existing Django apps, and nothing new sits above `sessions`.
- Leg facts get a module of their own (`sessions/legs.py`), because the queue, the drain and the delegator all read
  them, and `sessions.delegation` cannot be imported by the first two (R1).
- The inbox mechanics of the new kind sit beside the other inbox modules.
- The agent sees only new fields and text.

## Delivery Slices

Each slice can ship and be demonstrated on its own, in this order.

| # | Slice | Spec | Depends on |
|---|---|---|---|
| 0 | Prerequisite: the `delegation` eval suite merged to `main` and its baseline recorded (`eval-runs/before.jsonl`) | SC-003, Constitution V | — |
| 1 | Full leg replies: the reply from `response_text`, the 4,000 and 1,000 limits, the cut marker, the session id in the summary and status | US4; FR-015–FR-017; SC-007 | — |
| 2 | Foundation: `Leg`, the event kind, payload and migrations, `LegFollowUps.post` with its own run, the drain's split and promotion, settling and no retry, the policy expression, the fork rule, `asubmit_batch_runs(batch_id)` | FR-002–FR-005a, FR-011, FR-011a, FR-012, FR-013 | — |
| 3 | Sending and refusing: `DelegatedTarget.thread_id`, refusal order and texts, the duplicate check, batch assembly (R11), `StartedLeg`/`FailedLeg` fields, the not-run stop at take | US1, US2, US3; FR-001, FR-006–FR-010, FR-008a, FR-014 | 2 |
| 4 | What the coordinator is told: the tool description, system prompt, check description, the summary's closing guidance and follow-up markers; two new eval cases; the after-run and comparison | US5 scenarios 4–5; FR-018; SC-001–SC-003 | 0, 1, 3 |
| 5 | What people see: the distinct-session count, the leg payload fields, card matching and labels, the not-run marker, the inbox bubble | US5 scenarios 1–3; FR-019, FR-020 | 3 |
| — | Cross-cutting: docs, glossary, CHANGELOG | FR-021, FR-022 | each slice |

Slice 1 stands alone: it is the reply-length half of the feature, with its own eval signal (case 5). Slices 2 and 3
together are the MVP for follow-ups.

## Complexity Tracking

| Deviation | Why needed | Simpler alternative rejected because |
|---|---|---|
| **Exception to `patterns/errors.md` rule 4** (run status reflects what happened). A not-run leg follow-up's run is stored `FAILED`, with the label "Not run: its merge request was merged" (or "closed"). **Owner**: the maintainer who approves this plan. **Expiry**: the release after this feature ships, by when a neutral finished status is added or this exception is confirmed. **Residual gap**: run lists, status filters, the dashboard's Failed counts and Insights' failure counts show it as failed, and its leg session lists as failed while it is the newest run | `RunStatus` has no "not run". Everything else says "not run": the coordinator's outcome word, the transcript marker, and the notification envelope (`all-clear`, so nobody is notified of a failure; R16). Cancelled and lost legs are stored the same way | A new `RunStatus` touches every reader of terminal statuses (queue, recovery, notifications, filters, badges), for one rare outcome |
| A follow-up never waits for the coordinator's turn (`starts_after_this_turn` is always false) | `waits_for` holds plain QUEUED runs, which the inbox drain does not honour. The only case it would serve is a leg with no branch of its own, on the coordinator's branch, followed up in the same turn the coordinator pushes (refinement 3) | Teaching the drain `waits_for` is a second hold mechanism for one rare case. The coordinator can follow up after its push lands, in the next turn |
| `LINK_FAILED_PREFIX` moves from `sessions/queue.py` to `sessions/models.py` | `sessions.queue` imports `Leg`, which needs the prefix to tell a lost leg | A deferred import in `legs.py` would hide a cycle that the move removes |
