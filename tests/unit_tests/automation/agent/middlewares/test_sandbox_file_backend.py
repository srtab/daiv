import base64
from unittest.mock import AsyncMock

import pytest
from deepagents.backends.protocol import FILE_NOT_FOUND

from automation.agent.middlewares.file_system import SandboxFileBackend
from core.sandbox.schemas import (
    FsDeleteResponse,
    FsEditResponse,
    FsEntry,
    FsError,
    FsErrorCode,
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


def _err(code: FsErrorCode, message: str = "boom") -> FsError:
    """Build a structured sandbox error the way the wire now delivers it."""
    return FsError(code=code, message=message)


@pytest.fixture
def client():
    return AsyncMock()


@pytest.fixture
def backend(client):
    be = SandboxFileBackend(client=client)
    be.bind_session("sid")
    return be


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("/workspace/repo/x.py", "/workspace/repo/x.py"),  # already sandbox-absolute → unchanged
        ("/workspace", "/workspace"),  # workspace root itself → unchanged
        ("/workspace/skills/s.md", "/workspace/skills/s.md"),  # other workspace root → unchanged
        ("/", "/workspace"),  # deepagents virtual root (path-less glob/grep/ls default) → workspace root
        ("", "/workspace"),  # empty → workspace root
        ("/daiv/slash_commands", "/workspace/repo/daiv/slash_commands"),  # workspace prefix dropped → repo-relative
        ("/src/app.py", "/workspace/repo/src/app.py"),
        # A dropped /workspace/skills (or /tmp) prefix also resolves under the repo root, NOT back
        # under /workspace/skills — the resolver can't disambiguate, and repo paths are the common case.
        ("/skills/foo.md", "/workspace/repo/skills/foo.md"),
        ("/daiv/", "/workspace/repo/daiv/"),  # trailing slash survives, no double slash
    ],
)
def test_abs_path_resolution(given, expected):
    """Paths under /workspace pass straight through; the virtual root "/" maps to the workspace root,
    and a path the model rooted at "/" (workspace prefix dropped) resolves under the repo root rather
    than being rejected on the sandbox."""
    be = SandboxFileBackend()
    assert be._abs(given) == expected
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
    client.fs_write.return_value = FsWriteResponse()
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


async def test_aread_not_found_maps_to_does_not_exist(backend, client):
    client.fs_read.return_value = FsReadResponse(error=_err(FsErrorCode.NOT_FOUND, "/workspace/repo/x does not exist"))
    result = await backend.aread("/workspace/repo/missing.txt")
    assert result.file_data is None
    assert "does not exist" in result.error


async def test_aread_directory_tells_agent_to_use_ls(backend, client):
    """Reading a directory used to silently return an arbitrary inner file's bytes; the sandbox now
    reports ``is_a_directory`` and DAIV must route the agent to the listing tool."""
    client.fs_read.return_value = FsReadResponse(error=_err(FsErrorCode.IS_A_DIRECTORY, "is a directory"))
    result = await backend.aread("/workspace/repo/pkg")
    assert result.file_data is None
    assert "is a directory" in result.error and "ls" in result.error


async def test_als_returns_paths_unchanged(backend, client):
    client.fs_ls.return_value = FsLsResponse(
        entries=[FsEntry(path="/workspace/sub", is_dir=True), FsEntry(path="/workspace/f.py", is_dir=False)]
    )
    result = await backend.als("/workspace")
    paths = {(e["path"], e["is_dir"]) for e in result.entries}
    assert ("/workspace/sub", True) in paths and ("/workspace/f.py", False) in paths
    assert client.fs_ls.call_args.args[1].path == "/workspace"


async def test_als_empty_directory_is_not_an_error(backend, client):
    """An existing-but-empty directory (no error, empty list) must read as genuinely empty, NOT as
    an error — only ``not_found`` means absent."""
    client.fs_ls.return_value = FsLsResponse(entries=[])
    result = await backend.als("/workspace/repo/empty")
    assert result.error is None
    assert result.entries == []


async def test_als_missing_directory_surfaces_not_found(backend, client):
    """Absence is now distinct from emptiness: a missing path is surfaced as an error so the agent
    learns the path is wrong instead of concluding the directory is empty."""
    client.fs_ls.return_value = FsLsResponse(entries=[], error=_err(FsErrorCode.NOT_FOUND, "does not exist"))
    result = await backend.als("/workspace/repo/typo")
    assert result.entries is None
    assert result.error is not None and "does not exist" in result.error


async def test_agrep_returns_match_paths_unchanged(backend, client):
    client.fs_grep.return_value = FsGrepResponse(matches=[FsGrepMatch(path="/workspace/a.py", line=2, text="x")])
    result = await backend.agrep("x", path="/workspace", glob=None)
    assert result.matches[0]["path"] == "/workspace/a.py"
    assert result.matches[0]["line"] == 2


async def test_agrep_no_match_is_not_an_error(backend, client):
    """No match (empty list, no error) is a genuine zero-results outcome, not a failure."""
    client.fs_grep.return_value = FsGrepResponse(matches=[])
    result = await backend.agrep("nope", path="/workspace", glob=None)
    assert result.error is None
    assert result.matches == []


async def test_agrep_threads_extended_options_to_wire(backend, client):
    """The ripgrep options map straight onto the FsGrepRequest sent to the sandbox."""
    client.fs_grep.return_value = FsGrepResponse(matches=[])
    await backend.agrep("fo+", path="/workspace", glob="*.py", case_insensitive=True, multiline=True, head_limit=5)
    req = client.fs_grep.call_args.args[1]
    assert req.pattern == "fo+"
    assert req.glob == "*.py"
    assert req.case_insensitive is True
    assert req.multiline is True
    assert req.head_limit == 5


async def test_agrep_defaults_leave_extended_options_off(backend, client):
    """Omitting the extended options sends the schema defaults (regex on, everything else off)."""
    client.fs_grep.return_value = FsGrepResponse(matches=[])
    await backend.agrep("foo", path="/workspace", glob=None)
    req = client.fs_grep.call_args.args[1]
    assert req.case_insensitive is False
    assert req.multiline is False
    assert req.head_limit is None


async def test_agrep_invalid_pattern_surfaces_engine_message_verbatim(backend, client):
    """A regex the engine cannot parse comes back as invalid_pattern; the engine's own parse
    message must reach the model unchanged (no DAIV-authored hint for this code)."""
    client.fs_grep.return_value = FsGrepResponse(
        matches=[], error=_err(FsErrorCode.INVALID_PATTERN, "regex parse error: unclosed group")
    )
    result = await backend.agrep("foo(", path="/workspace", glob=None)
    assert result.matches is None
    assert result.error is not None and "regex parse error: unclosed group" in result.error


async def test_aedit_success_and_error_passthrough(backend, client):
    client.fs_edit.return_value = FsEditResponse(occurrences=2)
    ok = await backend.aedit("/workspace/repo/a.py", "old", "new", replace_all=True)
    assert ok.error is None and ok.occurrences == 2
    # string_not_found carries actionable retry guidance in `message`; it must pass through verbatim.
    client.fs_edit.return_value = FsEditResponse(
        error=_err(FsErrorCode.STRING_NOT_FOUND, "old string not found; check whitespace and the trailing newline")
    )
    bad = await backend.aedit("/workspace/repo/a.py", "nope", "x")
    assert bad.error is not None and "trailing newline" in bad.error


async def test_delete_removed_returns_true(backend, client):
    client.fs_delete.return_value = FsDeleteResponse(removed=True)
    assert await backend.delete("/workspace/repo/a.py") is True
    assert client.fs_delete.call_args.args[1].path == "/workspace/repo/a.py"
    assert await backend.stat_mode("/workspace/repo/a.py") == 0o644


async def test_delete_already_absent_is_idempotent(backend, client):
    """Deleting a path that was never there is success (ok=True) with removed=False — the protocol
    contract is "the file is gone", and it is."""
    client.fs_delete.return_value = FsDeleteResponse(removed=False)
    assert await backend.delete("/workspace/repo/gone.py") is True


async def test_delete_failure_logs_reason(backend, client, caplog):
    client.fs_delete.return_value = FsDeleteResponse(error=_err(FsErrorCode.PERMISSION_DENIED, "permission denied"))
    with caplog.at_level("WARNING"):
        assert await backend.delete("/workspace/repo/a.py") is False
    assert "permission denied" in caplog.text


async def test_als_propagates_error(backend, client):
    client.fs_ls.return_value = FsLsResponse(entries=[], error=_err(FsErrorCode.PERMISSION_DENIED, "permission denied"))
    result = await backend.als("/workspace")
    assert result.entries is None
    assert result.error is not None and "permission denied" in result.error


async def test_agrep_propagates_error(backend, client):
    """A real sandbox failure passes through verbatim (grep never ran)."""
    client.fs_grep.return_value = FsGrepResponse(matches=[], error=_err(FsErrorCode.EXEC_FAILED, "grep failed"))
    result = await backend.agrep("foo|bar", path="/workspace", glob=None)
    assert result.matches is None
    assert result.error is not None and "grep failed" in result.error


async def test_aglob_returns_paths_and_propagates_error(backend, client):
    client.fs_glob.return_value = FsGlobResponse(matches=[FsEntry(path="/workspace/a.py", is_dir=False)])
    ok = await backend.aglob("*.py", path="/workspace")
    assert ok.error is None
    assert ok.matches[0]["path"] == "/workspace/a.py"
    assert client.fs_glob.call_args.args[1].path == "/workspace"
    client.fs_glob.return_value = FsGlobResponse(matches=[], error=_err(FsErrorCode.NOT_A_DIRECTORY, "not a directory"))
    bad = await backend.aglob("[", path="/workspace/a.py")
    assert bad.matches is None and bad.error is not None and "not a directory" in bad.error


async def test_invalid_path_is_a_recoverable_tool_error_not_a_crash(backend, client):
    """Malformed paths now come back as HTTP 200 with ``invalid_path`` (they used to be HTTP 400 for
    ls/grep/glob). DAIV must surface them as a recoverable tool-result error, never raise."""
    client.fs_ls.return_value = FsLsResponse(
        entries=[], error=_err(FsErrorCode.INVALID_PATH, "path must be under /workspace")
    )
    result = await backend.als("/etc/passwd")
    assert result.entries is None
    assert result.error is not None and "path must be under /workspace" in result.error


async def test_awrite_already_exists_routes_to_edit(backend, client):
    """write_file is create-only; an existing target returns ``already_exists`` and the agent should
    be told to use edit_file instead."""
    client.fs_write.return_value = FsWriteResponse(error=_err(FsErrorCode.ALREADY_EXISTS, "already exists"))
    r = await backend.awrite("/workspace/repo/a.txt", "x")
    assert r.path is None
    assert "Failed to write file" in r.error and "already exists" in r.error and "edit_file" in r.error


async def test_awrite_failure_passes_through_message(backend, client):
    client.fs_write.return_value = FsWriteResponse(error=_err(FsErrorCode.EXEC_FAILED, "disk full"))
    r = await backend.awrite("/workspace/repo/a.txt", "x")
    assert r.path is None and "disk full" in r.error


async def test_aupload_files_ok_and_failure(backend, client):
    client.fs_write.return_value = FsWriteResponse()
    ok = await backend.aupload_files([("/workspace/skills/a.txt", b"x")])
    assert ok[0].path == "/workspace/skills/a.txt" and ok[0].error is None
    assert client.fs_write.call_args.args[1].path == "/workspace/skills/a.txt"
    client.fs_write.return_value = FsWriteResponse(error=_err(FsErrorCode.EXEC_FAILED, "disk full"))
    bad = await backend.aupload_files([("/workspace/skills/a.txt", b"x")])
    assert bad[0].error is not None and "disk full" in bad[0].error


async def test_adownload_files_branches(backend, client):
    client.fs_read.return_value = FsReadResponse(content="hi", encoding="utf-8")
    text = await backend.adownload_files(["/workspace/repo/a.txt"])
    assert text[0].content == b"hi" and text[0].error is None

    client.fs_read.return_value = FsReadResponse(content=base64.b64encode(b"\x00\x01").decode(), encoding="base64")
    binary = await backend.adownload_files(["/workspace/repo/b.bin"])
    assert binary[0].content == b"\x00\x01"

    # not_found must map to deepagents' FILE_NOT_FOUND sentinel (the old code compared the raw error
    # string to that sentinel, which silently stopped matching once errors became objects).
    client.fs_read.return_value = FsReadResponse(error=_err(FsErrorCode.NOT_FOUND, "does not exist"))
    missing = await backend.adownload_files(["/workspace/repo/gone.txt"])
    assert missing[0].error == FILE_NOT_FOUND and missing[0].content is None

    client.fs_read.return_value = FsReadResponse(error=_err(FsErrorCode.EXEC_FAILED, "boom"))
    err = await backend.adownload_files(["/workspace/repo/x.txt"])
    assert err[0].error is not None and "boom" in err[0].error and err[0].content is None


@pytest.mark.parametrize(
    ("code", "needle"),
    [
        (FsErrorCode.NOT_FOUND, "does not exist"),
        (FsErrorCode.IS_A_DIRECTORY, "ls"),
        (FsErrorCode.NOT_A_DIRECTORY, "read_file"),
        (FsErrorCode.ALREADY_EXISTS, "edit_file"),
        (FsErrorCode.NOT_A_TEXT_FILE, "text file"),
    ],
)
async def test_error_codes_get_distinct_actionable_hints(backend, client, code, needle):
    """Each routing-relevant code maps to its own actionable hint — they must not collapse into one
    generic 'operation failed' message."""
    client.fs_ls.return_value = FsLsResponse(entries=[], error=_err(code, "server message"))
    result = await backend.als("/workspace/x")
    assert needle in result.error


@pytest.mark.parametrize(
    "code", [FsErrorCode.STRING_NOT_FOUND, FsErrorCode.INVALID_OFFSET, FsErrorCode.EXEC_FAILED, FsErrorCode.TOO_LARGE]
)
async def test_unrouted_codes_pass_server_message_through(backend, client, code):
    """Codes without a DAIV routing hint surface the server's message verbatim (it already carries
    the actionable detail: retry hints, the bad offset, the underlying failure)."""
    client.fs_read.return_value = FsReadResponse(error=_err(code, "very specific server detail"))
    result = await backend.aread("/workspace/x")
    assert "very specific server detail" in result.error


def _http_status_error(status_code: int, detail: str | None = None):
    import httpx

    request = httpx.Request("POST", "http://sandbox:8000/session/sid/fs/ls")
    response = httpx.Response(status_code, json={"detail": detail} if detail is not None else {}, request=request)
    return httpx.HTTPStatusError(f"{status_code}", request=request, response=response)


async def test_busy_409_degrades_to_a_soft_retryable_error_not_a_crash(backend, client):
    """The reported failure: two grep tool calls run concurrently against one session, the sandbox
    serializes them on its per-session lock and the loser gets 409 "Session is busy". That op never
    ran, so it must surface as a soft, retryable tool result — never propagate and abort the run."""
    client.fs_grep.side_effect = _http_status_error(409, "Session is busy")
    result = await backend.agrep("slash_command", path="/workspace", glob=None)
    assert result.matches is None
    assert result.error is not None
    assert result.error.startswith("Grep 'slash_command':") and "retry" in result.error


@pytest.mark.parametrize("status", [408, 409, 429, 500, 503])
async def test_transient_http_error_degrades_to_retry_hint(backend, client, status, caplog):
    """A retryable status (lock contention, timeout, rate-limit, transient 5xx) becomes a soft
    'retry once' result on an agent-facing op rather than crashing the run, logged at WARNING (not
    ERROR) so a routine, recoverable contention doesn't surface as a tracked error."""
    client.fs_ls.side_effect = _http_status_error(status)
    with caplog.at_level("WARNING", logger="daiv.tools"):
        result = await backend.als("/workspace/repo")
    assert result.entries is None
    assert result.error is not None and "retry" in result.error
    fs_records = [r for r in caplog.records if r.name == "daiv.tools"]
    assert any(r.levelname == "WARNING" for r in fs_records) and not any(r.levelname == "ERROR" for r in fs_records)
    assert "transport failure" in caplog.text


@pytest.mark.parametrize("status", [401, 403, 404, 422])
async def test_permanent_http_error_tells_agent_tools_unavailable(backend, client, status, caplog):
    """A non-retryable status (auth, session-gone, bad-request) becomes a soft 'tools unavailable'
    result so the model can wind down gracefully, instead of the run aborting with a stack trace.
    Logged at ERROR (with traceback) so the genuine fault still reaches the logs / Sentry rather than
    vanishing into a tool message."""
    client.fs_read.side_effect = _http_status_error(status, "nope")
    with caplog.at_level("ERROR", logger="daiv.tools"):
        result = await backend.aread("/workspace/repo/x.py")
    assert result.file_data is None
    assert result.error is not None and "unavailable" in result.error
    assert any(r.levelname == "ERROR" and r.name == "daiv.tools" for r in caplog.records)
    assert "transport failure" in caplog.text


async def test_transport_error_with_no_response_is_transient(backend, client):
    """A transport error with no HTTP response at all (timeout/connection blip) is transient — the
    op did not run, so the agent is told to retry once."""
    import httpx

    client.fs_glob.side_effect = httpx.ConnectError("connection refused")
    result = await backend.aglob("**/*.py")
    assert result.matches is None and result.error is not None and "retry" in result.error


async def test_write_busy_409_degrades_to_retry_hint(backend, client):
    """write_file keeps its own per-op prefix and never raises on a busy-409."""
    client.fs_write.side_effect = _http_status_error(409, "Session is busy")
    result = await backend.awrite("/workspace/repo/new.py", "x")
    assert result.path is None
    assert result.error is not None and result.error.startswith("Failed to write file") and "retry" in result.error


async def test_edit_busy_409_degrades_to_retry_hint(backend, client):
    client.fs_edit.side_effect = _http_status_error(409, "Session is busy")
    result = await backend.aedit("/workspace/repo/a.py", "old", "new")
    assert result.path is None
    assert result.error is not None and result.error.startswith("Error editing file") and "retry" in result.error


async def test_delete_transport_error_returns_false_and_logs(backend, client, caplog):
    """``delete`` has no error channel (bare bool), so a transport fault is a failed delete — logged
    so it is diagnosable rather than a silent False."""
    client.fs_delete.side_effect = _http_status_error(409, "Session is busy")
    with caplog.at_level("WARNING", logger="daiv.tools"):
        assert await backend.delete("/workspace/repo/a.py") is False
    assert "transport failure" in caplog.text


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


def test_backend_does_not_advertise_execution():
    """SandboxFileBackend must NOT be a deepagents SandboxBackendProtocol.

    deepagents' FilesystemMiddleware always registers an `execute` tool, gated only at call
    time on `supports_execution(backend)`. Implementing the protocol would make that ungated
    tool live, bypassing daiv's _check_command_policy (and would break the read-only explore
    subagent, which combines _permissions with this backend). Command execution must stay on
    the policy-gated `bash` tool. See the design spec's "Rejected alternative".
    """
    from deepagents.backends.protocol import SandboxBackendProtocol
    from deepagents.middleware.filesystem import supports_execution

    be = SandboxFileBackend(client=AsyncMock())
    be.bind_session("sid")
    assert not isinstance(be, SandboxBackendProtocol)
    assert supports_execution(be) is False
