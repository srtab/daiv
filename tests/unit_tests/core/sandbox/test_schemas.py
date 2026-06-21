import base64

import pytest


def test_put_mutation_validates_mode_range():
    from core.sandbox.schemas import PutMutation

    PutMutation(path="/repo/foo.py", content=base64.b64encode(b"x"), mode=0o644)
    with pytest.raises(ValueError):
        PutMutation(path="/repo/foo.py", content=base64.b64encode(b"x"), mode=-1)
    with pytest.raises(ValueError):
        PutMutation(path="/repo/foo.py", content=base64.b64encode(b"x"), mode=0o10000)


def test_run_commands_request_no_archive():
    from core.sandbox.schemas import RunCommandsRequest

    assert "archive" not in RunCommandsRequest.model_fields


def test_start_session_request_no_ephemeral():
    from core.sandbox.schemas import StartSessionRequest

    assert "ephemeral" not in StartSessionRequest.model_fields


def test_start_session_request_no_extract_patch():
    from core.sandbox.schemas import StartSessionRequest

    assert "extract_patch" not in StartSessionRequest.model_fields


def test_run_commands_response_no_patch():
    from core.sandbox.schemas import RunCommandsResponse

    assert "patch" not in RunCommandsResponse.model_fields


def test_run_command_result_preserves_full_output():
    """Command output is returned in full — no line-count truncation.

    The internal ``GitManager`` needs the complete ``git diff`` (a cut-mid-hunk diff is
    unparseable), and the agent's ``bash`` tool offloads oversized output to a file via the
    filesystem middleware's token-threshold eviction. So the wire type must not silently
    drop lines.
    """
    from core.sandbox.schemas import RunCommandResult

    output = "\n".join(f"line {i}" for i in range(5000))
    result = RunCommandResult(command="git diff", output=output, exit_code=0)
    assert result.output.count("\n") == output.count("\n")
    assert result.output.endswith("line 4999")


def test_fs_error_message_must_be_non_empty():
    from core.sandbox.schemas import FsError, FsErrorCode

    FsError(code=FsErrorCode.NOT_FOUND, message="x")
    with pytest.raises(ValueError):
        FsError(code=FsErrorCode.NOT_FOUND, message="")


def test_fs_write_response_ok_is_derived_from_error():
    """``ok`` is no longer a stored field: it is derived as ``error is None`` so the two can never
    contradict (and so it stays absent from the validation schema the drift test pins)."""
    from core.sandbox.schemas import FsError, FsErrorCode, FsWriteResponse

    assert FsWriteResponse().ok is True
    assert FsWriteResponse(error=FsError(code=FsErrorCode.EXEC_FAILED, message="boom")).ok is False
    assert "ok" not in FsWriteResponse.model_fields


def test_fs_delete_response_removed_and_ok():
    from core.sandbox.schemas import FsDeleteResponse, FsError, FsErrorCode

    removed = FsDeleteResponse(removed=True)
    assert removed.removed is True and removed.ok is True

    absent = FsDeleteResponse()  # idempotent no-op: success, but nothing removed
    assert absent.removed is False and absent.ok is True

    failed = FsDeleteResponse(error=FsError(code=FsErrorCode.IS_A_DIRECTORY, message="is a directory"))
    assert failed.ok is False and failed.error.code is FsErrorCode.IS_A_DIRECTORY
