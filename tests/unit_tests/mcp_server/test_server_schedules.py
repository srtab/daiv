from datetime import timedelta
from unittest.mock import AsyncMock, patch

from django.utils import timezone

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
async def test_schedule_job_once_with_aware_datetime_creates_schedule():
    from mcp_server.server import schedule_job

    user = await _user("sj6")
    run_at = (timezone.now() + timedelta(days=1)).replace(microsecond=0)
    with patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=user)):
        data = await schedule_job(
            name="OneOff",
            prompt="p",
            repos=[{"repo_id": "a/b", "ref": ""}],
            frequency=Frequency.ONCE,
            run_at=run_at.isoformat(),
        )
    assert "error" not in data
    assert data["frequency"] == "once"
    assert data["next_run_at"] is not None
    job = await ScheduledJob.objects.aget(user=user, name="OneOff")
    assert job.frequency == Frequency.ONCE
    assert job.run_at == run_at
    assert job.next_run_at == run_at


@pytest.mark.django_db(transaction=True)
async def test_schedule_job_once_naive_datetime_is_coerced_to_aware():
    from mcp_server.server import schedule_job

    user = await _user("sj7")
    naive = (timezone.now() + timedelta(days=1)).replace(microsecond=0, tzinfo=None)
    with patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=user)):
        data = await schedule_job(
            name="OneOffNaive",
            prompt="p",
            repos=[{"repo_id": "a/b", "ref": ""}],
            frequency=Frequency.ONCE,
            run_at=naive.isoformat(),
        )
    assert "error" not in data
    assert data["next_run_at"] is not None
    job = await ScheduledJob.objects.aget(user=user, name="OneOffNaive")
    assert timezone.is_aware(job.run_at)


@pytest.mark.django_db(transaction=True)
async def test_schedule_job_invalid_run_at_returns_error():
    from mcp_server.server import schedule_job

    user = await _user("sj8")
    with patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=user)):
        data = await schedule_job(
            name="x", prompt="p", repos=[{"repo_id": "a/b", "ref": ""}], frequency=Frequency.ONCE, run_at="not-a-date"
        )
    assert "error" in data
    assert "run_at" in data["error"]
    assert not await ScheduledJob.objects.filter(user=user).aexists()


@pytest.mark.django_db(transaction=True)
async def test_schedule_job_past_run_at_returns_error():
    from mcp_server.server import schedule_job

    user = await _user("sj9")
    past = (timezone.now() - timedelta(days=1)).replace(microsecond=0)
    with patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=user)):
        data = await schedule_job(
            name="x",
            prompt="p",
            repos=[{"repo_id": "a/b", "ref": ""}],
            frequency=Frequency.ONCE,
            run_at=past.isoformat(),
        )
    assert "error" in data
    assert not await ScheduledJob.objects.filter(user=user).aexists()


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
    assert not await ScheduledJob.objects.filter(user=user).aexists()


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


@pytest.mark.django_db(transaction=True)
async def test_schedule_job_invalid_agent_model_returns_error():
    from mcp_server.server import schedule_job

    user = await _user("sj5")
    with patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=user)):
        data = await schedule_job(
            name="x",
            prompt="p",
            repos=[{"repo_id": "a/b", "ref": ""}],
            frequency=Frequency.DAILY,
            time="09:00",
            agent_model="unknownprovider:some-model",
        )
    assert "error" in data
    assert not await ScheduledJob.objects.filter(user=user).aexists()


@pytest.mark.django_db(transaction=True)
async def test_list_scheduled_jobs_scopes_and_filters():
    from mcp_server.server import list_scheduled_jobs, schedule_job

    user = await _user("ls1")
    with patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=user)):
        await schedule_job(
            name="A", prompt="p", repos=[{"repo_id": "a/b", "ref": ""}], frequency=Frequency.DAILY, time="09:00"
        )
        listed = await list_scheduled_jobs()
        filtered = await list_scheduled_jobs(repo_id="a/b")
        empty = await list_scheduled_jobs(repo_id="z/z")

    assert [s["name"] for s in listed["scheduled_jobs"]] == ["A"]
    assert listed["scheduled_jobs"][0]["time"] == "09:00"
    assert len(filtered["scheduled_jobs"]) == 1
    assert empty["scheduled_jobs"] == []


@pytest.mark.django_db(transaction=True)
async def test_list_scheduled_jobs_unauthenticated_returns_error():
    from mcp_server.server import list_scheduled_jobs

    with patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=None)):
        data = await list_scheduled_jobs()
    assert "error" in data


@pytest.mark.django_db(transaction=True)
async def test_list_scheduled_jobs_db_error_returns_friendly_error():
    from mcp_server.server import list_scheduled_jobs

    user = await _user("ls2")
    with (
        patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=user)),
        patch("mcp_server.server.alist_scheduled_jobs", new=AsyncMock(side_effect=RuntimeError("db down"))),
    ):
        data = await list_scheduled_jobs()
    assert "error" in data
    assert "db down" not in data["error"]  # internal detail not leaked
