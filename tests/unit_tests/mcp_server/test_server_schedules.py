from unittest.mock import AsyncMock, patch

import pytest

from accounts.models import User
from schedules.models import Frequency, ScheduledJob


async def _user(username):
    return await User.objects.acreate_user(username=username, email=f"{username}@e.com", password="x")  # noqa: S106


@pytest.mark.django_db(transaction=True)
async def test_schedule_job_creates_daily_schedule():
    from mcp_server.server import schedule_job

    user = await _user("sj1")
    with patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=user)):
        data = await schedule_job(
            name="Nightly",
            prompt="run audit",
            repos=[{"repo_id": "a/b", "ref": ""}],
            frequency=Frequency.DAILY,
            time="09:00",
        )
    assert "error" not in data
    assert data["name"] == "Nightly"
    assert data["next_run_at"] is not None
    assert await ScheduledJob.objects.filter(user=user, name="Nightly").aexists()


@pytest.mark.django_db(transaction=True)
async def test_schedule_job_bad_time_format_returns_error():
    from mcp_server.server import schedule_job

    user = await _user("sj2")
    with patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=user)):
        data = await schedule_job(
            name="x", prompt="p", repos=[{"repo_id": "a/b", "ref": ""}], frequency=Frequency.DAILY, time="9am"
        )
    assert "error" in data
    assert not await ScheduledJob.objects.filter(user=user).aexists()


@pytest.mark.django_db(transaction=True)
async def test_schedule_job_validation_error_is_mapped():
    from mcp_server.server import schedule_job

    user = await _user("sj3")
    with patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=user)):
        data = await schedule_job(
            name="x", prompt="p", repos=[{"repo_id": "a/b", "ref": ""}], frequency=Frequency.CUSTOM, cron_expression=""
        )
    assert "error" in data


@pytest.mark.django_db(transaction=True)
async def test_schedule_job_unknown_environment_returns_error():
    from mcp_server.server import schedule_job

    user = await _user("sj4")
    with patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=user)):
        data = await schedule_job(
            name="x",
            prompt="p",
            repos=[{"repo_id": "a/b", "ref": ""}],
            frequency=Frequency.DAILY,
            time="09:00",
            environment="nope",
        )
    assert "error" in data
    assert "nope" in data["error"]
