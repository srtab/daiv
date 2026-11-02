# Contract: HTTP surface changes

No new routers. Every change is on an existing endpoint, keeps its authentication, and keeps its repository access checks (`patterns/security.md` rule 6).

## Chat: `POST` on the AG-UI run endpoint (`daiv/chat/api/views.py:127`, under `/api/chat`)

**Today**: 409 "already in progress" when the slot is held, or "waiting to start" when non-chat runs are unfinished.

**New**:

| Session state | Response |
|---|---|
| Slot free and no next run queued | Unchanged inline stream. Pending events are taken right after the claim (`atake_for_chat`) and reach the model as context |
| Slot held, or a next run is queued | The message is posted as a `chat_message` event (key `chat:<agui message id>`). The response is a well-formed AG-UI stream with a `RUN_STARTED`, one `CUSTOM` event and a `RUN_FINISHED` with no assistant content |

The `CUSTOM` event is:

```json
{"type": "CUSTOM", "name": "daiv.queued", "value": {"event_id": "<uuid>", "run_id": "<next run uuid>", "position": "next"}}
```

- The page renders the user's message as queued, then follows the next run through the existing `GET /api/sessions/{id}/turns` poll and `GET /api/chat/stream` relay when it starts.
- The 409 remains only for an unauthorised caller and the existing validation errors.

## Session turns poll: `GET /api/sessions/{thread_id}/turns`

New fields in the response:

```json
{
  "turns": [{"id": "inbox-<uuid>", "role": "inbox_event", "kind": "ci_passed", "summary": "Pipeline 830731 passed", "reason": "", "reason_text": "", "failure_label": "", "web_url": "…", "created_at": "…"}],
  "up_next": {
    "groups": [{"run_id": "<uuid>|", "who": "…", "is_viewer": true, "starting": false, "bubbles": [{"id": "inbox-<uuid>", "event_id": "<uuid>", "kind": "chat_message", "received": false, "failed": false, "text": "…", "source_iid": 9, "trigger_type": "", "web_url": "", "created_at": "…"}], "told": ["<inbox_event entry>"]}],
    "told": [{"branch": "chore/x", "lines": ["<inbox_event entry>"]}]
  },
  "ci": [{"merge_request_iid": 9, "status": "failing|passing|running|stopped", "label": "Failing|Stopped after 3 attempts|…", "tone": "…", "failed_jobs": ["tests"], "attempts": 1, "max_attempts": 3, "watch_enabled": true, "explanation_run_id": "<uuid>|null"}]
}
```

- **`inbox_event` entries in `turns`**: events runs already took, and failed ones, interleaved with the transcript before the first turn of the run that took them (`interleave_inbox_events`). A failed event carries its `failure_label`; a quieted one its `reason` and `reason_text`.
- **`up_next`** (also served alone by `GET /api/sessions/{thread_id}/up-next`): the runs still to start, in the order the drain will start them, one group per branch and requester, each with the requests that start it (`bubbles`) and the remembered events it will be told; plus the remembered events no group will take, per branch. Every viewer of the session sees it (FR-034).
- **`ci`**: replaces the single `watch` payload. It lists every open merge request the session owns (FR-003), its status taken from the latest CI event of each subject on the newest commit (one subject per merge request on GitLab, one per workflow on GitHub). After a give-up the `label` reads "Stopped after N attempts" and `failed_jobs` names the failing jobs (FR-017). `watch` stays one release for old pages, derived from the first item.
- Summaries are built from payload fields and never contain exception text.

## CI watch toggle: `POST /api/sessions/{thread_id}/ci-watch`

- Body gains an optional `merge_request_iid`, which defaults to the session's only owned open merge request. It toggles `MergeRequestOwnership.watch_enabled`.
- 409 reasons are unchanged (`changed`, `disabled`), plus `not_owned` when the session doesn't own that merge request.

## Stop a background run: `POST /api/chat/cancel` (existing)

- Accepts a session's background run when the caller owns the session.
- A task-backed run is stopped through `RunManager.cancel`, which sets `cancel_requested_at`; the `run_job_task` `should_stop` check ends it.
- `LiveRun.stoppable` becomes true for background runs shown to the session's owner (FR-030).

## Jobs API `POST /api/jobs` and MCP `submit_job`, with `thread_id`

| Session state | Behaviour |
|---|---|
| Idle | Posts a `follow_up_job` event; the drain dispatches at once. `job_id` is the new run's id. Unchanged for callers |
| Busy | Posts the event into the next run for the job's branch. **`job_id` is that next run's id, which another follow-up job or event on the same branch may share** (R3). `get_job_status` reports that run |

- The job's `ref` becomes the event's `ref`. A ref different from the session's branch waits for a run on that branch (R4).
- **Documentation change**: the MCP server instructions and the jobs API docs say that a job sent to a busy session joins its next run.
