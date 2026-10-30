import json
import uuid
from unittest.mock import AsyncMock, patch

from django.test import Client

import pytest
from asgiref.sync import async_to_sync
from mcp_connectors.models import MCPServer
from sessions.services import BatchSubmitResult

from accounts.models import APIKey, User


@pytest.fixture
def api_key(db):
    MCPServer.objects.create(
        name="grafana", transport=MCPServer.Transport.HTTP, url="http://grafana", status=MCPServer.Status.ON_DEMAND
    )
    user = User.objects.create_user(username="u", email="u@e.com", password="x")  # noqa: S106
    _, raw_key = async_to_sync(APIKey.objects.create_key)(user=user, name="t", expires_at=None)
    return raw_key


def _post(api_key: str, **fields):
    body = {"prompt": "do work", "repos": [{"repo_id": "r/p", "ref": ""}], **fields}
    fake_run = type("R", (), {"id": uuid.uuid4(), "session_id": str(uuid.uuid4()), "status": "READY"})()
    with patch("jobs.api.views.asubmit_batch_runs", AsyncMock()) as submit:
        submit.return_value = BatchSubmitResult(batch_id=uuid.uuid4(), outcomes=[fake_run])
        response = Client().post(
            "/api/jobs", data=json.dumps(body), content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {api_key}"
        )
    return response, submit


@pytest.mark.django_db
def test_mcp_overrides_reach_the_submitted_runs(api_key):
    response, submit = _post(api_key, mcp_overrides={"grafana": True})

    assert response.status_code == 202
    assert submit.await_args.kwargs["mcp_overrides"] == {"grafana": "on"}


@pytest.mark.django_db
def test_omitted_mcp_overrides_leave_the_default_set(api_key):
    response, submit = _post(api_key)

    assert response.status_code == 202
    assert submit.await_args.kwargs["mcp_overrides"] is None


@pytest.mark.django_db
def test_unknown_mcp_server_is_rejected(api_key):
    response, submit = _post(api_key, mcp_overrides={"grafna": True})

    assert response.status_code == 400
    assert "grafna" in response.json()["detail"]
    submit.assert_not_awaited()


@pytest.mark.django_db
def test_mcp_override_value_must_be_a_boolean(api_key):
    response, _ = _post(api_key, mcp_overrides={"grafana": "yes"})

    assert response.status_code == 422
