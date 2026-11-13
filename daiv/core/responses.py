from __future__ import annotations

from typing import TYPE_CHECKING

from django.http import FileResponse

from asgiref.sync import sync_to_async

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class ThreadedFileResponse(FileResponse):
    """A ``FileResponse`` that streams a file under ASGI one block at a time, read in a worker thread.

    Django's own reads a sync file whole into memory before sending any of it to an ASGI server
    (``StreamingHttpResponse.__aiter__``), so each request for a large file would hold all of it.
    """

    block_size = 256 * 1024

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if (filelike := self.file_to_stream) is None:
            async for part in super().__aiter__():
                yield part
            return
        read = sync_to_async(filelike.read, thread_sensitive=False)
        while chunk := await read(self.block_size):
            yield chunk
