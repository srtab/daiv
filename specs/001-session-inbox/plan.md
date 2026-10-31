# Implementation Plan: Session Inbox

**Branch**: `001-session-inbox` | **Date**: 2026-10-30 | **Spec**: [spec.md](spec.md) | **Status**: Approved by a maintainer on 2026-10-30, including the spec refinements in [research.md](research.md#spec-refinements-accepted-2026-10-30)

**Input**: Feature specification from `specs/001-session-inbox/spec.md`

## Summary

Each session gets an inbox. Everything that asks an existing session to act becomes an `InboxEvent` on that session: a CI result, a mention on a merge request it owns, a queued chat message, a follow-up job, or a finished delegated batch. Each event either wakes the agent or is only remembered.

A drainer starts one run at a time per session. It claims the session slot when it creates the run, so no worker waits for a busy session. Each run takes every pending event for its branch:
- remembered events and DAIV notices reach the model as a hidden synthetic context message;
- people's requests reach it as the run's `HumanMessage`.

The session that publishes a merge request owns it. Ownership records the SHA DAIV pushed, so whether a CI failure wakes the agent is decided from facts: the head commit DAIV pushed, not yet examined, budget left, watch on, not muted. The CI watch stops dispatching fix runs into the merge request's own thread, and stops giving up when a fix run pushes nothing.

The decisions behind each part are in [research.md](research.md) (R1–R20).

## Technical Context

**Language/Version**: Python 3.14

**Primary Dependencies**:
- Django 6 under ASGI, and django-ninja;
- `django-tasks-db` with the deduplicating backend, and `django-crontask`;
- Deep Agents on LangGraph;
- python-gitlab and PyGithub through `codebase.clients`.

**Storage**: PostgreSQL holds two new tables (`InboxEvent`, `MergeRequestOwnership`) and new columns on `Session` and `Run`. Redis is unchanged: checkpoints and live run streams.

**Testing**:
- pytest, pytest-django and pytest-asyncio (`asyncio_mode = "auto"`);
- async database tests use `django_db(transaction=True)`;
- prompt evals run with `make eval-prompts` under a new `inbox` marker.

**Target Platform**: Linux containers. dip-daiv runs a single-node Docker Swarm (2 app, 4 worker replicas); daivagent runs Docker Compose (1 app, 2 workers).

**Project Type**: Django web service, with background workers running the agent.

**Performance Goals**:
- A run starts within 1 minute of a waking event reaching an idle session (SC-009).
- No worker is held by a run waiting for a busy session (SC-004).

**Constraints**:
- At most one run per session (FR-007).
- Migrations must be deploy-safe: old pods run alongside new ones mid-deploy, so columns are dropped one release after they stop being written.
- GitLab and GitHub parity (Constitution III).
- No agent tool changes in this feature (R10).

**Scale/Scope**:
- Today's volume is tens of events an hour across a few hundred active sessions, and each run takes a handful of events.
- The per-minute drain sweep reads a partial index of pending waking events.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design.*

| Principle | How the plan complies | Evidence to produce |
|---|---|---|
| I. Secure and isolated by default | No new inbound path: the new events arrive through the existing authenticated webhook views (`patterns/security.md` rule 5). Inbox runs check the owner's repository access before running (FR-022, R11), and ownerless sessions keep today's webhook-run privileges. No secret enters event payloads. Failure reasons are labels, never exception text. | Tests: access denial marks events failed, the run never starts, and no exception text reaches the payload |
| II. Enforced architecture boundaries | New code goes in `sessions` (R1); trigger apps call down into it; no new `ignore_imports`. The working-copy fast-forward is a `Workspace` operation (R17). `FixRunDispatcher`, the CI exhaust path and delegation's QUEUED continuation insert are removed, leaving one delivery path. Known remaining parallel paths are listed under Complexity Tracking. | `make lint-imports` passes; `test_import_hygiene.py` unchanged |
| III. Platform and provider neutrality | Every git-platform read and write goes through `RepoClient`. Pushes, merge request close and merge, and diff-comment mentions are handled on both GitLab and GitHub (R8, R10). Wake decisions use pushed SHAs, which work on both (R7). The size limit is in characters, not a provider's tokenizer (R14). | Paired GitLab and GitHub callback tests for each new event |
| IV. Tests prove project behaviour | Each slice ships with unit tests under `tests/unit_tests/<app>/` mirroring the modules, with no real LLM or network. | `make test` output |
| V. Measured agent behaviour | The run input format changes (R5), so a new `inbox` eval marker is measured before and after (R18). | `evals/compare_runs.py` output in the PR, or an explicit "unmeasured" statement |
| VI. Honest failure handling | Events that run out of retries are marked failed, logged with `logger.error` and shown in the session (R12). A CI run that pushes nothing is reported as such, not as "exhausted" (R15). Notifications reflect what happened (R16). | Tests for each outcome path |

**Gates**: `make lint`, `make lint-typing` and `make test` on every change. A docs build, because `docs/` changes. Evals, because of R5. A `CHANGELOG.md` `Unreleased` entry with an **Upgrade note**: GitHub App operators must subscribe to `pull_request_review_comment`; the migrations convert open CI watches.

**Result**: PASS. The deviations below are tracked rather than hidden.

**Post-design re-check (after Phase 1)**: PASS.
- The data model adds only `sessions` tables, and the contracts add no endpoint outside the existing routers (`chat`, `sessions`, `jobs`, `mcp_api`).
- Glossary additions are in the [data model](data-model.md#glossary-additions). They are a companion-document change in the same pull request, classified PATCH or MINOR per Governance.

## Project Structure

### Documentation (this feature)

```text
specs/001-session-inbox/
├── spec.md
├── plan.md               # this file
├── research.md           # Phase 0: decisions R1–R20
├── data-model.md         # Phase 1: entities, states, migrations
├── contracts/
│   ├── inbox-service.md  # sessions.inbox Python interface
│   ├── webhook-events.md # platform event → inbox event mapping
│   ├── http.md           # chat, jobs/MCP, ci-watch, cancel, turns-poll changes
│   └── agent-input.md    # what the model receives
├── quickstart.md         # validation scenarios
└── checklists/requirements.md
```

### Source Code (repository root)

```text
daiv/
├── sessions/
│   ├── models.py                 # + InboxEvent, MergeRequestOwnership, Session.inbox_muted, Run.pushed_sha
│   ├── migrations/               # schema + data migration of open watches (R15)
│   ├── inbox/                    # NEW package
│   │   ├── __init__.py           # module docstring: the inbox's rules (detailed behaviour lives in docstrings)
│   │   ├── kinds.py              # event kinds, source keys, subjects, precedence (R6)
│   │   ├── post.py               # apost_event: idempotent insert + drain on commit
│   │   ├── drain.py              # adrain: supersede, choose branch, claim slot, create/promote run (R2–R4)
│   │   ├── render.py             # synthetic context + HumanMessage (R5, R14)
│   │   ├── policy.py             # wake rules: mute, consecutive-run limit (FR-031, FR-033)
│   │   ├── complete.py           # run_ended: handled/retry/dismiss/fail, mention replies (R10, R12)
│   │   └── ownership.py          # claim after publish, lookup, foreign publish (R7, FR-001)
│   ├── pipeline_watch/           # judges pipelines, posts ci_* events; dispatch.py removed (R15)
│   ├── executor/
│   │   ├── lock.py               # + Claimed policy (R2)
│   │   ├── run.py                # arm_watch → ownership claim; synthetic context; fast-forward (R17)
│   │   └── tasks.py              # run_job_task runs pre-claimed inbox runs
│   ├── delegation.py             # resume via inbox event (R19)
│   ├── signals.py                # run_ended → complete + drain
│   ├── tasks.py                  # per-minute drain sweep; push classification task (R8, R13)
│   ├── header.py, transcript.py  # CI status from ownership; remembered-event timeline entries
│   ├── api/views.py              # ci-watch on ownership; turns poll: queued events; cancel background runs
│   └── templates/, static/       # timeline entries, queued state, mute toggle, inbox panel for owners and admins (FR-034)
├── automation/agent/
│   ├── publishers.py, events.py  # pushed_sha in PublishOutcome / publish result (R7)
│   └── workspace/                # fast-forward operation on the Workspace protocol (R17)
├── webhooks/
│   ├── dispatch.py               # route owned-MR mentions to the inbox (R10)
│   ├── gitlab/callbacks.py       # DiffNote on owned MRs, push to MR branches, MR close/merge
│   ├── github/callbacks.py       # pull_request_review_comment, push to PR branches, PR closed
│   └── managers/issue_addressor.py  # pass acting_user_id (R11)
├── chat/api/                     # busy session → queue chat_message (R9)
├── chat/static/chat/js/          # render queued message, tail when it starts
├── jobs/api/views.py             # continuation → follow_up_job event (R20)
├── mcp_api/server.py             # same for submit_job with thread_id
└── notifications/                # classify PIPELINE_WEBHOOK, owner recipient, consecutive-limit event (R16)

tests/unit_tests/
├── sessions/inbox/test_{post,drain,render,policy,complete,ownership}.py
├── sessions/pipeline_watch/, sessions/executor/, sessions/test_delegation.py   # updated
├── webhooks/gitlab/, webhooks/github/, webhooks/test_dispatch.py               # updated
├── chat/, jobs/, mcp_api/, notifications/                                       # updated
evals/                            # inbox marker cases (R18)
docs/                             # CI watch behaviour, GitHub App events
constitutions/glossary/README.md  # new terms
CHANGELOG.md
```

**Structure Decision**: this extends the existing Django apps. The new behaviour sits in one package, `daiv/sessions/inbox/`, because `sessions` is the lowest layer every caller can reach. The trigger apps only change how they hand work to a session.

## Delivery Slices

Each slice can ship and be demonstrated on its own, in this order. Every slice after the foundation maps to one user story.

| # | Slice | Spec | Depends on |
|---|---|---|---|
| 0 | Foundation: models and migration, `apost_event`, drainer with the `Claimed` slot, completion and retries, per-minute sweep, synthetic context, inbox panel on the session page (DAIV has no Django admin) | FR-005–FR-014, FR-034 | — |
| 1 | CI in the publishing session: `pushed_sha`, ownership claim at publish, CI events and wake rules, the watch moved onto ownership plus the data migration, notifications, the issue addressor's acting user, the header's CI status | US1; FR-001–FR-004, FR-015–FR-020 | 0 |
| 2 | Busy sessions: queued chat, taking pending events on idle chat, a stop control for background runs, mute, consecutive-run limit | US2; FR-029–FR-033 | 0 |
| 3 | Remembered events: push, merge and close callbacks on both platforms, delayed push classification, timeline entries, the working-copy fast-forward, the "branch moved" notice | US3; FR-023, FR-024 | 0, 1 |
| 4 | One delivery path: delegation resume, delegated CI outcome to the parent, jobs and MCP continuation, operator view | US4; FR-021, FR-022 | 0, 1 |
| 5 | Mentions on owned merge requests: routing, diff comments on both platforms, reply placement, App subscription docs | US5; FR-025–FR-028 | 0, 1 |
| — | Cross-cutting: `inbox` evals, docs, glossary, CHANGELOG with Upgrade note | Constitution V, gates | each slice |

Slices 0 and 1 together are the MVP. They fix the incident in the spec (merge requests 8 and 9).

## Complexity Tracking

| Deviation | Why needed | Simpler alternative rejected because |
|---|---|---|
| `Session.watch_*` columns stay, unwritten, until the next release | A dropped column breaks pods still running the old code mid-deploy (the same reason `RunEnvelope` keeps its table name) | Dropping now risks failures during deploy |
| Issue-webhook runs keep their own task path and still wait for the slot | The spec's delivery path (FR-021) lists CI, delegated results and follow-up jobs. Moving issue mentions also means moving the issue addressor's reply posting | Including it doubles slice 4's scope. Proposed as a follow-up |
| Mentions on merge requests no session owns keep the merge request thread path | The spec keeps today's behaviour there (edge case "A merge request DAIV did not publish") | Routing them would need an owner the spec doesn't define |
| A task-backed `chat` run (queued chat) | FR-032 needs a chat message accepted while busy, and the browser can't be relied on to hold it | Queuing in the browser loses the message when the tab closes (R9) |
