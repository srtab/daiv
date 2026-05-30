import base64
from unittest.mock import AsyncMock

import pytest
from deepagents.backends.protocol import FILE_NOT_FOUND

from automation.agent.middlewares.file_system import SandboxFileBackend
from core.sandbox.schemas import (
    FsDeleteResponse,
    FsEditResponse,
    FsEntry,
    FsGlobResponse,
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


# -- error propagation: a soft sandbox failure (200 + error, empty list) must surface --


async def test_als_propagates_error(backend, client):
    client.fs_ls.return_value = FsLsResponse(entries=[], error="permission_denied")
    result = await backend.als("/")
    assert result.entries is None
    assert result.error is not None and "permission_denied" in result.error


async def test_agrep_propagates_error(backend, client):
    client.fs_grep.return_value = FsGrepResponse(matches=[], error="grep_failed")
    result = await backend.agrep("x", path="/", glob=None)
    assert result.matches is None
    assert result.error is not None and "grep_failed" in result.error


async def test_aglob_remaps_and_propagates_error(backend, client):
    client.fs_glob.return_value = FsGlobResponse(matches=[FsEntry(path="/scratch/a.py", is_dir=False)])
    ok = await backend.aglob("*.py", path="/")
    assert ok.error is None
    assert ok.matches[0]["path"] == "/a.py"
    assert client.fs_glob.call_args.args[1].path == "/scratch"
    client.fs_glob.return_value = FsGlobResponse(matches=[], error="bad_glob")
    bad = await backend.aglob("[", path="/")
    assert bad.matches is None and bad.error is not None and "bad_glob" in bad.error


async def test_awrite_failure_maps_error(backend, client):
    client.fs_write.return_value = FsWriteResponse(ok=False, error="disk full")
    r = await backend.awrite("/a.txt", "x")
    assert r.path is None and "disk full" in r.error
    # ok=False with no error string must not be reported as success
    client.fs_write.return_value = FsWriteResponse(ok=False)
    r2 = await backend.awrite("/a.txt", "x")
    assert r2.path is None and "unknown sandbox error" in r2.error


async def test_aupload_files_ok_and_failure(backend, client):
    client.fs_write.return_value = FsWriteResponse(ok=True)
    ok = await backend.aupload_files([("/a.txt", b"x")])
    assert ok[0].path == "/a.txt" and ok[0].error is None
    assert client.fs_write.call_args.args[1].path == "/scratch/a.txt"
    # ok=False, error=None must still surface as an error (not silent success)
    client.fs_write.return_value = FsWriteResponse(ok=False)
    bad = await backend.aupload_files([("/a.txt", b"x")])
    assert bad[0].error == "unknown sandbox error"


async def test_adownload_files_branches(backend, client):
    client.fs_read.return_value = FsReadResponse(content="hi", encoding="utf-8")
    text = await backend.adownload_files(["/a.txt"])
    assert text[0].content == b"hi" and text[0].error is None

    client.fs_read.return_value = FsReadResponse(content=base64.b64encode(b"\x00\x01").decode(), encoding="base64")
    binary = await backend.adownload_files(["/b.bin"])
    assert binary[0].content == b"\x00\x01"

    client.fs_read.return_value = FsReadResponse(error="file_not_found")
    missing = await backend.adownload_files(["/gone.txt"])
    assert missing[0].error == FILE_NOT_FOUND and missing[0].content is None

    client.fs_read.return_value = FsReadResponse(error="boom")
    err = await backend.adownload_files(["/x.txt"])
    assert err[0].error == "boom" and err[0].content is None
