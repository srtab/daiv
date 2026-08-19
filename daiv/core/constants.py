BOT_NAME = "DAIV"
BOT_LABEL = "daiv"
BOT_MAX_LABEL = "daiv-max"
BOT_AUTO_LABEL = "daiv-auto"

# User-facing terminal messages for chat runs. Written by the chat streamer (as the
# RUN_ERROR event message and persisted to Run.error_message), and rendered verbatim in
# the session transcript on reload, so they must never carry raw exception text. The
# sessions transcript annotator reads Run.error_message back and treats the two neutral
# terminations — CANCELLED_BY_USER_MESSAGE and INTERRUPTED_MESSAGE — as the "aborted"
# marker, and anything else on a FAILED run as a genuine "failed" marker.
CANCELLED_BY_USER_MESSAGE = "Stopped by user."
INTERRUPTED_MESSAGE = "Run was interrupted before completing."
RUN_FAILED_MESSAGE = "Run failed. Check server logs for details."

# Task queues. Short user-visible work runs on ``interactive`` so it never waits behind an agent
# run: a worker runs one task to completion before claiming the next, and priority cannot preempt.
TASK_QUEUE_DEFAULT = "default"
TASK_QUEUE_INTERACTIVE = "interactive"

# Priorities inside a queue (higher runs first). Only the interactive queue orders its members:
# every default-queue task is long, so ranking them would starve whichever lost.
TASK_PRIORITY_TITLING = 20
TASK_PRIORITY_NOTIFICATION = 10
