import asyncio
import logging


class SuppressCancelledError(logging.Filter):
    """Drop CancelledError records emitted by asyncio when an SSE client disconnects mid-stream.

    Why: Django ASGI wraps sync middleware with ``sync_to_async``; when a client closes
    a streaming response (e.g. the chat SSE), the cancellation propagates through every
    sync middleware in the chain and asgiref/asyncio logs the resulting traceback. The
    cleanup itself runs correctly in the view's ``finally`` — the log is just noise.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        exc_info = record.exc_info
        return not (exc_info and exc_info[0] is not None and issubclass(exc_info[0], asyncio.CancelledError))
