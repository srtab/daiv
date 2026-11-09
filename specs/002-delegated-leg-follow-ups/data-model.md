# Data Model: Delegated Leg Follow-ups

This feature needs no new tables and no new columns. It adds one inbox event kind and three event-failure labels,
both as choices-only migrations (research R18). It also adds a derived view of a leg (`Leg`) and new fields on the
agent-facing dataclasses.

## InboxEvent: the `leg_follow_up` kind

| Field | Value for a leg follow-up |
|---|---|
| `kind` | `leg_follow_up`, labelled "Leg follow-up" (new `EventKind` choice) |
| `session` | the leg's session |
| `wake` | `true`, always; never turned into a remembered event by the run limit (FR-004a) |
| `ref` | the leg session's `ref` when posted; it follows the session (`follows_session()`) |
| `source_key` | `leg-follow-up:<batch_id>`, unique per session, so one follow-up per leg per batch |
| `subject` | blank; never superseded |
| `author` | the coordinator run's `user`, the person it acts as (FR-005) |
| `run` | its own run (below), set when posted, never shared |
| `payload` | `LegFollowUpPayload` |

```text
LegFollowUpPayload
  prompt: str              # the coordinator's request, the leg's HumanMessage
  batch_id: str            # the batch the delegate call created
  references?: list[dict]  # merged into the session when the run takes it, as a follow-up job's are
  muted?: bool             # the coordinator's effective mute
```

**Validation**: `EventContent.validate_payload` requires `prompt` and `batch_id`. `Inbox.post` is not used for this
kind; `LegFollowUps.post` creates the event and its run together.

### EventFailure: new values

| Value | Label (the only text shown) | When |
|---|---|---|
| `run_failed` | "The run that took it failed" | its run ended FAILED and was not cancelled (R7) |
| `mr_merged` | "Its merge request was merged" | stopped at start (FR-008a, R5) |
| `mr_closed` | "Its merge request was closed" | stopped at start (FR-008a, R5) |

`access_denied` already exists and is reused (FR-005a, R6).

### State transitions of the event

```text
pending ──take──▶ taken ──run SUCCESSFUL─────────────▶ handled
   │                 ├──run FAILED (cancelled)────────▶ dismissed
   │                 └──run FAILED────────────────────▶ failed (run_failed)
   ├──take: merge request merged/closed──────────────▶ failed (mr_merged | mr_closed)
   ├──take: requester may not run on the repository──▶ failed (access_denied)
   └──its run ended before the take
         ├── cancelled ──────────────────────────────▶ dismissed
         └── otherwise ──────────────────────────────▶ failed (run_failed)
```

An event never goes back from `taken` to `pending`: it is never retried (FR-011a).

## Run: a leg follow-up's run

A leg follow-up adds no new `Run` fields. Its run is an ordinary `DELEGATED_JOB` run in the leg's session.

| Field | Value |
|---|---|
| `session` | the leg's session |
| `trigger_type` | `delegated_job` |
| `batch_id` | the batch of the delegate call that sent it |
| `status` | `QUEUED` from the moment it is sent, until the drain promotes it to `READY` |
| `user` | the coordinator run's user |
| `muted` | the coordinator's effective mute |
| `prompt` | the follow-up's request, kept current by the take (`RunInput.request()`) |
| `ref` | the leg session's branch, reset to the take's branch on promotion |
| `repo_id` | the leg's repository |
| `waits_for` | never set (research refinement 3) |
| `task_result` | set when the drain enqueues it |

A run is a leg follow-up's when a `leg_follow_up` event is linked to it (`InboxEvent.run`). `Leg`, `WakePolicy` and
`_resumes` all read it this way.

### Outcome words (`leg_state` → the coordinator's word)

| State | Derived from | Word |
|---|---|---|
| `queued` | `QUEUED` | queued |
| `running` | `RUNNING` | running |
| `stopping` | cancel requested, not terminal | stopping |
| `finished` | `SUCCESSFUL` | succeeded |
| `failed` | `FAILED` | failed |
| `cancelled` | `FAILED` with `cancel_requested_at` | cancelled |
| `lost` | `FAILED` with `LINK_FAILED_PREFIX` | started, but DAIV lost track of it |
| `not_run_merged` (new) | `FAILED` with the `NOT_RUN_MERGED` label | not run: its merge request was merged |
| `not_run_closed` (new) | `FAILED` with the `NOT_RUN_CLOSED` label | not run: its merge request was closed |

`not_run_*` counts as not succeeded in "N of M succeeded", and it is not cancelled, so the batch still resumes.

## Leg (derived, `sessions/legs.py`)

`Leg` is a session whose `parent` is the coordinator, read through its delegated runs. It is not a model.

| Property | Definition |
|---|---|
| `session` | the leg's `Session` |
| `runs` | its `DELEGATED_JOB` runs, oldest first |
| `base_ref` | the `ref` of its first delegated run: the branch it started from, after a waiting leg's rebase |
| `has_own_branch()` | `session.ref != base_ref` |
| `merge_request()` | the newest merge request its runs published on its main repository: `(repo_id, iid, web_url)`, or none |
| `merge_request_state()` | `open`, `merged` or `closed`, from `MergeRequestOwnership` (`merged`, `closed_at`); no row reads as `open` |
| `is_busy()` | some delegated run is not terminal: `QUEUED`, including a follow-up's run or a leg waiting for the coordinator, `READY` or `RUNNING` |
| `is_lost()` | its latest delegated run is FAILED with `LINK_FAILED_PREFIX` |
| `archived_by(user)` | a `HiddenSession` row exists for `(session, user)` |
| `belongs_to(coordinator)` | `session.parent_id == coordinator` and it has at least one delegated run |

## Batch (unchanged shape, wider membership)

The runs that share a `batch_id` are new legs' first runs and leg follow-ups' runs. A batch is finished when all of
its runs are terminal. It resumes its coordinator when it is finished and:

- some run reached the task queue, waited for the coordinator, or is a leg follow-up's run (R11); and
- not every run was cancelled.

## Agent-facing dataclasses (`automation/agent/delegation.py`)

| Dataclass | Change |
|---|---|
| `DelegatedTarget` | `+ thread_id: str = ""`: a leg's session id; when set, the target is a leg follow-up |
| `StartedLeg` | `+ follow_up: bool = False`; for a follow-up, `ref` is the branch it works on and `starts_after_this_turn` is false |
| `FailedLeg` | `+ thread_id: str = ""`, set on a follow-up's refusal |
| `LegStatus` | `+ follow_up: bool = False`; `ref` is where the leg started, `branch` its own branch whenever it has one, `merge_request_url` the leg's; `reply` is cut at the caller's limit with the cut marker |

Constants in `sessions/delegation.py`: `LEG_REPLY_MAX_CHARS = 4000` (was 1000) and
`LEG_OVERVIEW_REPLY_MAX_CHARS = 1000` (new).

## Glossary additions

These go into `constitutions/glossary/README.md` (FR-022). It is a companion-document change in the same pull
request, classified MINOR: terms are added and nothing is redefined.

| Term | Meaning | Not |
|---|---|---|
| Coordinator | The session whose run delegated work with `delegate_jobs`, and which is resumed when its batch finishes | parent job, orchestrator |
| Leg | One piece of delegated work: a session whose `parent` is the coordinator, publishing to a branch and merge request of its own | sub-job, child |
| Batch | The new legs and leg follow-ups one `delegate_jobs` call sent, sharing a `batch_id`; the coordinator is resumed once when all have ended | — |
| Leg follow-up | A coordinator's request to continue a leg it delegated earlier, in the leg's session and on its branch (`leg_follow_up` inbox event) | follow-up job, follow-up |

The "Inbox event" row lists "a leg follow-up" among its examples.
