"""Signals that ``sessions`` sends. No project imports here, so any module can import this at the top.

The receivers live in ``sessions.signals``.
"""

from django.dispatch import Signal

# Emitted when a Run transitions to a terminal status (``RunStatus.terminal()``).
# Arguments: run (Run instance).
run_finished = Signal()

# Emitted exactly once when a RunEnvelope is persisted for a Run.
# Arguments: run (Run instance), envelope (RunEnvelope instance).
run_classified = Signal()

# Emitted when the CI watch runs out of fix attempts with CI still failing; at most once per armed watch.
# Arguments: session (Session), failing_jobs (list[str]), pipeline_url (str), pipeline_id (int).
watch_exhausted = Signal()
