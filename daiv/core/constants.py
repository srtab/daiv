BOT_NAME = "DAIV"
BOT_LABEL = "daiv"
BOT_MAX_LABEL = "daiv-max"
BOT_AUTO_LABEL = "daiv-auto"

# User-facing terminal messages for chat runs. Rendered verbatim in the session
# transcript, so they must never carry raw exception text. Shared by the chat
# streamer (event + Run.error_message) and the sessions transcript annotator,
# which distinguishes an explicit user cancel from a failure by comparing against
# CANCELLED_BY_USER_MESSAGE.
CANCELLED_BY_USER_MESSAGE = "Stopped by user."
INTERRUPTED_MESSAGE = "Run was interrupted before completing."
RUN_FAILED_MESSAGE = "Run failed. Check server logs for details."
