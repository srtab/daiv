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
  "queued": {"run_id": "<uuid>", "events": [{"id": "<uuid>", "kind": "chat_message", "summary": "…", "created_at": "…"}]},
  "timeline": [{"id": "<uuid>", "kind": "ci_passed", "summary": "Pipeline 830731 passed", "web_url": "…", "created_at": "…", "state": "pending"}],
  "ci": [{"merge_request_iid": 9, "status": "failing|passing|running|stopped", "attempts": 1, "max_attempts": 3, "watch_enabled": true, "explanation_run_id": "<uuid>|null"}]
}
```

- **`timeline`**: remembered events, to interleave with transcript turns by `created_at`. The server side is `annotate_transcript`, which emits `inbox_event` pseudo-entries like the existing `run_status` entries.
- **`ci`**: replaces the single `watch` payload. It lists every open merge request the session owns (FR-003). `watch` stays one release for old pages, derived from the first item.
- Summaries are built from payload fields and never contain exception text.

## CI watch toggle: `POST /api/sessions/{thread_id}/ci-watch`

- Body gains an optional `merge_request_iid`, which defaults to the session's only owned open merge request. It toggles `MergeRequestOwnership.watch_enabled`.
- 409 reasons are unchanged (`changed`, `disabled`), plus `not_owned` when the session doesn't own that merge request.

## Inbox mute: `POST /api/sessions/{thread_id}/inbox-mute`

- New action on the existing sessions router. Body `{"muted": bool}`. Owner or admin only. Sets `Session.inbox_muted` (FR-031).
- While muted, mentions still wake the agent, and CI and delegation events are remembered only.

## Stop a background run: `POST /api/chat/cancel` (existing)

- Accepts a session's background run when the caller owns the session.
- A task-backed run is stopped through `RunManager.cancel`, which sets `cancel_requested_at`; the `run_job_task` `should_stop` check ends it.
- `LiveRun.stoppable` becomes true for background runs shown to the session's owner (FR-030).

## Jobs API `POST /api/jobs` and MCP `submit_job`, with `thread_id`

| Session state | Behaviour |
|---|---|
| Idle | Posts a `follow_up_job` event; the drain dispatches at once. `job_id` is the new run's id. Unchanged for callers |
| Busy | Posts the event into the next run. **`job_id` is the next run's id, which another follow-up job or event may share** (R3). `get_job_status` reports that run |

- The job's `ref` becomes the event's `ref`. A ref different from the session's branch waits for a run on that branch (R4).
- **Documentation change**: the MCP server instructions and the jobs API docs say that a job sent to a busy session joins its next run.
