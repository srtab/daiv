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
    assert empty["next_cursor"] is None


@pytest.mark.django_db(transaction=True)
async def test_list_scheduled_jobs_cursor_paginates_without_overlap():
    """Walking pages via next_cursor covers every schedule once, newest first."""
    from mcp_server.server import list_scheduled_jobs, schedule_job

    user = await _user("ls_pg")
    now = timezone.now()
    names = ["A", "B", "C", "D", "E"]
    with patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=user)):
        for name in names:
            await schedule_job(
                name=name, prompt="p", repos=[{"repo_id": "a/b", "ref": ""}], frequency=Frequency.DAILY, time="09:00"
            )
        # Assign created by name so ordering is deterministic: "E" newest (now), "A" oldest.
        for i, name in enumerate(names):
            job = await ScheduledJob.objects.aget(user=user, name=name)
            await ScheduledJob.objects.filter(pk=job.pk).aupdate(created=now - timedelta(minutes=len(names) - 1 - i))

        seen: list[str] = []
        cursor = None
        for _ in range(10):
            data = await list_scheduled_jobs(limit=2, cursor=cursor)
            seen.extend(s["name"] for s in data["scheduled_jobs"])
            cursor = data["next_cursor"]
            if cursor is None:
                break

    assert cursor is None
    assert seen == ["E", "D", "C", "B", "A"]  # newest-first, no gaps or repeats


@pytest.mark.django_db(transaction=True)
async def test_list_scheduled_jobs_cursor_tie_break_on_same_created():
    """Schedules use an integer PK stringified into the cursor. With >10 rows sharing an
    identical ``created``, the id tie-break must order numerically (not lexically) so the
    9→10 boundary neither skips nor repeats a row."""
    from mcp_server.server import list_scheduled_jobs, schedule_job

    user = await _user("ls_tie")
    same = timezone.now()
    with patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=user)):
        for i in range(11):
            await schedule_job(
                name=f"s{i}", prompt="p", repos=[{"repo_id": "a/b", "ref": ""}], frequency=Frequency.DAILY, time="09:00"
            )
        await ScheduledJob.objects.filter(user=user).aupdate(created=same)

        seen: list[str] = []
        cursor = None
        for _ in range(20):
            data = await list_scheduled_jobs(limit=2, cursor=cursor)
            seen.extend(s["name"] for s in data["scheduled_jobs"])
            cursor = data["next_cursor"]
            if cursor is None:
                break

    assert sorted(seen) == sorted(f"s{i}" for i in range(11))
    assert len(seen) == len(set(seen))  # each row exactly once across the 9→10 boundary


@pytest.mark.django_db(transaction=True)
async def test_list_scheduled_jobs_invalid_cursor_returns_error():
    from mcp_server.server import list_scheduled_jobs

    user = await _user("ls_badc")
    with patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=user)):
        data = await list_scheduled_jobs(cursor="not-valid")
    assert "error" in data
    assert "cursor" in data["error"].lower()


@pytest.mark.django_db(transaction=True)
async def test_list_scheduled_jobs_wrong_tool_cursor_returns_invalid_not_transient():
    """A cursor from list_jobs (UUID id) reused on list_scheduled_jobs (int PK) is a permanent
    client error, not a transient server fault: it must decode-coerce to a caught error and
    return "Invalid cursor.", never the generic "try again later." message."""
    import uuid

    from mcp_server.server import _encode_cursor, list_scheduled_jobs

    user = await _user("ls_xtool")
    # A well-formed base64(JSON) cursor whose id is a UUID string (as list_jobs would emit).
    bad = _encode_cursor({"c": timezone.now().isoformat(), "id": str(uuid.uuid4())})
    with patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=user)):
        data = await list_scheduled_jobs(cursor=bad)
    assert data.get("error") == "Invalid cursor."


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
