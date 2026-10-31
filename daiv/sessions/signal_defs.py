"""Signals that ``sessions`` sends. No project imports here, so any module can import this at the top.

The receivers live in ``sessions.signals``, ``memory.signals`` and ``notifications.signals``.
"""

from django.dispatch import Signal

# Emitted by ``RunManager.finish``/``afinish`` when a non-chat Run reaches a terminal status. Arguments: run (Run),
# skip_dispatch (bool, the sender releases the next run itself), never_started (bool, failed before it started).
run_finished = Signal()

# Emitted by ``RunManager.finish``/``afinish`` for every Run that reaches a terminal status, chat included, before
# ``run_finished``. Arguments: run (Run).
run_ended = Signal()

# Emitted when CI fails on a commit DAIV pushed and the merge request's CI attempts are spent; once per commit.
# Arguments: ownership (MergeRequestOwnership), session (Session, the owning one), failing_jobs (list[str]),
# pipeline_url (str), sha (str).
watch_exhausted = Signal()
