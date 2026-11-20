import io

from core.responses import ThreadedFileResponse


class _RecordingFile(io.BytesIO):
    def __init__(self, content: bytes) -> None:
        super().__init__(content)
        self.reads = 0

    def read(self, size: int | None = -1) -> bytes:
        self.reads += 1
        return super().read(size)


async def test_streams_a_file_one_block_at_a_time():
    fh = _RecordingFile(b"x" * (ThreadedFileResponse.block_size * 2 + 1))
    response = ThreadedFileResponse(fh)
    chunks = aiter(response)

    first = await anext(chunks)

    assert (len(first), fh.reads) == (ThreadedFileResponse.block_size, 1)
    assert [len(chunk) async for chunk in chunks] == [ThreadedFileResponse.block_size, 1]


async def test_a_response_without_a_file_streams_as_django_does():
    response = ThreadedFileResponse(iter([b"a", b"b"]))

    assert [chunk async for chunk in response] == [b"a", b"b"]
