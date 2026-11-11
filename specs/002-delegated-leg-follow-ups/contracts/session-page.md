# Contract: what people see

These are changes to the coordinator's and the leg's session pages. No endpoint is added: the `delegations` key
already reaches the page through `page_state` and the turns poll (`_HEADER_KEYS`).

## The `delegations` payload (`sessions.header.delegations_for`)

```text
{
  "total": int,              # CHANGED: distinct leg sessions across every batch (was: legs summed per batch)
  "batches": [               # unchanged: newest first
    {"goal": str, "created_at": iso, "legs": [LegPayload, ...]}
  ]
}
```

A leg session that served two batches appears in both batches' `legs` and counts once in `total` (FR-019, US5
scenario 2).

## `LegPayload` (`sessions.delegation._leg_payload`)

```text
{
  "repo_id": str,
  "ref": str,
  "state": str,              # adds "not_run_merged", "not_run_closed"
  "merge_request_url": str,  # "" unless the viewer may see the session
  "session_url": str,        # "" unless the viewer may see the session
  "thread_id": str,          # NEW: "" unless the viewer may see the session
  "leg_follow_up": bool      # NEW: this entry is a leg follow-up's run
}
```

## The `delegate_jobs` call card (`chat/static/chat/js/tool-renderers.js`)

Server-side matching is unchanged: `transcript.mark_delegations` attaches a batch's legs to the call by `batch_id`.

Client-side matching, in `mergedLegs` and `delegatedTable`:

1. A target or result entry with a `thread_id` matches the leg with that `thread_id` first.
2. Otherwise it falls back to today's matching: repository and branch, then repository alone.
3. A failed entry with a `thread_id` (a refusal) matches no server leg, because a refused follow-up created no run.
   It is added as its own row with its `error`, as today's failed entries are.

A row for a leg follow-up carries the "leg follow-up" label next to its repository (FR-020).

## Labels (`sessions/transcript_labels.py`, mirrored in `chat/static/chat/js/transcript-labels.js`)

| Key | Text |
|---|---|
| `leg_not_run_merged` | "Not run: merge request merged" |
| `leg_not_run_closed` | "Not run: merge request closed" |
| `leg_follow_up` | "Leg follow-up" |

The two new states need no CSS. A state without a `leg-dot--<state>` rule gets the base `.leg-dot`, the neutral
faint dot a cancelled leg also gets (`static_src/css/input.css`). No new design token.

## The leg's own session page

- **Inbox panel**: a `leg_follow_up` event is listed under "Leg follow-up", with its prompt as text
  (`inbox_display.bubble`) and as its one-line summary (`EventText.summary`). The waiting state is shown as for other
  waiting requests. The chat bubble is labelled "Leg follow-up" (`bubbleKindLabel` in `chat-stream.js`, with the
  fallback `inbox_leg_follow_up` in `transcript-labels.js`; the server builds that label from the event kind).
- **Transcript**: the follow-up's run shows its prompt as the user turn that opens the run, like the leg's first run.
  While it waits, that turn is shown as pending (`_prompt_turns(run, pending=True)`, as today for QUEUED runs).
- **Run marker**: a not-run follow-up gets the neutral "aborted" marker with "Not run: its merge request was merged"
  (or "closed"). An access-denied one gets the failed marker with the `access_denied` event-failure label.
- **Cancel**: the existing stop control on a waiting or running run stops a leg follow-up. Its run ends cancelled,
  its event is dismissed, and the batch then resumes the coordinator or not, by today's rules.

## Docs (`docs/features/orchestration.md`)

- **"How it works"** gains a paragraph on leg follow-ups.
- **New section "Following up a leg"**, after "Where a leg's work lands":
  - when to follow up and when to delegate a new leg;
  - that a follow-up works in the leg's session, branch and merge request;
  - that it waits while the leg is busy and runs alone;
  - that it is never retried.
- **"Rules and limits"** gains rows for:
  - one follow-up outstanding per leg;
  - follow-ups counting toward the 10-target limit;
  - the refusals;
  - a follow-up always waking the leg, even at its run limit;
  - replies up to 4,000 characters, and 1,000 in the overview.
- **"Seeing the batch"** says a leg is counted once and listed under each batch it served, and names the two "not run"
  states.

`docs/features/sessions.md` keeps its trigger table, since a follow-up is a Delegated Run. Its inbox section lists
the "Leg follow-up" event.
