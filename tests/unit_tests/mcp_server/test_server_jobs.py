from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from django.utils import timezone

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


@pytest.mark.django_db(transaction=True)
async def test_list_jobs_orders_newest_first():
    from mcp_server.server import list_jobs

    user = await _user("lj3")
    older = await Activity.objects.acreate(
        user=user, repo_id="a/b", status=ActivityStatus.READY, trigger_type=TriggerType.MCP_JOB
    )
    newer = await Activity.objects.acreate(
        user=user, repo_id="a/b", status=ActivityStatus.READY, trigger_type=TriggerType.MCP_JOB
    )
    # created_at is auto_now_add, so nudge it explicitly to make ordering observable.
    now = timezone.now()
    await Activity.objects.filter(pk=older.pk).aupdate(created_at=now - timedelta(hours=1))
    await Activity.objects.filter(pk=newer.pk).aupdate(created_at=now)
    with patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=user)):
        data = await list_jobs()
    assert [j["job_id"] for j in data["jobs"]] == [str(newer.id), str(older.id)]


@pytest.mark.django_db(transaction=True)
async def test_list_jobs_not_truncated_at_exact_limit():
    from mcp_server.server import list_jobs

    user = await _user("lj4")
    for _ in range(2):
        await Activity.objects.acreate(
            user=user, repo_id="a/b", status=ActivityStatus.READY, trigger_type=TriggerType.MCP_JOB
        )
    with patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=user)):
        data = await list_jobs(limit=2)
    assert len(data["jobs"]) == 2
    assert data["truncated"] is False


@pytest.mark.django_db(transaction=True)
async def test_list_jobs_serializes_cost_and_tokens():
    from mcp_server.server import list_jobs

    user = await _user("lj5")
    await Activity.objects.acreate(
        user=user,
        repo_id="a/b",
        status=ActivityStatus.SUCCESSFUL,
        trigger_type=TriggerType.MCP_JOB,
        cost_usd=Decimal("1.234567"),
        total_tokens=4242,
    )
    with patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=user)):
        data = await list_jobs()
    job = data["jobs"][0]
    # cost_usd is stringified (Decimal is not JSON-serializable); total_tokens stays an int.
    assert job["cost_usd"] == "1.234567"
    assert job["total_tokens"] == 4242


@pytest.mark.django_db(transaction=True)
async def test_list_jobs_status_filter():
    from mcp_server.server import list_jobs

    user = await _user("lj6")
    await Activity.objects.acreate(
        user=user, repo_id="a/b", status=ActivityStatus.RUNNING, trigger_type=TriggerType.MCP_JOB
    )
    await Activity.objects.acreate(
        user=user, repo_id="a/b", status=ActivityStatus.SUCCESSFUL, trigger_type=TriggerType.MCP_JOB
    )
    with patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=user)):
        data = await list_jobs(status=ActivityStatus.RUNNING)
    assert {j["status"] for j in data["jobs"]} == {"RUNNING"}


@pytest.mark.django_db(transaction=True)
async def test_list_jobs_db_error_returns_friendly_error():
    from mcp_server.server import list_jobs

    user = await _user("lj7")
    with (
        patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=user)),
        patch("mcp_server.server.alist_user_activities", new=AsyncMock(side_effect=RuntimeError("db down"))),
    ):
        data = await list_jobs()
    assert "error" in data
    assert "db down" not in data["error"]  # internal detail not leaked


@pytest.mark.django_db(transaction=True)
async def test_list_jobs_auth_exception_returns_error():
    from mcp_server.server import list_jobs

    with patch("mcp_server.server.get_current_user", new=AsyncMock(side_effect=RuntimeError("boom"))):
        data = await list_jobs()
    assert "error" in data
