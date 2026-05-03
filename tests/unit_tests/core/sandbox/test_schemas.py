import base64

import pytest


def test_put_mutation_validates_mode_range():
    from core.sandbox.schemas import PutMutation

    PutMutation(path="/repo/foo.py", content=base64.b64encode(b"x"), mode=0o644)
    with pytest.raises(ValueError):
        PutMutation(path="/repo/foo.py", content=base64.b64encode(b"x"), mode=-1)
    with pytest.raises(ValueError):
        PutMutation(path="/repo/foo.py", content=base64.b64encode(b"x"), mode=0o10000)


def test_seed_session_request_carries_repo_archive():
    from core.sandbox.schemas import SeedSessionRequest

    req = SeedSessionRequest(repo_archive=base64.b64encode(b"tar"))
    assert req.repo_archive == b"tar"


def test_run_commands_request_no_archive():
    from core.sandbox.schemas import RunCommandsRequest

    assert "archive" not in RunCommandsRequest.model_fields


def test_start_session_request_no_ephemeral():
    from core.sandbox.schemas import StartSessionRequest

    assert "ephemeral" not in StartSessionRequest.model_fields
