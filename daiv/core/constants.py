BOT_NAME = "DAIV"
BOT_LABEL = "daiv"
BOT_MAX_LABEL = "daiv-max"
BOT_AUTO_LABEL = "daiv-auto"

# Appended to anything DAIV publishes in a project other than the run's attached one. Those writes
# carry a person's attribution rather than the bot's, so the webhook's "is this my own event?"
# check (which compares against the bot user) cannot recognise them — and a DAIV-watched target
# project would otherwise feed DAIV's own comment straight back as a new run. Renders as nothing
# on both platforms.
CROSS_PROJECT_CONTENT_MARKER = "<!-- daiv:cross-project -->"

# User-facing terminal messages for chat runs. Written by the chat streamer (as the
# RUN_ERROR event message and persisted to Run.error_message), and rendered verbatim in
# the session transcript on reload, so they must never carry raw exception text. The
# sessions transcript annotator reads Run.error_message back and treats the two neutral
# terminations — CANCELLED_BY_USER_MESSAGE and INTERRUPTED_MESSAGE — as the "aborted"
# marker, and anything else on a FAILED run as a genuine "failed" marker.
CANCELLED_BY_USER_MESSAGE = "Stopped by user."
INTERRUPTED_MESSAGE = "Run was interrupted before completing."
RUN_FAILED_MESSAGE = "Run failed. Check server logs for details."

# A worker runs one task to completion before claiming the next, so short user-visible work
# needs its own queue — priority alone cannot get it past an agent run already running.
TASK_QUEUE_DEFAULT = "default"
TASK_QUEUE_INTERACTIVE = "interactive"

# Ordering within the interactive queue. Default-queue tasks are all long, so ranking them
# would only starve whichever lost.
TASK_PRIORITY_TITLING = 20
TASK_PRIORITY_NOTIFICATION = 10
