import asyncio
import logging

# Substrings of the asyncio/asgiref log lines that fire when an SSE client
# disconnects mid-stream. Anything outside this allow-list keeps logging so
# unrelated cancellation bugs (timeouts, deliberate cancels, shutdown races)
# remain visible.
_ALLOWED_CANCEL_FRAGMENTS = ("Task was destroyed but it is pending", "Exception in callback", "was never awaited")


class SuppressCancelledError(logging.Filter):
    """Drop the specific CancelledError records produced by ASGI client disconnects.

    Why: Django ASGI wraps sync middleware with ``sync_to_async``; when a chat SSE
    client closes the response, the cancellation propagates through every sync
    middleware and asgiref/asyncio logs the resulting traceback. The cleanup
    itself runs correctly in the streamer's ``finally``.

    Scoped narrowly: only matches records on the ``asyncio`` / ``concurrent.futures``
    loggers whose exc_info is a CancelledError AND whose formatted message
    contains one of the known noise fragments. Anything else (including
    legitimate cancellation diagnostics from our own code) passes through.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        exc_info = record.exc_info
        if not (exc_info and exc_info[0] is not None and issubclass(exc_info[0], asyncio.CancelledError)):
            return True
        if record.name not in ("asyncio", "concurrent.futures"):
            return True
        message = record.getMessage()
        return not any(fragment in message for fragment in _ALLOWED_CANCEL_FRAGMENTS)
