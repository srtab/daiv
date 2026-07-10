# Clarify session creation: launcher + workflow fixes

- **Date:** 2026-07-09
- **Status:** Approved (design), pending implementation plan
- **Author:** Sandro (with Claude)

## Problem

There are two ways to start work in the dashboard, and it is not clear when to
use which:

- **Chat** (`/dashboard/sessions/new/`) — a live, streaming composer. One repo,
  turn-by-turn, you steer as it goes.
- **Agent run** (`/dashboard/runs/new/`) — a form. Prompt + 1–20 repos + model /
  thinking / sandbox / notification, submitted to the background task queue.

Both are "point the agent at a repo with a prompt", both produce rows in the same
sessions list, and they sit behind two separate CTAs on two pages. The confusion
is specifically **lack of guidance on *when* to use each** — not that the two
modes are redundant (they are genuinely different) and not a desire to reduce
code surface.

The backend is already unified: chat, UI run, API, MCP, webhook, and schedule all
create the same `Session` (keyed by `thread_id`, tagged with an `origin`) plus one
or more `Run` rows. This is a front-door problem, not a data-model problem.

### Agreed mental model

- **Chat = "work *with*" the agent** — collaborate live, iterate, steer; one repo.
- **Run = "hand *off*" a task** — well-specified, background, notified when done;
  one or many repos, optionally scheduled.

## Two bugs/behaviours found during design

1. **False "session expired" banner.** `sessions/hydration.py:ahydrate_thread`
   returns `expired=True` whenever there is no LangGraph checkpoint tuple. Per its
   own docstring this covers "a thread that never checkpointed" — i.e. a
   freshly-submitted run that has not started/checkpointed yet. So the detail page
   greets a brand-new run with "This session's state has expired." It conflates
   *pruned / TTL-expired* with *hasn't-started-yet*.

2. **In-flight background runs already render like chat (when not masked by the
   bug).** `session_detail.html` wires `poll_transcript` →
   `sessions/js/session-sync.js`, which polls the `session_turns` endpoint
   (`sessions/api/views.py`) and fills the same transcript UI as chat for
   non-chat in-flight runs. The only thing hiding it is bug #1: when `expired` is
   true the transcript is suppressed and the composer is removed
   (`session_detail.html:240-242`).

## Design

### Part 1 — Single "New" launcher (chooser)

Replace the two competing CTAs ("New session" and "Start a run") with one **New**
entry point that opens a lightweight chooser with two cards. Each card carries the
one-line rule of thumb — the guidance lives at the fork:

- **Chat — work with the agent**: live, iterate turn-by-turn, one repo.
- **Run — hand off a task**: background, notified when done, one or many repos,
  schedulable.

Routing:

- `/dashboard/sessions/new/` becomes the **chooser**.
- The current chat empty-state hero moves to its own route (e.g.
  `/dashboard/sessions/new/chat/`).
- The run form stays at `/dashboard/runs/new/`.

The two destination flows are otherwise untouched. Backend cost ≈ routing + one
template.

### Part 2 — Post-submit redirect → sessions list

After a run is submitted, always redirect to the sessions list scoped to the new
batch: `session_list?batch=<batch_id>` — for single-repo runs too (every submit
already carries a `batch_id`). This reinforces the "hand off" model: fire the
task, see it appear queued/running in the list (the list already renders live SSE
status dots), and walk away. Any row remains clickable to watch it live.

Replaces the current single-run → `session_detail` branch in
`sessions/views.py:AgentRunCreateView.form_valid` (~lines 429-431). The existing
batch → list redirect is unchanged.

### Part 3 — Fix false "expired" + unify the in-flight detail view

Gate the expired banner on there being no in-flight run: render the "expired"
state only when `expired AND not is_in_flight`. Concretely, in
`SessionDetailView.get_context_data`, `ctx["expired"]` becomes
`expired and not ctx["is_in_flight"]` (localized, low-risk).

Result:

- In-flight run with no checkpoint yet → the existing "Waiting in queue" /
  "Agent is working" state (`session_detail.html:117-161`) shows, then the
  existing turn-polling fills in the transcript live — the chat-equivalent view.
  This delivers requirement (b) without new UI.
- Genuine expiry (an old finished session whose checkpoint was pruned; no
  in-flight run) → banner still shows, unchanged.

Caveat to verify during implementation (a check, not new behaviour): the
composer, now visible during an in-flight background run, must stay gated — a new
chat turn cannot start while `active_run_id` is held. The `chat()` Alpine
component already receives `activeRunId` and `statusEndpoint`; confirm it disables
send while a run is active and re-enables when the slot frees.

## Out of scope

- Merging chat and run into a single composer (Approach C). The two modes are
  intentionally kept distinct.
- Token-by-token SSE streaming for background runs. In-flight runs keep polling
  the transcript; only chat streams live. Acceptable for this iteration.
- Any change to the underlying `Session` / `Run` model or the task queue.

## Touch-points (for the implementation plan)

| Change | File(s) |
|---|---|
| Chooser template + route | `sessions/urls.py`, new `sessions/templates/sessions/session_new.html` (chooser), move chat hero to its own template/route |
| Replace two CTAs with "New" | `sessions/templates/sessions/session_list.html` (and any nav/menu CTA) |
| Redirect to list after submit | `sessions/views.py:AgentRunCreateView.form_valid` |
| Gate expired banner | `sessions/views.py:SessionDetailView.get_context_data` |
| Verify composer gating | `chat/_composer.html`, `chat/js/chat-stream.js` (chat component) |
| i18n | new strings → `makemessages` / `compilemessages` (hand-add to avoid cross-app churn) |

## Success criteria

- A user starting fresh sees one "New" action and a chooser that states, in one
  line each, when to use Chat vs Run.
- Submitting a run lands on the sessions list with the new run visible; no
  "expired" banner appears for a just-submitted run.
- Opening an in-flight background run's detail page shows the live transcript in
  the same layout as a chat session.
- A genuinely pruned/expired old session still shows the expired banner.
