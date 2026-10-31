"""A session's inbox: everything that asks an existing session to act, and how it reaches the agent.

A CI result, a mention on a merge request the session owns, a chat message sent while it is busy, a follow-up job,
a delegated batch finishing: each becomes an ``InboxEvent`` on that session, posted through
:func:`sessions.inbox.post.apost_event`, the only way in. The rules:

- **An event wakes the agent or is only remembered.** ``wake`` is fixed when the event is posted. A remembered event
  starts no run; the next run on its branch takes it as context. Only CI and delegation events can be turned into
  remembered ones at take time, by mute or the consecutive-run limit (:mod:`sessions.inbox.policy`), so a person's
  request is never dropped.
- **One run per session, claimed when the run is created.** :func:`sessions.inbox.drain.adrain` claims
  ``Session.active_run_id`` with the run's pk in the same transaction that makes the run READY, and ``run_job_task``
  runs it under the ``Claimed`` lock policy: it heartbeats and releases, and never polls. No worker waits for a busy
  session; the drain is retried when the holder's run ends, and by a per-minute sweep.
- **One QUEUED next run per busy session and branch.** A waking event that lands on a busy session joins the
  session's QUEUED run for the event's branch, or creates it, so every source gets a ``Run`` id to show or return
  while it waits. The drain promotes it to READY when the slot frees.
- **A run takes the pending events for its branch.** The branch is that of the oldest waking event. Every pending
  event whose ``ref`` is blank or equals it is taken; events for another branch wait for a run on theirs. Of the
  pending events sharing a ``subject`` only the newest is taken; the older ones become ``superseded``.
- **Remembered events reach the model as a hidden synthetic message.** :func:`sessions.inbox.render.arender_input`
  turns the taken events into a ``synthetic_message(kind="inbox")`` the transcript hides, followed by one
  ``HumanMessage`` with people's words, or a DAIV-written prompt when only CI or a delegation woke the run.
- **The run's end settles its events** (:mod:`sessions.inbox.complete`): handled when it succeeds or asks a question,
  dismissed when its owner stopped it, retried up to three times when it fails, then failed. Then the next drain runs.

A chat message sent while the session is busy runs as a task-backed ``chat`` run the drain starts. The chat-specific
paths tell such a run from an inline turn by whether it has a ``task_result``, not by its trigger type:
``sync_stuck_runs`` re-syncs it from its task instead of reaping it as an orphaned turn, ``finalize_chat_run`` is never
called for it, and the chat cancel endpoint stops it through ``RunManager.cancel``. An inline turn takes the session's
pending remembered events when it claims the slot, as context; waking events wait for the drain.
"""
