import base64
from unittest.mock import AsyncMock

import pytest
from deepagents.backends.protocol import FILE_NOT_FOUND

from automation.agent.workspace.sandbox_backend import SandboxFileBackend
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
        ("/workspace/", "/workspace/"),  # trailing slash preserved (no longer collapsed to /workspace)
        ("/workspace/skills/s.md", "/workspace/skills/s.md"),  # other workspace root → unchanged
        ("/", "/workspace"),  # deepagents virtual root (path-less glob/grep/ls default) → workspace root
        ("", "/workspace"),  # empty → workspace root
        # Out-of-workspace paths pass straight through UNCHANGED — never re-homed under the repo root
        # (a dropped-prefix repo slip and a literal out-of-workspace path are indistinguishable here,
        # so the sandbox is left to reject them; see ``SandboxFileBackend._abs``).
        ("/daiv/slash_commands", "/daiv/slash_commands"),  # dropped-prefix repo slip
        (
            "/home/daiv-sandbox/.local/lib/python3.14/site-packages/dbt/impl.py",
            "/home/daiv-sandbox/.local/lib/python3.14/site-packages/dbt/impl.py",
        ),
        # The removed ``startswith("/workspace/")`` branch used to gate these; both must pass through
        # now and be rejected by the sandbox (a prefix collision is NOT in-workspace; a ``..`` segment
        # is normalised+rejected sandbox-side, never lexically collapsed here).
        ("/workspacefoo/x", "/workspacefoo/x"),  # prefix collision — not under /workspace
        ("/workspace/repo/../../etc", "/workspace/repo/../../etc"),  # '..' passed through, not collapsed
    ],
)
def test_abs_path_resolution(given, expected):
    """The only normalisation is the path-less default ("" / "/") → the workspace root. Every other
    path — including an out-of-workspace path or a dropped-prefix repo slip — passes through unchanged
    so the sandbox can accept it (when under /workspace) or reject it with ``invalid_path``."""
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


async def test_agrep_no_match_with_regex_looking_pattern_does_not_hint_here(backend, client):
    """The literal-semantics hint deliberately does NOT live at this level: the composite
    treats a sub-backend GrepResult.error as a backend failure and would suppress real
    matches from sibling backends. A zero-match here stays a clean empty result; the hint
    fires on the aggregate in DAIVCompositeBackend.agrep (tested in test_file_system.py)."""
    client.fs_grep.return_value = FsGrepResponse(matches=[])
    result = await backend.agrep("get_catalog|list_relations", path="/workspace", glob=None)
    assert result.error is None
    assert result.matches == []


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


async def test_unlink_removed_returns_true(backend, client):
    client.fs_delete.return_value = FsDeleteResponse(removed=True)
    assert await backend.unlink("/workspace/repo/a.py") is True
    assert client.fs_delete.call_args.args[1].path == "/workspace/repo/a.py"
    assert await backend.stat_mode("/workspace/repo/a.py") == 0o644


async def test_unlink_already_absent_is_idempotent(backend, client):
    """Deleting a path that was never there is success (ok=True) with removed=False — the protocol
    contract is "the file is gone", and it is."""
    client.fs_delete.return_value = FsDeleteResponse(removed=False)
    assert await backend.unlink("/workspace/repo/gone.py") is True


async def test_unlink_failure_logs_reason(backend, client, caplog):
    client.fs_delete.return_value = FsDeleteResponse(error=_err(FsErrorCode.PERMISSION_DENIED, "permission denied"))
    with caplog.at_level("WARNING"):
        assert await backend.unlink("/workspace/repo/a.py") is False
    assert "permission denied" in caplog.text


async def test_als_propagates_error(backend, client):
    client.fs_ls.return_value = FsLsResponse(entries=[], error=_err(FsErrorCode.PERMISSION_DENIED, "permission denied"))
    result = await backend.als("/workspace")
    assert result.entries is None
    assert result.error is not None and "permission denied" in result.error


async def test_agrep_propagates_error(backend, client):
    """A real sandbox failure passes through verbatim — even for a regex-looking pattern, the
    literal-semantics hint must never replace a genuine error (grep never ran)."""
    client.fs_grep.return_value = FsGrepResponse(matches=[], error=_err(FsErrorCode.EXEC_FAILED, "grep failed"))
    result = await backend.agrep("foo|bar", path="/workspace", glob=None)
    assert result.matches is None
    assert result.error is not None and "grep failed" in result.error
    assert "LITERAL" not in result.error


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
    """Malformed / out-of-workspace paths now come back as HTTP 200 with ``invalid_path`` (they used to
    be HTTP 400 for ls/grep/glob). DAIV must surface them as a recoverable tool-result error (never
    raise) and route the agent to /workspace or the bash tool rather than echo the raw server text."""
    client.fs_ls.return_value = FsLsResponse(
        entries=[], error=_err(FsErrorCode.INVALID_PATH, "path must be under /workspace")
    )
    result = await backend.als("/etc/passwd")
    assert result.entries is None
    assert result.error is not None and "/workspace" in result.error and "bash" in result.error


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
        (FsErrorCode.INVALID_PATH, "bash"),  # out-of-workspace path → route to the bash tool
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


async def test_grep_outside_workspace_passes_path_through_and_is_rejected(backend, client):
    """Regression: grepping an installed package under the sandbox home
    (``/home/daiv-sandbox/.local/...``) returned the misleading "does not exist" because the path was
    silently re-homed under the repo root ("/workspace/repo/home/...", which does not exist). The path
    must now reach the sandbox UNCHANGED so it is rejected as ``invalid_path``, and the agent is pointed
    at the bash tool for files outside /workspace — not told the file is absent."""
    client.fs_grep.return_value = FsGrepResponse(
        error=_err(FsErrorCode.INVALID_PATH, "path must be under one of ('/workspace',), got: ...")
    )
    out_of_workspace = "/home/daiv-sandbox/.local/lib/python3.14/site-packages/dbt/adapters/base/impl.py"
    result = await backend.agrep("catalog|filter", path=out_of_workspace, glob=None)
    # Sent unchanged — not rewritten to /workspace/repo/home/...
    assert client.fs_grep.call_args.args[1].path == out_of_workspace
    # Honest, actionable error — not the misleading "does not exist": it names /workspace and routes
    # the agent to the bash tool for files outside it.
    assert result.error is not None
    assert "does not exist" not in result.error
    assert "/workspace" in result.error and "bash" in result.error


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


async def test_unlink_transport_error_returns_false_and_logs(backend, client, caplog):
    """``unlink`` has no error channel (bare bool), so a transport fault is a failed unlink — logged
    so it is diagnosable rather than a silent False."""
    client.fs_delete.side_effect = _http_status_error(409, "Session is busy")
    with caplog.at_level("WARNING", logger="daiv.tools"):
        assert await backend.unlink("/workspace/repo/a.py") is False
    assert "transport failure" in caplog.text


async def test_refresh_egress_forwards_to_client(backend, client):
    from core.sandbox.schemas import EgressConfigRequest

    egress = EgressConfigRequest()
    await backend.refresh_egress(egress)

    # Forwarded to update_egress under the bound session id with the given config.
    client.update_egress.assert_awaited_once_with("sid", egress)


async def test_refresh_egress_before_bind_raises(client):
    from core.sandbox.schemas import EgressConfigRequest

    unbound = SandboxFileBackend(client=client)
    with pytest.raises(RuntimeError, match="not bound"):
        await unbound.refresh_egress(EgressConfigRequest())


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


def test_bind_session_sets_session_with_construction_client():
    from automation.agent.workspace.sandbox_backend import SandboxFileBackend

    backend = SandboxFileBackend(client=object())
    backend.bind_session("sess-1")
    assert backend._session_id == "sess-1"


def test_bind_session_rejects_cross_session_rebind():
    from automation.agent.workspace.sandbox_backend import SandboxFileBackend

    backend = SandboxFileBackend(client=object(), session_id="sess-1")
    with pytest.raises(RuntimeError, match="refusing"):
        backend.bind_session("sess-2")


def test_is_bound_requires_both_client_and_session():
    # is_bound is the non-raising counterpart of _require_bound and drives GitMiddleware's
    # slash-command short-circuit. BOTH conditions must hold (client AND session): an `or` slip,
    # or checking only the session, would pass the git tests (which always supply a client) yet
    # wrongly report a client-less backend as bound — so guard the two-condition logic directly.
    from automation.agent.workspace.sandbox_backend import SandboxFileBackend

    assert SandboxFileBackend(client=None).is_bound() is False
    assert SandboxFileBackend(client=object()).is_bound() is False  # client, no session
    assert SandboxFileBackend(client=object(), session_id="sess-1").is_bound() is True


class TestSandboxGrepTruncation:
    def _bound_backend(self, fs_grep_response):
        from unittest.mock import AsyncMock

        from automation.agent.workspace.sandbox_backend import SandboxFileBackend

        client = AsyncMock()
        client.fs_grep = AsyncMock(return_value=fs_grep_response)
        backend = SandboxFileBackend(client=client, session_id="sess-1")
        return backend

    async def test_truncated_response_sets_the_flag_without_a_synthetic_match(self):
        from automation.agent.constants import REPO_PATH
        from core.sandbox.schemas import FsGrepMatch, FsGrepResponse

        resp = FsGrepResponse(
            matches=[FsGrepMatch(path=f"{REPO_PATH}/f{i}.py", line=1, text="x") for i in range(3)], truncated=True
        )
        backend = self._bound_backend(resp)

        result = await backend.agrep("x", path=REPO_PATH)

        assert result.error is None
        # deepagents 0.7 renders its own truncation guidance from `truncated`, so the note is no
        # longer smuggled through a synthetic match's `path` to survive `files_with_matches`.
        assert result.truncated is True
        assert len(result.matches) == 3, "every returned match must be a real file"
        assert all(m["path"].startswith(REPO_PATH) for m in result.matches)

    async def test_untruncated_response_has_no_note(self):
        from automation.agent.constants import REPO_PATH
        from core.sandbox.schemas import FsGrepMatch, FsGrepResponse

        resp = FsGrepResponse(matches=[FsGrepMatch(path=f"{REPO_PATH}/a.py", line=1, text="x")], truncated=False)
        backend = self._bound_backend(resp)

        result = await backend.agrep("x", path=REPO_PATH)

        assert len(result.matches) == 1
        assert result.truncated is False

    async def test_max_count_trims_and_flags_truncation(self):
        from automation.agent.constants import REPO_PATH
        from core.sandbox.schemas import FsGrepMatch, FsGrepResponse

        resp = FsGrepResponse(
            matches=[FsGrepMatch(path=f"{REPO_PATH}/f{i}.py", line=1, text="x") for i in range(5)], truncated=False
        )
        backend = self._bound_backend(resp)

        result = await backend.agrep("x", path=REPO_PATH, max_count=2)

        assert len(result.matches) == 2
        assert result.truncated is True, "trimming on this side must still be reported as truncation"
        assert all(not m["path"].startswith("(grep results truncated") for m in result.matches)

    async def test_invalid_pattern_error_maps_to_model_hint(self):
        """The sandbox returns `invalid_pattern`; the backend must rewrite it to the actionable hint
        (this is the production path — daiv doesn't validate the regex itself for the sandbox)."""
        from automation.agent.constants import REPO_PATH
        from core.sandbox.schemas import FsError, FsErrorCode, FsGrepResponse

        resp = FsGrepResponse(error=FsError(code=FsErrorCode.INVALID_PATTERN, message="invalid regular expression"))
        backend = self._bound_backend(resp)

        result = await backend.agrep("foo(", path=REPO_PATH)

        assert not result.matches
        assert result.error is not None
        assert result.error.startswith("Grep 'foo(': ")
        assert "not a valid regular expression" in result.error
        assert "escape regex metacharacters" in result.error


class TestSandboxReadPagination:
    """`SandboxFileBackend.aread` maps the sandbox's reported line window onto deepagents'
    read-window fields, so the middleware's pagination notice is server-side truth rather than a
    count of the returned text (which would also count the truncation banner's rows)."""

    def _bound_backend(self, fs_read_response):
        from unittest.mock import AsyncMock

        from automation.agent.workspace.sandbox_backend import SandboxFileBackend

        client = AsyncMock()
        client.fs_read = AsyncMock(return_value=fs_read_response)
        return SandboxFileBackend(client=client, session_id="sess-1")

    async def test_mid_file_page_reports_the_exact_remainder(self):
        """The header names the window and carries the total, so the exact number of lines left is
        derivable — which only the sandbox's `total_lines` makes possible.

        deepagents 0.7.19 replaced the `_remaining_lines_notice` prose ("250 lines remaining from
        offset 150") with structured `_window_fields` header fields that state the window, the
        total, and the resume offset. The exact remainder is now encoded as
        `total_lines - next_offset` rather than spelled out, so both the fields and the derivable
        remainder are asserted.
        """
        from deepagents.middleware.filesystem import _window_fields

        from automation.agent.constants import REPO_PATH
        from core.sandbox.schemas import FsReadResponse

        content = "".join(f"line {i}\n" for i in range(101, 151))
        backend = self._bound_backend(FsReadResponse(content=content, encoding="utf-8", total_lines=400, end_line=150))

        result = await backend.aread(f"{REPO_PATH}/f.py", offset=100, limit=50)

        assert result.start_line == 101
        assert result.end_line == 150
        assert result.total_lines == 400
        assert result.next_offset == 150
        fields = _window_fields(result)
        assert "lines 101-150 of 400" in fields
        assert "next offset 150" in fields
        assert result.total_lines - result.next_offset == 250

    async def test_final_page_emits_no_notice(self):
        from deepagents.middleware.filesystem import _window_fields

        from automation.agent.constants import REPO_PATH
        from core.sandbox.schemas import FsReadResponse

        content = "".join(f"line {i}\n" for i in range(351, 401))
        backend = self._bound_backend(FsReadResponse(content=content, encoding="utf-8", total_lines=400, end_line=400))

        result = await backend.aread(f"{REPO_PATH}/f.py", offset=350, limit=50)

        assert result.end_line == 400
        assert result.next_offset is None, "the window reached EOF"
        # A final page advertises no resume offset: `_window_fields` emits only the span.
        assert _window_fields(result) == ["lines 351-400 of 400"]

    async def test_file_length_that_is_an_exact_multiple_of_limit_has_no_next_offset(self):
        """A window ending exactly at `total_lines` is EOF, not a full page — advertising a resume
        offset here would name an offset the next read rejects as invalid."""
        from automation.agent.constants import REPO_PATH
        from core.sandbox.schemas import FsReadResponse

        content = "".join(f"line {i}\n" for i in range(1, 101))
        backend = self._bound_backend(FsReadResponse(content=content, encoding="utf-8", total_lines=100, end_line=100))

        result = await backend.aread(f"{REPO_PATH}/f.py", offset=0, limit=100)

        assert result.end_line == 100
        assert result.next_offset is None

    async def test_byte_capped_page_resumes_at_the_partial_line(self):
        """A capped page ends mid-line and carries a banner. The window must exclude both, so the
        resume offset points *at* the partial line and re-reads it whole."""
        from automation.agent.constants import REPO_PATH
        from core.sandbox.schemas import FsReadResponse

        # The banner text is illustrative; `aread` never parses `content`.
        content = "".join(f"line {i}\n" for i in range(1, 98)) + "line 98 is cut he"
        content += "\n\n[Output truncated: exceeded the 512000-byte read limit.]"
        backend = self._bound_backend(
            FsReadResponse(content=content, encoding="utf-8", total_lines=400, end_line=97, truncated=True)
        )

        result = await backend.aread(f"{REPO_PATH}/f.py", offset=0, limit=100)

        assert result.start_line == 1
        assert result.end_line == 97, "the banner rows and the cut line are not source lines"
        assert result.total_lines == 400
        assert result.next_offset == 97, "0-indexed 97 is 1-indexed line 98 — the line that was cut"

    async def test_byte_capped_single_huge_line_carries_no_window(self, caplog):
        """A page whose first line alone exceeds the byte cap holds zero complete lines. deepagents
        cannot express an empty window, so the model gets none — and no resume offset, which is a
        dead end worth an operator warning."""
        import logging

        from automation.agent.constants import REPO_PATH
        from core.sandbox.schemas import FsReadResponse

        backend = self._bound_backend(
            FsReadResponse(content="x" * 100, encoding="utf-8", total_lines=1, end_line=0, truncated=True)
        )

        with caplog.at_level(logging.WARNING, logger="daiv.tools"):
            result = await backend.aread(f"{REPO_PATH}/f.py", offset=0, limit=100)

        assert result.start_line is None
        assert result.end_line is None
        assert result.total_lines is None
        assert result.next_offset is None
        assert "no complete line" in caplog.text

    async def test_binary_read_carries_no_window(self):
        from automation.agent.constants import REPO_PATH
        from core.sandbox.schemas import FsReadResponse

        backend = self._bound_backend(FsReadResponse(content="Zm9vYmFy", encoding="base64"))

        result = await backend.aread(f"{REPO_PATH}/img.png", offset=0, limit=100)

        assert result.start_line is None
        assert result.end_line is None
        assert result.next_offset is None

    async def test_empty_file_sentinel_carries_no_window(self):
        """The sandbox returns a sentinel string (not the file's bytes) for an empty file; it is not
        file content, so it is not line-windowed."""
        from deepagents.middleware.filesystem import EMPTY_CONTENT_WARNING

        from automation.agent.constants import REPO_PATH
        from core.sandbox.schemas import FsReadResponse

        backend = self._bound_backend(FsReadResponse(content=EMPTY_CONTENT_WARNING, encoding="utf-8"))

        result = await backend.aread(f"{REPO_PATH}/empty.py", offset=0, limit=1)

        assert result.start_line is None
        assert result.end_line is None
        assert result.next_offset is None

    async def test_sandbox_without_line_metadata_drops_the_window_and_warns(self, caplog):
        """A sandbox predating the wire fields returns them unset. Losing the notice is acceptable;
        guessing a window is not — but the version skew must be visible to operators."""
        import logging

        from automation.agent.constants import REPO_PATH
        from core.sandbox.schemas import FsReadResponse

        content = "".join(f"line {i}\n" for i in range(1, 101))
        backend = self._bound_backend(FsReadResponse(content=content, encoding="utf-8"))

        with caplog.at_level(logging.WARNING, logger="daiv.tools"):
            result = await backend.aread(f"{REPO_PATH}/f.py", offset=0, limit=100)

        assert result.start_line is None
        assert result.end_line is None
        assert result.total_lines is None
        assert result.next_offset is None
        assert "no read-window metadata" in caplog.text

    @pytest.mark.parametrize(
        ("response_kwargs", "expected"),
        [({}, "no read-window metadata"), ({"total_lines": 9999, "end_line": 9000}, "outside the requested window")],
        ids=["version-skew", "malformed-window"],
    )
    async def test_read_faults_are_reported_once_per_run(self, caplog, response_kwargs, expected):
        """Every cause here is systematic — a version skew or a sandbox arithmetic slip repeats on
        every read — and each ERROR is a Sentry event, so one per run is the whole point."""
        import logging

        from automation.agent.constants import REPO_PATH
        from core.sandbox.schemas import FsReadResponse

        content = "".join(f"line {i}\n" for i in range(1, 11))
        backend = self._bound_backend(FsReadResponse(content=content, encoding="utf-8", **response_kwargs))

        with caplog.at_level(logging.WARNING, logger="daiv.tools"):
            for _ in range(5):
                await backend.aread(f"{REPO_PATH}/f.py", offset=0, limit=100)

        assert caplog.text.count(expected) == 1

    async def test_total_lines_below_end_line_degrades_instead_of_raising(self, caplog):
        """deepagents' `ReadResult` rejects this pair outright, and `aread` is called unguarded, so
        passing it through would abort the run over a sandbox arithmetic slip."""
        import logging

        from automation.agent.constants import REPO_PATH
        from core.sandbox.schemas import FsReadResponse

        content = "".join(f"line {i}\n" for i in range(101, 151))
        backend = self._bound_backend(FsReadResponse(content=content, encoding="utf-8", total_lines=120, end_line=150))

        with caplog.at_level(logging.ERROR, logger="daiv.tools"):
            result = await backend.aread(f"{REPO_PATH}/f.py", offset=100, limit=50)

        assert result.file_data is not None, "the content still reaches the model"
        assert result.total_lines is None, "an impossible total is dropped, not clamped"
        assert result.next_offset == 150
        assert "total_lines=120 below end_line=150" in caplog.text

    async def test_end_line_past_the_requested_window_drops_the_window(self, caplog):
        """`end_line` cannot exceed `offset + limit`. Trusting a larger one hands the model a resume
        offset past source lines it was never shown."""
        import logging

        from automation.agent.constants import REPO_PATH
        from core.sandbox.schemas import FsReadResponse

        content = "".join(f"line {i}\n" for i in range(1, 11))
        backend = self._bound_backend(
            FsReadResponse(content=content, encoding="utf-8", total_lines=9999, end_line=9000)
        )

        with caplog.at_level(logging.ERROR, logger="daiv.tools"):
            result = await backend.aread(f"{REPO_PATH}/f.py", offset=0, limit=100)

        assert result.next_offset is None
        assert result.start_line is None
        assert "outside the requested window" in caplog.text

    async def test_window_over_empty_content_drops_the_window(self, caplog):
        """A window with no text behind it would page the model forward over lines it never read."""
        import logging

        from automation.agent.constants import REPO_PATH
        from core.sandbox.schemas import FsReadResponse

        backend = self._bound_backend(FsReadResponse(content="", encoding="utf-8", total_lines=400, end_line=150))

        with caplog.at_level(logging.ERROR, logger="daiv.tools"):
            result = await backend.aread(f"{REPO_PATH}/f.py", offset=100, limit=50)

        assert result.start_line is None
        assert result.next_offset is None
        assert "over empty content" in caplog.text

    async def test_window_without_a_total_still_advertises_a_resume_offset(self):
        """Half-populated metadata must not read as EOF: an over-advertised offset self-corrects on
        the next read, a missing one silently drops the rest of the file."""
        from deepagents.middleware.filesystem import _window_fields

        from automation.agent.constants import REPO_PATH
        from core.sandbox.schemas import FsReadResponse

        content = "".join(f"line {i}\n" for i in range(1, 51))
        backend = self._bound_backend(FsReadResponse(content=content, encoding="utf-8", end_line=50))

        result = await backend.aread(f"{REPO_PATH}/f.py", offset=0, limit=100)

        assert result.total_lines is None
        assert result.next_offset == 50
        # Without a total, `_window_fields` still advertises the resume offset so the
        # rest of the file is not silently dropped.
        fields = _window_fields(result)
        assert "lines 1-50" in fields
        assert "next offset 50" in fields
