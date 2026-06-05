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
    RunCommandResult,
    RunCommandsRequest,
    RunCommandsResponse,
)


@pytest.fixture
def client():
    return AsyncMock()


@pytest.fixture
def backend(client):
    be = SandboxFileBackend(client=client)
    be.bind_session("sid")
    return be


def test_paths_pass_through_unchanged():
    # The agent already addresses files by sandbox-absolute paths; the backend must NOT translate.
    be = SandboxFileBackend()
    assert be._abs("/workspace/repo/x.py") == "/workspace/repo/x.py"
    assert be._abs("/") == "/"
    assert be._abs("") == "/"
    assert be._rel("/workspace/repo/x.py") == "/workspace/repo/x.py"


async def test_calls_before_bind_raise():
    be = SandboxFileBackend()
    with pytest.raises(RuntimeError, match="not bound"):
        await be.als("/")


def test_rebind_same_session_is_noop(client):
    be = SandboxFileBackend(client=client)
    be.bind_session("sid")
    be.bind_session("sid")  # must not raise (subagents share the parent-bound backend)
    assert be._session_id == "sid"


def test_rebind_different_session_raises(client):
    be = SandboxFileBackend(client=client)
    be.bind_session("sid")
    with pytest.raises(RuntimeError, match="already bound to session"):
        be.bind_session("other-sid")


async def test_bound_backend_sends_absolute_paths_unchanged(client):
    be = SandboxFileBackend(client=client)
    be.bind_session("sid")
    client.fs_read.return_value = FsReadResponse(content="x", encoding="utf-8")
    await be.aread("/workspace/repo/pkg/mod.py")
    assert client.fs_read.call_args.args[0] == "sid"
    assert client.fs_read.call_args.args[1].path == "/workspace/repo/pkg/mod.py"


async def test_awrite_sends_absolute_path(backend, client):
    client.fs_write.return_value = FsWriteResponse(ok=True)
    result = await backend.awrite("/workspace/tmp/foo.txt", "hello\n")
    sent = client.fs_write.call_args.args[1]
    assert sent.path == "/workspace/tmp/foo.txt"
    assert sent.content == b"hello\n"
    assert result.error is None and result.path == "/workspace/tmp/foo.txt"


async def test_aread_returns_filedata(backend, client):
    client.fs_read.return_value = FsReadResponse(content="hi", encoding="utf-8")
    result = await backend.aread("/workspace/repo/foo.txt")
    assert result.error is None
    assert result.file_data["content"] == "hi"
    assert result.file_data["encoding"] == "utf-8"


async def test_aread_maps_error(backend, client):
    client.fs_read.return_value = FsReadResponse(error="file_not_found")
    result = await backend.aread("/workspace/repo/missing.txt")
    assert result.file_data is None and "file_not_found" in result.error


async def test_als_returns_paths_unchanged(backend, client):
    client.fs_ls.return_value = FsLsResponse(
        entries=[FsEntry(path="/workspace/sub", is_dir=True), FsEntry(path="/workspace/f.py", is_dir=False)]
    )
    result = await backend.als("/workspace")
    paths = {(e["path"], e["is_dir"]) for e in result.entries}
    assert ("/workspace/sub", True) in paths and ("/workspace/f.py", False) in paths
    assert client.fs_ls.call_args.args[1].path == "/workspace"


async def test_agrep_returns_match_paths_unchanged(backend, client):
    client.fs_grep.return_value = FsGrepResponse(matches=[FsGrepMatch(path="/workspace/a.py", line=2, text="x")])
    result = await backend.agrep("x", path="/workspace", glob=None)
    assert result.matches[0]["path"] == "/workspace/a.py"
    assert result.matches[0]["line"] == 2


async def test_aedit_success_and_error(backend, client):
    client.fs_edit.return_value = FsEditResponse(occurrences=2)
    ok = await backend.aedit("/workspace/repo/a.py", "old", "new", replace_all=True)
    assert ok.error is None and ok.occurrences == 2
    client.fs_edit.return_value = FsEditResponse(error="string_not_found")
    bad = await backend.aedit("/workspace/repo/a.py", "nope", "x")
    assert bad.error is not None


async def test_delete_and_stat_mode(backend, client):
    client.fs_delete.return_value = FsDeleteResponse(ok=True)
    assert await backend.delete("/workspace/repo/a.py") is True
    assert client.fs_delete.call_args.args[1].path == "/workspace/repo/a.py"
    assert await backend.stat_mode("/workspace/repo/a.py") == 0o644


async def test_delete_failure_logs_reason(backend, client, caplog):
    client.fs_delete.return_value = FsDeleteResponse(ok=False, error="permission_denied")
    with caplog.at_level("WARNING"):
        assert await backend.delete("/workspace/repo/a.py") is False
    assert "permission_denied" in caplog.text


async def test_als_propagates_error(backend, client):
    client.fs_ls.return_value = FsLsResponse(entries=[], error="permission_denied")
    result = await backend.als("/workspace")
    assert result.entries is None
    assert result.error is not None and "permission_denied" in result.error


async def test_agrep_propagates_error(backend, client):
    client.fs_grep.return_value = FsGrepResponse(matches=[], error="grep_failed")
    result = await backend.agrep("x", path="/workspace", glob=None)
    assert result.matches is None
    assert result.error is not None and "grep_failed" in result.error


async def test_aglob_returns_paths_and_propagates_error(backend, client):
    client.fs_glob.return_value = FsGlobResponse(matches=[FsEntry(path="/workspace/a.py", is_dir=False)])
    ok = await backend.aglob("*.py", path="/workspace")
    assert ok.error is None
    assert ok.matches[0]["path"] == "/workspace/a.py"
    assert client.fs_glob.call_args.args[1].path == "/workspace"
    client.fs_glob.return_value = FsGlobResponse(matches=[], error="bad_glob")
    bad = await backend.aglob("[", path="/workspace")
    assert bad.matches is None and bad.error is not None and "bad_glob" in bad.error


async def test_awrite_failure_maps_error(backend, client):
    client.fs_write.return_value = FsWriteResponse(ok=False, error="disk full")
    r = await backend.awrite("/workspace/repo/a.txt", "x")
    assert r.path is None and "disk full" in r.error
    client.fs_write.return_value = FsWriteResponse(ok=False)
    r2 = await backend.awrite("/workspace/repo/a.txt", "x")
    assert r2.path is None and "unknown sandbox error" in r2.error


async def test_aupload_files_ok_and_failure(backend, client):
    client.fs_write.return_value = FsWriteResponse(ok=True)
    ok = await backend.aupload_files([("/workspace/skills/a.txt", b"x")])
    assert ok[0].path == "/workspace/skills/a.txt" and ok[0].error is None
    assert client.fs_write.call_args.args[1].path == "/workspace/skills/a.txt"
    client.fs_write.return_value = FsWriteResponse(ok=False)
    bad = await backend.aupload_files([("/workspace/skills/a.txt", b"x")])
    assert bad[0].error == "unknown sandbox error"


async def test_adownload_files_branches(backend, client):
    client.fs_read.return_value = FsReadResponse(content="hi", encoding="utf-8")
    text = await backend.adownload_files(["/workspace/repo/a.txt"])
    assert text[0].content == b"hi" and text[0].error is None

    client.fs_read.return_value = FsReadResponse(content=base64.b64encode(b"\x00\x01").decode(), encoding="base64")
    binary = await backend.adownload_files(["/workspace/repo/b.bin"])
    assert binary[0].content == b"\x00\x01"

    client.fs_read.return_value = FsReadResponse(error="file_not_found")
    missing = await backend.adownload_files(["/workspace/repo/gone.txt"])
    assert missing[0].error == FILE_NOT_FOUND and missing[0].content is None

    client.fs_read.return_value = FsReadResponse(error="boom")
    err = await backend.adownload_files(["/workspace/repo/x.txt"])
    assert err[0].error == "boom" and err[0].content is None


async def test_run_commands_forwards_to_client(backend, client):
    client.run_commands.return_value = RunCommandsResponse(
        results=[RunCommandResult(command="echo hi", output="hi", exit_code=0)]
    )
    result = await backend.run_commands(["echo hi", "ls"], fail_fast=False)

    assert result.results[0].output == "hi"
    # Forwarded under the bound session id, as a RunCommandsRequest carrying the list + fail_fast.
    assert client.run_commands.call_args.args[0] == "sid"
    sent = client.run_commands.call_args.args[1]
    assert isinstance(sent, RunCommandsRequest)
    assert sent.commands == ["echo hi", "ls"]
    assert sent.fail_fast is False


async def test_run_commands_before_bind_raises():
    be = SandboxFileBackend()
    with pytest.raises(RuntimeError, match="not bound"):
        await be.run_commands(["echo hi"], fail_fast=True)


async def test_run_commands_propagates_transport_error(backend, client):
    # Unlike the bash tool, the backend is a raising pass-through; graceful degradation
    # is the caller's job. A transport error must NOT be swallowed here.
    client.run_commands.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError, match="boom"):
        await backend.run_commands(["echo hi"], fail_fast=True)
