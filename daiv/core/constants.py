from enum import StrEnum

BOT_NAME = "DAIV"
BOT_LABEL = "daiv"
BOT_MAX_LABEL = "daiv-max"
BOT_AUTO_LABEL = "daiv-auto"

# A cross-project write carries a person's attribution, so the webhook's "is this my own event?"
# check cannot recognise it. Renders as nothing on both platforms.
CROSS_PROJECT_CONTENT_MARKER = "<!-- daiv:cross-project -->"


class CrossProjectOutcome(StrEnum):
    """How one cross-project attempt ended. ``codebase.models`` derives its choices from this so
    the middleware can name an outcome without importing models at app-load time."""

    ALLOWED = "allowed"
    DENIED_NO_ACCESS = "denied_no_access"
    DENIED_NO_CREDENTIAL = "denied_no_credential"
    DENIED_DISABLED = "denied_disabled"
    DENIED_POLICY = "denied_policy"
    ERROR = "error"


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
