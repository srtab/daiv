from unittest.mock import AsyncMock, patch

import pytest
from activity.models import Activity, ActivityStatus, TriggerType

from accounts.models import User


async def _user(username):
    return await User.objects.acreate_user(username=username, email=f"{username}@e.com", password="x")  # noqa: S106


@pytest.mark.django_db(transaction=True)
async def test_list_jobs_returns_user_jobs_and_excludes_result_summary():
    from mcp_server.server import list_jobs

    user = await _user("lj1")
    await Activity.objects.acreate(
        user=user,
        repo_id="a/b",
        status=ActivityStatus.SUCCESSFUL,
        trigger_type=TriggerType.MCP_JOB,
        result_summary="secret detail",
    )
    with patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=user)):
        data = await list_jobs()
    assert data["truncated"] is False
    assert len(data["jobs"]) == 1
    job = data["jobs"][0]
    assert job["repo_id"] == "a/b"
    assert job["status"] == "SUCCESSFUL"
    assert "result_summary" not in job


@pytest.mark.django_db(transaction=True)
async def test_list_jobs_truncates_and_caps():
    from mcp_server.server import list_jobs

    user = await _user("lj2")
    for _ in range(3):
        await Activity.objects.acreate(
            user=user, repo_id="a/b", status=ActivityStatus.READY, trigger_type=TriggerType.MCP_JOB
        )
    with patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=user)):
        data = await list_jobs(limit=2)
    assert len(data["jobs"]) == 2
    assert data["truncated"] is True


@pytest.mark.django_db(transaction=True)
async def test_list_jobs_unauthenticated_returns_error():
    from mcp_server.server import list_jobs

    with patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=None)):
        data = await list_jobs()
    assert "error" in data
