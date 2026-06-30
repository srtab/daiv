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


def test_egress_rule_uppercases_methods_and_rejects_empty():
    from core.sandbox.schemas import EgressRule

    assert EgressRule(host="x", methods=["get", "post"]).methods == ["GET", "POST"]
    with pytest.raises(ValueError):
        EgressRule(host="x", methods=[])


def test_egress_rule_rejects_blank_and_crlf_host():
    """Host validity is enforced on the type (not only the form) so from_stored/apply_platform_egress
    can't carry a blank or CR/LF-smuggled host through to the sidecar. A surrounding-whitespace host
    is normalised."""
    from core.sandbox.schemas import EgressRule

    assert EgressRule(host="  github.com  ").host == "github.com"
    with pytest.raises(ValueError, match="blank"):
        EgressRule(host="   ")
    with pytest.raises(ValueError, match="CR or LF"):
        EgressRule(host="github.com\r\nHost: evil.example")


def test_egress_secret_rejects_crlf_and_blank_header_and_value():
    """Header-injection vectors are rejected at the parse boundary for every construction path."""
    from pydantic import SecretStr

    from core.sandbox.schemas import EgressSecret

    with pytest.raises(ValueError, match="CR or LF"):
        EgressSecret(header="Authorization\r\nX-Evil: 1", value=SecretStr("Bearer t"))
    with pytest.raises(ValueError, match="blank"):
        EgressSecret(header="  ", value=SecretStr("Bearer t"))
    with pytest.raises(ValueError, match="CR or LF"):
        EgressSecret(header="Authorization", value=SecretStr("Bearer t\r\nX-Evil: 1"))


def test_egress_config_request_rejects_dangling_inject():
    from core.sandbox.schemas import EgressConfigRequest, EgressPolicy, EgressRule

    with pytest.raises(ValueError, match="unknown secret"):
        EgressConfigRequest(policy=EgressPolicy(rules=[EgressRule(host="x", inject="missing")]), secrets={})


def test_empty_egress_config_request_is_deny_all():
    """The fail-closed fallback in ``row_to_override`` substitutes a bare ``EgressConfigRequest()``
    for an unusable stored config. That is only safe while the empty request denies everything — a
    future flip of ``EgressPolicy.default`` to ``allow`` would silently turn the fallback fail-open."""
    from core.sandbox.schemas import EgressConfigRequest

    req = EgressConfigRequest()
    assert req.policy.default == "deny"
    assert req.policy.rules == []
    assert req.secrets == {}


def test_egress_secret_value_is_redacted_in_repr():
    from pydantic import SecretStr

    from core.sandbox.schemas import EgressSecret

    s = EgressSecret(header="Authorization", value=SecretStr("Bearer t"))
    assert "Bearer t" not in repr(s)
    assert s.value.get_secret_value() == "Bearer t"


def test_to_wire_unwraps_secret_and_carries_every_policy_field():
    """``to_wire`` must send plaintext secrets AND every ``EgressPolicy`` field. Pinning the policy
    block to ``model_dump`` guards against the hand-built wire dict silently dropping a field added
    to ``EgressPolicy`` later (the reason ``to_wire`` exists instead of inlining in the client)."""
    from core.sandbox.schemas import EgressConfigRequest, EgressPolicy

    req = EgressConfigRequest.from_stored(
        {"default": "deny", "intercept": "credentialed", "rules": [{"host": "gitlab.com", "inject": "tok"}]},
        {"tok": {"header": "PRIVATE-TOKEN", "value": "supersecret"}},
    )
    wire = req.to_wire()
    assert wire["secrets"] == {"tok": {"header": "PRIVATE-TOKEN", "value": "supersecret"}}
    # Every field of the policy schema is present (no silent drop) and the masked dump is not used.
    assert set(wire["policy"]) == set(EgressPolicy.model_fields)
    assert wire["policy"]["intercept"] == "credentialed"
