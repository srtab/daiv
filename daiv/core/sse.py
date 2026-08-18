"""Shared server-sent-events plumbing.

Three endpoints stream SSE to the browser — the chat run tail
(``chat.api.views``), the session status stream (``sessions.views``) and the nav
badges (``accounts.api.views``). They differ in what they put on the wire, not in
how it is framed, so the response wrapper, the frame vocabulary and the duration
cap live here rather than being restated per endpoint.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from django.http import StreamingHttpResponse

if TYPE_CHECKING:
    from collections.abc import AsyncIterable, Iterable

# Bounds how long one connection occupies a worker. Closing without a terminal frame is
# what makes EventSource reconnect, so each endpoint's reconnect is also its resync.
STREAM_MAX_DURATION_S = 300.0

KEEP_ALIVE_FRAME = ": keep-alive\n\n"


def retry_frame(milliseconds: int) -> str:
    """Tell the browser how long to wait before reconnecting."""
    return f"retry: {milliseconds}\n\n"


def end_frame(reason: str) -> str:
    """Terminal frame. Tells the client to stop reconnecting, unlike a silent close."""
    return f"event: end\ndata: {json.dumps({'reason': reason})}\n\n"


def sse_response(frames: Iterable[str] | AsyncIterable[str]) -> StreamingHttpResponse:
    """Wrap a frame generator in the response every SSE endpoint shares.

    ``X-Accel-Buffering: no`` + ``Cache-Control: no-cache`` are the load-bearing
    headers that stop nginx from buffering the stream — keep them in one place so
    the callers can't drift.
    """
    return StreamingHttpResponse(
        frames, content_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )
