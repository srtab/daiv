"""Signals that ``sessions`` sends. No project imports here, so any module can import this at the top.

The receivers live in ``sessions.signals``, ``memory.signals`` and ``notifications.signals``.
"""

from django.dispatch import Signal

# Emitted by ``RunManager.finish``/``afinish`` when a non-chat Run reaches a terminal status. Arguments: run (Run),
# skip_dispatch (bool, the sender releases the session's next run), never_started (bool, it failed before it ran).
run_finished = Signal()

# Emitted exactly once when a RunEnvelope is persisted for a Run.
# Arguments: run (Run instance), envelope (RunEnvelope instance).
run_classified = Signal()

# Emitted when the CI watch runs out of fix attempts with CI still failing; at most once per armed watch.
# Arguments: session (Session), failing_jobs (list[str]), pipeline_url (str), pipeline_id (int).
watch_exhausted = Signal()
