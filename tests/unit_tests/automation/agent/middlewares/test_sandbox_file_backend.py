from unittest.mock import AsyncMock

import pytest

from automation.agent.middlewares.file_system import SandboxFileBackend
from core.sandbox.schemas import (
    FsDeleteResponse,
    FsEditResponse,
    FsEntry,
    FsGrepMatch,
    FsGrepResponse,
    FsLsResponse,
    FsReadResponse,
    FsWriteResponse,
)


@pytest.fixture
def client():
    return AsyncMock()


@pytest.fixture
def backend(client):
    return SandboxFileBackend(client=client, session_id="sid")


async def test_awrite_maps_route_relative_to_scratch_abs(backend, client):
    client.fs_write.return_value = FsWriteResponse(ok=True)
    result = await backend.awrite("/foo.txt", "hello\n")
    sent = client.fs_write.call_args.args[1]
    assert sent.path == "/scratch/foo.txt"
    assert sent.content == b"hello\n"
    assert result.error is None and result.path == "/foo.txt"


async def test_aread_returns_filedata(backend, client):
    client.fs_read.return_value = FsReadResponse(content="hi", encoding="utf-8")
    result = await backend.aread("/foo.txt")
    assert result.error is None
    assert result.file_data["content"] == "hi"
    assert result.file_data["encoding"] == "utf-8"


async def test_aread_maps_error(backend, client):
    client.fs_read.return_value = FsReadResponse(error="file_not_found")
    result = await backend.aread("/missing.txt")
    assert result.file_data is None and "file_not_found" in result.error


async def test_als_remaps_paths_to_route_relative(backend, client):
    client.fs_ls.return_value = FsLsResponse(
        entries=[FsEntry(path="/scratch/sub", is_dir=True), FsEntry(path="/scratch/f.py", is_dir=False)]
    )
    result = await backend.als("/")
    paths = {(e["path"], e["is_dir"]) for e in result.entries}
    assert ("/sub", True) in paths and ("/f.py", False) in paths
    assert client.fs_ls.call_args.args[1].path == "/scratch"


async def test_agrep_remaps_match_paths(backend, client):
    client.fs_grep.return_value = FsGrepResponse(matches=[FsGrepMatch(path="/scratch/a.py", line=2, text="x")])
    result = await backend.agrep("x", path="/", glob=None)
    assert result.matches[0]["path"] == "/a.py"
    assert result.matches[0]["line"] == 2


async def test_aedit_success_and_error(backend, client):
    client.fs_edit.return_value = FsEditResponse(occurrences=2)
    ok = await backend.aedit("/a.py", "old", "new", replace_all=True)
    assert ok.error is None and ok.occurrences == 2
    client.fs_edit.return_value = FsEditResponse(error="string_not_found")
    bad = await backend.aedit("/a.py", "nope", "x")
    assert bad.error is not None


async def test_delete_and_stat_mode(backend, client):
    client.fs_delete.return_value = FsDeleteResponse(ok=True)
    assert await backend.delete("/a.py") is True
    assert client.fs_delete.call_args.args[1].path == "/scratch/a.py"
    assert await backend.stat_mode("/a.py") == 0o644
