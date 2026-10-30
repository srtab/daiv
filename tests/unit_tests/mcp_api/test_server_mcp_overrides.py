import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from mcp_connectors.models import MCPServer
from sessions.services import BatchSubmitResult


async def _submit(**kwargs):
    from mcp_api.server import submit_job

    from accounts.models import User

    await MCPServer.objects.acreate(
        name="grafana", transport=MCPServer.Transport.HTTP, url="http://grafana", status=MCPServer.Status.ON_DEMAND
    )
    user = await User.objects.acreate_user(username="u", email="u@e.com", password="x")  # noqa: S106
    fake_run = type("R", (), {"id": uuid.uuid4(), "session_id": None, "status": "READY"})()
    with (
        patch("mcp_api.server.get_current_user", new=AsyncMock(return_value=user)),
        patch("mcp_api.server.asubmit_batch_runs", new=AsyncMock()) as submit,
    ):
        submit.return_value = BatchSubmitResult(batch_id=uuid.uuid4(), outcomes=[fake_run])
        result = await submit_job(prompt="p", repos=[{"repo_id": "r/p", "ref": ""}], **kwargs)
    return json.loads(result), submit


@pytest.mark.django_db(transaction=True)
async def test_submit_job_passes_mcp_overrides_to_the_runs():
    _, submit = await _submit(mcp_overrides={"grafana": True})

    assert submit.await_args.kwargs["mcp_overrides"] == {"grafana": "on"}


@pytest.mark.django_db(transaction=True)
async def test_submit_job_rejects_an_unknown_mcp_server():
    data, submit = await _submit(mcp_overrides={"grafna": True})

    assert "grafna" in data["error"]
    submit.assert_not_awaited()
